# FastAPI Testing with pytest and Fixtures

## Concept

pytest fixtures are the backbone of any well-structured FastAPI test suite. They provide composable, reusable setup/teardown that integrates with both sync and async test code. The challenge in FastAPI projects is that your application is fundamentally async — databases, HTTP clients, and lifespan events all run on an event loop — and pytest was designed for sync code. Getting fixture scopes, event loops, and async teardown right separates a test suite that scales from one that accumulates subtle isolation bugs.

**The core tension:** pytest runs synchronously; FastAPI apps run async. Every async fixture or async test needs an event loop. How that loop is created, shared, and closed across fixture scopes determines whether your tests are isolated, fast, and correct.

**Key libraries:**
- `pytest-asyncio` — the standard choice; provides `@pytest.mark.asyncio` and `async` fixture support
- `anyio` + `pytest-anyio` — backend-agnostic; required if your app uses `anyio` directly (Starlette does)
- `httpx` — async HTTP client used with `AsyncClient` for FastAPI tests
- `pytest-postgresql` / `testing.postgresql` — spins up real PostgreSQL for integration tests

**Fixture scopes:** `function` (default) → `class` → `module` → `package` → `session`. Scope controls how many times a fixture is created. An async fixture can only be used by a fixture of the same or narrower scope.

---

## Interview Questions

### Q1: How do you structure pytest fixtures for a FastAPI application that uses an async SQLAlchemy database session?

**Model answer:**

The pattern has three layers: (1) a session-scoped engine fixture, (2) a function-scoped transaction fixture that rolls back after each test, and (3) a function-scoped session fixture that runs inside that transaction.

```python
# conftest.py
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    AsyncEngine,
    create_async_engine,
    async_sessionmaker,
)
from sqlalchemy.pool import NullPool

from myapp.models import Base
from myapp.dependencies import get_db
from myapp.main import app

DATABASE_URL = "postgresql+asyncpg://user:pass@localhost/test_db"


@pytest_asyncio.fixture(scope="session")
async def engine() -> AsyncEngine:
    """One engine for the whole test session — expensive to create."""
    engine = create_async_engine(DATABASE_URL, poolclass=NullPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest_asyncio.fixture
async def db_session(engine: AsyncEngine) -> AsyncSession:
    """
    Per-test session that rolls back at the end.
    Uses a nested transaction (SAVEPOINT) so each test sees a clean slate.
    """
    async with engine.connect() as conn:
        await conn.begin()
        # Bind a session to this connection (not creating its own)
        session_factory = async_sessionmaker(
            bind=conn, expire_on_commit=False, class_=AsyncSession
        )
        session = session_factory()
        # Nested transaction = SAVEPOINT — rolls back to here after test
        await session.begin_nested()

        yield session

        await session.rollback()
        await conn.rollback()


@pytest_asyncio.fixture
async def client(db_session: AsyncSession):
    """HTTP client wired to use the test DB session."""
    from httpx import AsyncClient, ASGITransport

    async def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        yield ac
    app.dependency_overrides.clear()
```

**Why `NullPool`?** The session-scoped engine hands connections to function-scoped fixtures. SQLAlchemy's default connection pool (`QueuePool`) tracks connections by thread/task. In tests, multiple async tasks may share connections in unexpected ways. `NullPool` disables pooling entirely — each `engine.connect()` opens a fresh connection, preventing cross-test contamination.

**Why `begin_nested()`?** This issues a `SAVEPOINT` in PostgreSQL. The test runs inside the savepoint. Rollback after the test goes back to the savepoint, not the outer transaction. The outer transaction (opened with `conn.begin()`) is also rolled back, so the DB stays pristine across tests without truncating tables.

**Gotcha follow-up:** What breaks if you use `session.commit()` inside a test that uses this rollback fixture?

`session.commit()` releases the savepoint. The subsequent `session.rollback()` in the fixture teardown rolls back to the outer transaction start — but any data committed inside the savepoint is already materialized to the outer transaction. You'll see cross-test pollution. Solutions: (1) use `session.flush()` in tests instead of `commit()` where possible, or (2) truncate tables in teardown instead of relying on savepoint rollback.

---

### Q2: What is the `asyncio_mode` configuration in pytest-asyncio, and why do most FastAPI projects set it to `"auto"`?

**Model answer:**

`pytest-asyncio` has three modes, configured in `pytest.ini` or `pyproject.toml`:

```toml
[tool.pytest.ini_options]
asyncio_mode = "auto"
```

**`strict` (default before 0.21):** every async test and async fixture must be explicitly marked with `@pytest.mark.asyncio` or `@pytest_asyncio.fixture`. Easy to forget; leads to async tests silently being collected but not awaited (they pass without running).

