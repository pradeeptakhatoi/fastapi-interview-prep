# Multi-process State, OpenAPI Schema Generation, and Lifespan Edge Cases

## Concept

**Multi-process state sharing:**

Gunicorn + UvicornWorker spawns N worker processes. Each process has its own Python interpreter, memory space, and `app.state`. There is **no shared memory** between workers without an external mechanism. `app.state.db_pool` initialized in one worker is not accessible in another.

**OpenAPI schema generation:**

FastAPI generates the OpenAPI schema lazily — on the first request to `/openapi.json`. It's then cached in `app.openapi_schema`. The generation involves:
1. Iterating all registered routes
2. Calling `get_openapi()` with the app's route list
3. Generating JSON Schema for each request/response model via Pydantic's `model_json_schema()`
4. Caching the result in `app.openapi_schema`

**Lifespan edge cases:**

The `lifespan` context manager runs during the ASGI `lifespan` protocol. Failures here (exceptions before `yield`) send `lifespan.startup.failed` to the ASGI server. Uvicorn's behavior: it exits with a non-zero status code. Gunicorn's behavior: it retries the worker, potentially in a crash loop.

---

## Interview Questions

### Q1: You initialize a connection pool in `app.state` during lifespan. Under Gunicorn + UvicornWorker with 4 workers, how many connection pools exist? What's the implication for connection pool sizing?

**Model answer:**

**4 connection pools** — one per worker process. `app.state` is per-process; there's no inter-process sharing.

**Pool sizing implication:** if each pool allows 10 connections and you have 4 workers, your database can see up to 40 connections from a single application server. With multiple application servers, multiply further.

The formula for max database connections:
```
max_db_connections = workers_per_server × servers × pool_size_per_worker
```

For `pgbouncer` or databases with connection limits (RDS default 100, Postgres default determined by `max_connections`), this arithmetic must be explicit. A common production bug: setting `pool_size=10` per worker, deploying 8 workers × 3 servers = 240 connections, exceeding Postgres's `max_connections`.

The fix: use a connection pooler (PgBouncer) in transaction pooling mode, or reduce `pool_size` per worker accounting for the total.

```python
import os
from contextlib import asynccontextmanager
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import create_async_engine

# Compute pool size accounting for number of workers
# (workers passed via env var from Gunicorn config)
WORKERS = int(os.getenv("GUNICORN_WORKERS", 4))
MAX_DB_CONNECTIONS = 40  # total across all workers
POOL_SIZE_PER_WORKER = max(1, MAX_DB_CONNECTIONS // WORKERS)

@asynccontextmanager
async def lifespan(app: FastAPI):
    engine = create_async_engine(
        DATABASE_URL,
        pool_size=POOL_SIZE_PER_WORKER,
        max_overflow=2,
    )
    app.state.engine = engine
    yield
    await engine.dispose()
```

---

### Q2: How does FastAPI generate and cache the OpenAPI schema, and how do you override it programmatically?

**Model answer:**

FastAPI's `openapi()` method (in `fastapi/applications.py`):

```python
def openapi(self) -> dict:
    if not self.openapi_schema:
        self.openapi_schema = get_openapi(
            title=self.title,
            version=self.version,
            openapi_version=self.openapi_version,
            description=self.description,
            routes=self.routes,
        )
    return self.openapi_schema
```

It's cached after the first call. `get_openapi()` in `fastapi/openapi/utils.py` iterates routes, builds path items, and calls Pydantic's `model_json_schema()` for each model. The entire schema is held in memory as a Python dict.

**Override pattern:**

```python
from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi

app = FastAPI()

# ... route definitions ...

def custom_openapi():
    if app.openapi_schema:
        return app.openapi_schema
    
    schema = get_openapi(
        title="My API",
        version="2.0.0",
        description="Custom description",
        routes=app.routes,
    )
    
    # Add custom security schemes
    schema["components"]["securitySchemes"] = {
        "ApiKeyAuth": {"type": "apiKey", "in": "header", "name": "X-API-Key"},
        "BearerAuth": {"type": "http", "scheme": "bearer", "bearerFormat": "JWT"},
    }
    
    # Add global security requirement
    schema["security"] = [{"BearerAuth": []}]
    
    # Remove specific endpoints from docs
    paths_to_hide = {"/health", "/metrics"}
    for path in paths_to_hide:
        schema["paths"].pop(path, None)
    
    app.openapi_schema = schema
    return app.openapi_schema

app.openapi = custom_openapi
```

**Forcing schema regeneration:** set `app.openapi_schema = None` to clear the cache. This is useful in tests or during dynamic route registration.

**Gotcha follow-up:** What happens if you add routes to the app after the schema has been generated?

The schema is cached and won't include the new routes. You must set `app.openapi_schema = None` to force regeneration. In production, routes are always added before startup, so this only matters in tests that dynamically add routes.

---

### Q3: Describe three distinct ways a lifespan startup can fail silently and leave the app in a broken state.

**Model answer:**

