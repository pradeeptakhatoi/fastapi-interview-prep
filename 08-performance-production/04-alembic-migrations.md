# Database Migrations with Alembic in FastAPI

## Concept

Alembic is SQLAlchemy's migration tool. It tracks schema changes as versioned Python scripts (`upgrade()` / `downgrade()`), maintains a `alembic_version` table in the database, and applies migrations in order.

**The FastAPI-specific complexity:** FastAPI uses `async` SQLAlchemy. Alembic was designed for sync SQLAlchemy. The `env.py` configuration must be adapted to run async migrations — this is where most teams trip up on first setup.

**Migration anatomy:**

```
alembic/
  env.py              ← connection setup, runs migrations
  script.py.mako      ← template for new migration files
  versions/
    001_create_users.py
    002_add_email_index.py
alembic.ini           ← config: sqlalchemy.url, script_location
```

Each version file:
```python
def upgrade() -> None:
    op.create_table("users", ...)  # forward

def downgrade() -> None:
    op.drop_table("users")         # backward
```

**When to run migrations:** never auto-run in the FastAPI lifespan. Run as a separate step in your CI/CD pipeline before deploying the new application version. Auto-running in lifespan means every worker process that starts attempts to migrate — with multiple Gunicorn workers, this races and can corrupt the migration state.

---

## Interview Questions

### Q1: How do you configure Alembic to work with an async SQLAlchemy engine?

**Model answer:**

Alembic's `env.py` runs migrations synchronously by default. For async SQLAlchemy, you need `asyncio.run()` wrapping the migration execution:

```python
# alembic/env.py
import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine

from myapp.models import Base      # your DeclarativeBase
from myapp.config import settings  # DATABASE_URL

config = context.config
fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Generate SQL without a live DB connection (for review/dry-run)."""
    url = settings.DATABASE_URL
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    """Run migrations against a live DB using async engine."""
    connectable = create_async_engine(settings.DATABASE_URL)
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
```

**Key detail:** `connection.run_sync(do_run_migrations)` converts the async connection to a sync interface that Alembic's migration context understands. Alembic internally calls `cursor.execute()` synchronously — `run_sync` provides a sync wrapper over the async connection that keeps the event loop running under the hood.

**Gotcha follow-up:** Why can't you just use `create_engine` (sync) for migrations even though your app uses `create_async_engine`?

You can — Alembic doesn't care whether you use the sync or async engine for migrations, since it calls `run_sync` anyway. Many teams use a sync `DATABASE_URL` (replacing `+asyncpg` with `+psycopg2`) in `env.py` to keep migration code simpler. The risk: if your `DATABASE_URL` construction has async-specific logic, you might silently connect to the wrong database. It's safer to share the same URL and use `run_sync`.

---

### Q2: What is the difference between `--autogenerate` and manually writing migrations, and what does autogenerate miss?

**Model answer:**

**`alembic revision --autogenerate -m "add users table"`** compares the current database schema (introspected via `SHOW COLUMNS`, `pg_catalog`, etc.) against your SQLAlchemy `metadata` and generates a migration script with the diff.

**What autogenerate detects:**
- Table creation and deletion
- Column addition, removal, type changes
- `NOT NULL` → `nullable` and vice versa
- Index creation and removal
- Unique constraint changes
- Foreign key changes (with some limitations)

**What autogenerate MISSES — these require manual migration code:**

1. **Data migrations** — moving data between columns, backfilling values, transforming existing rows. Autogenerate only looks at schema, not data.

2. **`server_default` vs `default`** — `Column(default=func.now())` is a Python-side default (applied by SQLAlchemy, not the DB). `Column(server_default=func.now())` is a DB-side default. Autogenerate can miss server_default changes or confuse the two.

3. **PostgreSQL-specific features** — partitioned tables, custom types, `ENUM` type changes, sequences, `GENERATED ALWAYS AS` columns, `INCLUDE` in indexes. Alembic has basic support but may not detect changes.

4. **Index renaming** — Alembic sees "old index gone, new index added" — generates a drop + create rather than an `ALTER INDEX RENAME TO`. For large tables this rebuilds the index unnecessarily.

5. **Check constraints on existing columns** — partially detected; complex `CHECK` expressions may not compare correctly.

6. **Views, stored procedures, triggers** — not tracked by autogenerate at all.

**Production workflow:** always review the generated migration before applying. Treat autogenerate output as a starting draft, not a finished migration.

---

### Q3: How do you perform a zero-downtime migration for adding a NOT NULL column to a large table?

**Model answer:**

Adding `NOT NULL` directly to a large table takes an `ACCESS EXCLUSIVE` lock on the entire table for the duration of the table rewrite. For a 100M row table, this can lock out reads and writes for minutes — unacceptable in production.

**Zero-downtime pattern (3 migrations, 2 deployments):**