**`auto`:** all `async def` test functions and `async def` fixtures are automatically treated as async. No per-test decoration needed. This is the idiomatic choice for FastAPI projects where almost every test is async.

**`loose`:** async tests run automatically; async fixtures still need `@pytest_asyncio.fixture`. A middle ground rarely chosen.

**The silent failure risk in strict mode:**

```python
# strict mode — THIS TEST ALWAYS PASSES, NEVER RUNS
async def test_user_creation():  # missing @pytest.mark.asyncio
    response = await client.post("/users", json={"name": "Alice"})
    assert response.status_code == 201  # never evaluated
```

pytest collects this as a sync test that returns a coroutine object. A coroutine object is truthy — the test "passes" with zero assertions executed. `asyncio_mode = "auto"` prevents this class of bug entirely.

**Event loop scope (pytest-asyncio 0.21+):**

```toml
[tool.pytest.ini_options]
asyncio_mode = "auto"
asyncio_default_fixture_loop_scope = "session"
```

Without this, each test function gets its own event loop. A session-scoped async fixture creates its event loop once at session start — if a function-scoped test uses a different loop, you get `RuntimeError: Task attached to a different loop`. Setting `asyncio_default_fixture_loop_scope = "session"` shares one loop for the full session, which is required when you have session-scoped async fixtures (like the `engine` fixture above).

---

### Q3: How do you test a FastAPI endpoint that triggers a background task, and how do you assert the task completed?

**Model answer:**

`BackgroundTasks` run after the response is sent, inside the same ASGI call. With `AsyncClient` + `ASGITransport`, the entire ASGI call (including background tasks) completes before `await client.post(...)` returns. This means you can assert background task side effects directly after the request.

```python
# endpoint
from fastapi import BackgroundTasks

def send_welcome_email(user_id: int) -> None:
    # sync function — runs in threadpool
    email_service.send(user_id=user_id, template="welcome")

@app.post("/users", status_code=201)
async def create_user(payload: UserCreate, tasks: BackgroundTasks, db: AsyncSession = Depends(get_db)):
    user = User(**payload.model_dump())
    db.add(user)
    await db.flush()
    tasks.add_task(send_welcome_email, user.id)
    return {"id": user.id}
```

```python
# test
from unittest.mock import patch, MagicMock

@pytest.mark.asyncio
async def test_create_user_sends_email(client):
    with patch("myapp.routers.users.email_service") as mock_email:
        mock_email.send = MagicMock()

        response = await client.post("/users", json={"name": "Alice", "email": "a@b.com"})

        assert response.status_code == 201
        # Background task has already run by this point
        mock_email.send.assert_called_once_with(
            user_id=response.json()["id"],
            template="welcome",
        )
```