**1. Exception caught and swallowed before yield:**

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        db_pool = await connect_to_db()
    except Exception as e:
        logger.error(f"DB connection failed: {e}")
        # No re-raise → app starts up without a DB pool
        # app.state.db doesn't exist → AttributeError at request time
    else:
        app.state.db = db_pool
    yield
    # teardown
```

**2. Timeout not enforced on startup I/O:**

If `connect_to_db()` hangs (e.g., waiting for a DB that's not up yet), the lifespan coroutine hangs indefinitely. Uvicorn/Gunicorn have startup timeouts, but if the timeout is generous or absent, the server may appear to start but routes hang waiting for a pool that never initialized.

**3. Initialization in the wrong scope:**

```python
# Module-level initialization (runs at import time, before lifespan)
db_pool = asyncio.get_event_loop().run_until_complete(create_pool())
# This creates a pool on a potentially wrong event loop,
# then the ASGI server starts its OWN event loop.
# The pool is attached to the old loop and all operations deadlock.
```

The fix: always initialize async resources inside `lifespan`, never at module level or in `__init__`.

---

### Q4: What happens during graceful shutdown when a request is in-flight?

**Model answer:**

On SIGTERM, Uvicorn/Gunicorn sends `lifespan.shutdown` to the ASGI app and stops accepting new connections. In-flight requests have a grace period (`--timeout-graceful-shutdown` in Uvicorn, default 0 seconds — meaning immediate).

The shutdown sequence:
1. ASGI server receives SIGTERM
2. Stops accepting new connections
3. Sends `lifespan.shutdown` event
4. Waits for in-flight requests to complete (grace period)
5. After grace period, kills remaining requests
6. FastAPI's `lifespan` context manager exits the `yield` — teardown runs

**Implications:**
- Long-running requests (file processing, heavy computation) are cut off if they exceed the grace period
- DB transactions in flight are rolled back (connection pool teardown sends `async with session` out of scope → rollback)
- If teardown itself takes too long (e.g., `await pool.dispose()` waits for all connections to close), it may be killed by the SIGKILL that follows SIGTERM after the OS timeout

**Best practice:**
```python
import asyncio
from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app: FastAPI):
    pool = await create_pool()
    app.state.pool = pool
    yield
    # Graceful teardown with timeout
    try:
        await asyncio.wait_for(pool.close(), timeout=5.0)
    except asyncio.TimeoutError:
        pool.terminate()  # force close remaining connections
```

---

## Code: Multi-process Safe State Management

```python
import os
import asyncio
from contextlib import asynccontextmanager
from typing import AsyncGenerator
from fastapi import FastAPI, Depends
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine


# Per-process state (not shared across Gunicorn workers)
_engine = None
_session_factory = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _engine, _session_factory

    workers = int(os.getenv("GUNICORN_WORKERS", "1"))
    db_pool_size = max(2, 20 // workers)  # total 20 connections across workers

    _engine = create_async_engine(
        os.environ["DATABASE_URL"],
        pool_size=db_pool_size,
        max_overflow=db_pool_size // 2,
        pool_pre_ping=True,
        pool_recycle=3600,  # recycle connections after 1 hour
    )
    _session_factory = async_sessionmaker(_engine, expire_on_commit=False)

    # Store on app.state for access from dependencies
    app.state.engine = _engine
    app.state.session_factory = _session_factory

    try:
        # Verify connectivity at startup
        async with _engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception:
        # Re-raise to send lifespan.startup.failed
        raise

    yield

    # Teardown: wait for active connections to close
    try:
        await asyncio.wait_for(_engine.dispose(), timeout=10.0)
    except asyncio.TimeoutError:
        pass  # Already shutting down; connections will be closed by OS


app = FastAPI(lifespan=lifespan)


# Dependency that reads from app.state (safe: written once at startup)
async def get_db(request: Request) -> AsyncGenerator[AsyncSession, None]:
    factory = request.app.state.session_factory
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


@app.get("/health")
async def health(request: Request):
    # Verify engine from app.state (same object as initialized in lifespan)
    engine = request.app.state.engine
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))
    return {"status": "ok", "worker_pid": os.getpid()}
```

---

## Under the Hood

**Multi-process state:** Python's GIL is per-process. Gunicorn's `--preload` flag loads the application before forking. This means the Python module (including class definitions and module-level constants) is shared via copy-on-write OS memory. But *async resources* (DB pools, event loop objects) cannot be shared — the event loop is per-process after fork, and any async handles are invalid in child processes. Always initialize async resources inside `lifespan`, after the fork.

**OpenAPI schema cache:** `app.openapi_schema` is an instance variable on the `FastAPI` object. In a multi-worker setup, each worker generates and caches its own schema independently. This is generally fine (schemas are identical), but means the first `/openapi.json` request to each worker triggers schema generation. Under `--preload`, schema generation happens pre-fork if any worker requests the schema during startup — it would be shared via copy-on-write, saving CPU on each worker's first request.