**Migration 1 (before deployment):** Add column as `nullable`, no lock duration issue:
```python
def upgrade():
    op.add_column("orders", sa.Column("region", sa.String, nullable=True))
```

**Deployment 1:** deploy application code that writes `region` on all new orders and reads it (with a fallback for `None`).

**Migration 2 (backfill, offline or as background job):** backfill existing rows in batches:
```python
def upgrade():
    op.execute("""
        UPDATE orders SET region = 'us-east-1'
        WHERE region IS NULL AND id BETWEEN :start AND :end
    """)
    # In practice: run as a separate script with pagination to avoid one giant transaction
```

**Migration 3 (after all rows backfilled):** apply `NOT NULL` constraint using PostgreSQL's `VALIDATE CONSTRAINT` pattern — avoids full table lock:
```python
def upgrade():
    # Add CHECK constraint without validation (fast, no lock)
    op.execute("""
        ALTER TABLE orders
        ADD CONSTRAINT orders_region_not_null
        CHECK (region IS NOT NULL)
        NOT VALID
    """)
    # Validate in background (SHARE UPDATE EXCLUSIVE lock, allows reads/writes)
    op.execute("ALTER TABLE orders VALIDATE CONSTRAINT orders_region_not_null")
    # Then set NOT NULL (fast — constraint already proven, just metadata update)
    op.alter_column("orders", "region", nullable=False)
    op.drop_constraint("orders_region_not_null", "orders")
```

**Deployment 2:** deploy application code that no longer handles `None` for `region`.

This 3-migration, 2-deployment pattern is the standard zero-downtime approach used at companies operating PostgreSQL at scale.

---

### Q4: How should Alembic migrations be run in a containerized FastAPI deployment?

**Model answer:**

**Never in the application lifespan.** Common mistake:

```python
# WRONG — runs in every worker on startup
@asynccontextmanager
async def lifespan(app: FastAPI):
    await run_migrations()  # race condition with other workers
    yield
```

With 4 Gunicorn workers all starting simultaneously, all 4 attempt to apply pending migrations concurrently. Alembic uses an `alembic_version` table (not row-level locking) — concurrent runs can produce duplicate migration application or `IntegrityError` on the version insert.

**Correct patterns:**

**1. Kubernetes Init Container (canonical):**
```yaml
initContainers:
  - name: run-migrations
    image: myapp:latest
    command: ["alembic", "upgrade", "head"]
    env:
      - name: DATABASE_URL
        valueFrom: { secretKeyRef: { name: db-secret, key: url } }
```
The init container runs to completion before the app container starts. Exactly one migration run per deployment.

**2. Docker Compose / local dev:**
```yaml
services:
  migrate:
    image: myapp:latest
    command: alembic upgrade head
    depends_on: [postgres]
  api:
    image: myapp:latest
    command: uvicorn myapp.main:app
    depends_on:
      migrate:
        condition: service_completed_successfully
```

**3. CI/CD pipeline step:**
```bash
# GitHub Actions / GitLab CI
- name: Run migrations
  run: alembic upgrade head
  env:
    DATABASE_URL: ${{ secrets.DATABASE_URL }}
- name: Deploy application
  run: kubectl rollout restart deployment/myapp
```