**Important caveat:** this only works with `AsyncClient` + `ASGITransport`. With `TestClient` (Starlette's sync client), background tasks run in the same thread but after `response` is returned — the assertion above would race. With `httpx.AsyncClient` targeting a real running server (via `base_url="http://localhost:8000"`), the background task is in a separate process — you'd need polling or a queue-based assertion strategy.

**Gotcha follow-up:** What if the background task is async (`async def`) and raises an exception?

FastAPI catches exceptions in background tasks and logs them but does **not** propagate them to the caller — the HTTP response is already sent. In tests, a failing async background task will appear as a passing test with an ERROR log. To assert on exceptions in background tasks, either (1) mock the function and assert it wasn't called with bad args, (2) capture the log output via `caplog` fixture, or (3) redesign: push work to a real task queue (Celery/ARQ) and test the task function in isolation.

---

### Q4: How do you write a session-scoped fixture that starts the FastAPI lifespan exactly once for the entire test session?

**Model answer:**

`AsyncClient` with `ASGITransport` does **not** trigger lifespan by default. To run lifespan in tests, you must use `asgi_lifespan` from the `asgi-lifespan` package, or use `httpx`'s lifespan parameter:

```python
# Option 1: asgi-lifespan package
import pytest_asyncio
from asgi_lifespan import LifespanManager
from httpx import AsyncClient, ASGITransport

@pytest_asyncio.fixture(scope="session")
async def app_with_lifespan():
    """Start the app lifespan once for the whole test session."""
    async with LifespanManager(app) as manager:
        yield manager.app  # app with lifespan events executed


@pytest_asyncio.fixture(scope="session")
async def session_client(app_with_lifespan):
    async with AsyncClient(
        transport=ASGITransport(app=app_with_lifespan),
        base_url="http://test",
    ) as client:
        yield client
```

```python
# Option 2: httpx 0.27+ native lifespan support
@pytest_asyncio.fixture(scope="session")
async def session_client():
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        follow_redirects=True,
    ) as client:
        # httpx 0.27+: pass `lifespan=app` to trigger startup/shutdown
        yield client
```

**Why session scope for lifespan?** The lifespan creates expensive resources: connection pools, Redis clients, ML model loading. Creating and destroying these per test is slow. Session scope means they're created once, shared, and torn down after all tests run.

**The scope mismatch trap:** a session-scoped `session_client` fixture cannot use a function-scoped `db_session` fixture directly. If you need per-test DB isolation with a session-scoped client, you must pass the test DB session to the client via `dependency_overrides` — but `dependency_overrides` is a dict on the app (module-level state), which means concurrent tests will race. Solution: don't use session-scoped client for tests that need DB isolation; use function-scoped client with the rollback fixture instead.

---

### Q5: How do you use pytest parametrize with async fixtures to test multiple authentication scenarios?

**Model answer:**

`@pytest.mark.parametrize` works with async tests exactly like sync tests. For fixtures that need parametrize-like behavior, use `params` on the fixture itself:

```python
# Factory fixture — returns a callable that creates users with given roles
@pytest_asyncio.fixture
async def make_user(db_session: AsyncSession):
    created = []

    async def _make_user(role: str = "user", email: str | None = None) -> User:
        user = User(
            email=email or f"test_{role}_{len(created)}@example.com",
            role=role,
            hashed_password=hash_password("test-pass"),
        )
        db_session.add(user)
        await db_session.flush()
        created.append(user)
        return user

    return _make_user


@pytest_asyncio.fixture
async def auth_token(make_user):
    """Returns a callable: auth_token(role) → Bearer token string."""
    async def _token(role: str = "user") -> str:
        user = await make_user(role=role)
        return create_access_token({"sub": str(user.id), "role": role})
    return _token


# Parametrized test using factory fixtures
@pytest.mark.parametrize("role,expected_status", [
    ("user", 403),
    ("admin", 200),
    ("superadmin", 200),
])
async def test_admin_endpoint_authorization(client, auth_token, role, expected_status):
    token = await auth_token(role=role)
    response = await client.get(
        "/admin/users",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == expected_status


# Fixture-level parametrize — runs all dependent tests for each param
@pytest_asyncio.fixture(params=["postgresql+asyncpg", "sqlite+aiosqlite"])
async def multi_db_engine(request):
    """Test against both Postgres and SQLite to catch driver-specific bugs."""
    url = f"{request.param}://..." if "postgresql" in request.param else "sqlite+aiosqlite:///./test.db"
    engine = create_async_engine(url, poolclass=NullPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()
```

**Factory fixture pattern** (`make_user` returning a callable) is the FastAPI testing idiom for creating test objects. It's more flexible than parametrized fixtures when test logic needs to control the exact object shape. The factory captures `db_session` in its closure — all created objects are within the test's transaction and roll back automatically.

---

## Code: Complete Test Suite Structure

```python
# pyproject.toml
[tool.pytest.ini_options]
asyncio_mode = "auto"
asyncio_default_fixture_loop_scope = "session"
testpaths = ["tests"]
filterwarnings = [
    "error",                                   # fail on unhandled warnings
    "ignore::DeprecationWarning:sqlalchemy",   # SQLAlchemy internal warnings
]
```

```python
# tests/conftest.py
import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from myapp.dependencies import get_db, get_current_user
from myapp.main import app
from myapp.models import Base, User
from myapp.security import create_access_token, hash_password

TEST_DATABASE_URL = "postgresql+asyncpg://user:pass@localhost/test_myapp"


# ─── Database fixtures ───────────────────────────────────────────────────────

@pytest_asyncio.fixture(scope="session")
async def engine() -> AsyncEngine:
    engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest_asyncio.fixture
async def db(engine: AsyncEngine) -> AsyncSession:
    """Isolated per-test session via savepoint rollback."""
    async with engine.connect() as conn:
        txn = await conn.begin()
        session = async_sessionmaker(
            bind=conn, expire_on_commit=False, class_=AsyncSession
        )()
        await session.begin_nested()  # SAVEPOINT

        yield session

        await session.rollback()
        await txn.rollback()


# ─── App / HTTP client fixtures ───────────────────────────────────────────────

@pytest_asyncio.fixture
async def client(db: AsyncSession) -> AsyncClient:
    """Function-scoped client wired to the test DB session."""
    async def _get_db():
        yield db

    app.dependency_overrides[get_db] = _get_db
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as ac:
        yield ac
    app.dependency_overrides.pop(get_db, None)


# ─── Data factory fixtures ────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def make_user(db: AsyncSession):
    async def _factory(
        *,
        email: str = "user@example.com",
        role: str = "user",
        password: str = "testpassword",
    ) -> User:
        user = User(
            email=email,
            role=role,
            hashed_password=hash_password(password),
        )
        db.add(user)
        await db.flush()
        await db.refresh(user)
        return user

    return _factory


@pytest_asyncio.fixture
async def user(make_user) -> User:
    """Default unprivileged user."""
    return await make_user(email="user@example.com", role="user")


@pytest_asyncio.fixture
async def admin(make_user) -> User:
    """Admin user."""
    return await make_user(email="admin@example.com", role="admin")


# ─── Auth fixtures ────────────────────────────────────────────────────────────

@pytest.fixture
def user_token(user: User) -> str:
    return create_access_token({"sub": str(user.id), "role": user.role})


@pytest.fixture
def admin_token(admin: User) -> str:
    return create_access_token({"sub": str(admin.id), "role": admin.role})


@pytest.fixture
def user_headers(user_token: str) -> dict:
    return {"Authorization": f"Bearer {user_token}"}


@pytest.fixture
def admin_headers(admin_token: str) -> dict:
    return {"Authorization": f"Bearer {admin_token}"}


# ─── Shortcut: authenticated client ──────────────────────────────────────────

@pytest_asyncio.fixture
async def authed_client(client: AsyncClient, user: User) -> AsyncClient:
    """Client with user auth header pre-set."""
    token = create_access_token({"sub": str(user.id), "role": user.role})
    client.headers.update({"Authorization": f"Bearer {token}"})
    return client
```

```python
# tests/test_users.py
import pytest
from httpx import AsyncClient
from myapp.models import User


async def test_create_user_returns_201(client: AsyncClient) -> None:
    response = await client.post("/users", json={"email": "new@example.com", "name": "New"})
    assert response.status_code == 201
    assert response.json()["email"] == "new@example.com"


async def test_get_user_requires_auth(client: AsyncClient, user: User) -> None:
    response = await client.get(f"/users/{user.id}")
    assert response.status_code == 401


async def test_get_user_authenticated(authed_client: AsyncClient, user: User) -> None:
    response = await authed_client.get(f"/users/{user.id}")
    assert response.status_code == 200
    assert response.json()["id"] == user.id


@pytest.mark.parametrize("role,status", [
    ("user", 403),
    ("admin", 200),
])
async def test_admin_list_users_by_role(
    client: AsyncClient, make_user, role: str, status: int
) -> None:
    u = await make_user(email=f"{role}@example.com", role=role)
    token = create_access_token({"sub": str(u.id), "role": role})
    response = await client.get(
        "/admin/users",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == status


async def test_duplicate_email_returns_409(client: AsyncClient, user: User) -> None:
    response = await client.post(
        "/users", json={"email": user.email, "name": "Dupe"}
    )
    assert response.status_code == 409
```

```python
# tests/test_lifespan.py — testing startup/shutdown behavior
import pytest_asyncio
from asgi_lifespan import LifespanManager
from httpx import AsyncClient, ASGITransport
from myapp.main import app


@pytest_asyncio.fixture(scope="module")
async def started_app():
    """Verify lifespan runs without error."""
    async with LifespanManager(app) as manager:
        yield manager.app


async def test_health_check_after_lifespan(started_app) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=started_app),
        base_url="http://test",
    ) as client:
        response = await client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


async def test_app_state_populated_after_lifespan(started_app) -> None:
    # Verify lifespan stored resources on app.state
    assert hasattr(started_app.state, "redis")
    assert hasattr(started_app.state, "db_pool")
```

---

## Under the Hood

**Why `pytest_asyncio.fixture` vs `pytest.fixture` for async fixtures:** `pytest.fixture` doesn't know how to await coroutines. If you decorate an `async def` with `@pytest.fixture`, pytest calls it and gets back a coroutine object — which is truthy but never awaited. The fixture's body never runs; the "fixture value" is a coroutine object instead of your intended return value. `@pytest_asyncio.fixture` wraps the coroutine in the event loop managed by pytest-asyncio, awaiting it properly.

**Event loop lifecycle in `asyncio_mode = "auto"` with `asyncio_default_fixture_loop_scope = "session"`:** pytest-asyncio creates a single `asyncio.new_event_loop()` at session start and runs all async fixtures and tests on it. Teardown of session-scoped async fixtures happens in this same loop at session end, before the loop is closed. Without the session scope setting, each function-scoped test would create its own loop — fine for isolated tests, but incompatible with session-scoped async fixtures that were created on a different loop instance.

**`begin_nested()` and SAVEPOINTs:** SQLAlchemy translates `session.begin_nested()` to `SAVEPOINT sp1` in PostgreSQL. `session.rollback()` after a nested transaction issues `ROLLBACK TO SAVEPOINT sp1`, not `ROLLBACK` — the outer transaction is still active. The fixture's `conn.rollback()` issues the outer `ROLLBACK`, discarding all changes including those that ran after the savepoint. This is identical to wrapping each test in a transaction that is never committed — the database is always left in its pre-test state.