**4. Startup script with check (if init container isn't available):**
```bash
#!/bin/bash
alembic upgrade head          # idempotent — no-op if already up to date
exec uvicorn myapp.main:app   # replace shell with uvicorn (proper signal handling)
```

The `exec` is critical — without it, uvicorn is a subprocess of bash. SIGTERM from Kubernetes goes to bash, not uvicorn, breaking graceful shutdown.

---

### Q5: How do you write a data migration safely — one that transforms existing rows while the application is live?

**Model answer:**

Data migrations must handle:
- Large tables (can't lock for the full rewrite)
- Application reads/writes continuing during migration
- Rollback if something goes wrong

```python
# versions/003_backfill_full_name.py
"""Backfill full_name from first_name + last_name."""
from alembic import op
import sqlalchemy as sa

BATCH_SIZE = 1000


def upgrade() -> None:
    # Add the new column first (as nullable — separate migration ideally)
    op.add_column("users", sa.Column("full_name", sa.String, nullable=True))

    conn = op.get_bind()

    # Process in batches to avoid one massive transaction + table lock
    offset = 0
    while True:
        result = conn.execute(
            sa.text("""
                UPDATE users
                SET full_name = first_name || ' ' || last_name
                WHERE full_name IS NULL
                  AND id IN (
                      SELECT id FROM users
                      WHERE full_name IS NULL
                      ORDER BY id
                      LIMIT :limit OFFSET :offset
                  )
                RETURNING id
            """),
            {"limit": BATCH_SIZE, "offset": offset},
        )
        rows_updated = result.rowcount
        if rows_updated == 0:
            break
        offset += rows_updated

    # After all rows backfilled, set NOT NULL in a separate migration (migration 004)


def downgrade() -> None:
    op.drop_column("users", "full_name")
```

**Why batch?** A single `UPDATE users SET full_name = ...` on 10M rows holds an `ACCESS EXCLUSIVE`-level row lock on every updated row simultaneously. Other transactions waiting to update any of those rows are blocked for the duration. Batching keeps individual transactions small, reducing lock contention.

**Why `WHERE full_name IS NULL` as the loop condition?** It's idempotent — if the migration is interrupted and re-run, it resumes from where it left off rather than re-processing completed rows.

**Production consideration:** run large data migrations as a separate offline job (not in an Alembic migration file) and track completion in a separate `migration_status` table. Alembic migration files should complete in seconds; long-running backfills belong in a script that can be monitored, paused, and resumed independently.

---

## Code: Complete Alembic Setup for Async FastAPI

```
myapp/
├── alembic.ini
├── alembic/
│   ├── env.py
│   ├── script.py.mako
│   └── versions/
│       └── 001_initial_schema.py
├── myapp/
│   ├── models.py
│   ├── config.py
│   └── main.py
```

```ini
# alembic.ini
[alembic]
script_location = alembic
# DATABASE_URL injected at runtime via env var — not hardcoded here
sqlalchemy.url =

[loggers]
keys = root,sqlalchemy,alembic

[handlers]
keys = console

[formatters]
keys = generic

[logger_root]
level = WARN
handlers = console

[logger_sqlalchemy]
level = WARN
handlers =

[logger_alembic]
level = INFO
handlers =

[handler_console]
class = StreamHandler
args = (sys.stderr,)
level = NOTSET
formatter = generic

[formatter_generic]
format = %(levelname)-5.5s [%(name)s] %(message)s
```

```python
# alembic/env.py
import asyncio
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine

from myapp.models import Base

config = context.config
if config.config_file_name:
    fileConfig(config.config_file_name)

# Override URL from environment — never hardcode credentials
DATABASE_URL = os.environ["DATABASE_URL"]
config.set_main_option("sqlalchemy.url", DATABASE_URL)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=DATABASE_URL,
        target_metadata=target_metadata,
        literal_binds=True,
        compare_type=True,       # detect column type changes
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    engine = create_async_engine(DATABASE_URL, echo=False)
    async with engine.connect() as conn:
        await conn.run_sync(do_run_migrations)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
```

```python
# alembic/versions/001_initial_schema.py
"""Initial schema."""
from alembic import op
import sqlalchemy as sa

revision = "001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("email", sa.String(255), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
    )
    # Create index CONCURRENTLY to avoid locking — use op.execute for PG-specific DDL
    op.execute(
        "CREATE UNIQUE INDEX CONCURRENTLY uq_users_email ON users (email)"
    )


def downgrade() -> None:
    op.drop_table("users")
```

```python
# myapp/models.py
from sqlalchemy import String, DateTime, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from datetime import datetime


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
```

```bash
# Common Alembic commands
alembic revision --autogenerate -m "add orders table"  # generate migration
alembic upgrade head          # apply all pending migrations
alembic downgrade -1          # roll back one migration
alembic current               # show current revision in DB
alembic history --verbose     # show all revisions
alembic show head             # show latest revision
alembic upgrade head --sql    # print SQL without executing (dry run)
```

---

## Under the Hood

**`alembic_version` table:** Alembic creates a single-row table `alembic_version(version_num VARCHAR)` in the target schema. `upgrade head` reads this row, finds pending migrations by walking the revision chain from `current → head`, and applies each. Between applying migrations, it updates this row. There is no distributed lock — concurrent `alembic upgrade head` calls race on the `UPDATE alembic_version SET version_num = :new` statement. PostgreSQL's row-level locking means one wins; the other sees the updated version and considers the migration already applied. This is usually safe but not guaranteed across all edge cases — use an init container or pre-hook to ensure exactly one runner.

**`connection.run_sync(fn)`** in SQLAlchemy async: this runs `fn(sync_connection)` in the same thread as the event loop, but using a synchronous proxy object that translates each `cursor.execute()` call into `await async_connection.execute()` using the running event loop. It's not a thread — it's a synchronous facade over the async connection that temporarily resumes the event loop for each DB call. This is why `asyncio.run(run_migrations_online())` is necessary — `run_sync` needs an active event loop to dispatch through.

**`CREATE INDEX CONCURRENTLY`** cannot run inside a transaction block. Alembic wraps each migration in a transaction by default. To use `CONCURRENTLY`, you must either run outside the transaction (`op.execute` with explicit connection) or configure the migration to run outside Alembic's default transaction with `with op.get_context().autocommit_block():` in newer Alembic versions.
