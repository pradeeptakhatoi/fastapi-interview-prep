# Overriding Dependencies — app.dependency_overrides for Testing

## Concept

`app.dependency_overrides` is a dict that maps a dependency callable to a replacement callable. When FastAPI resolves dependencies, it checks this dict first. If the original callable is present as a key, the replacement is used instead.

This is the primary mechanism for testing: you swap real implementations (DB sessions, auth, external APIs) with fakes or mocks without changing the application code.

The override applies globally to the app instance. In tests, always clean up: set `app.dependency_overrides = {}` (or remove specific keys) after the test, or use a fixture with teardown.

The replacement callable follows the same rules as the original: it can be a function, class, `yield` generator, etc. FastAPI doesn't enforce that the replacement returns the same type — it's your responsibility to ensure type compatibility.

---

## Interview Questions

### Q1: How does `app.dependency_overrides` work under the hood?

**Model answer:**

At request time, when `solve_dependencies()` is building the resolved value for a dependency, it checks `app.dependency_overrides` before calling the original callable:

```python
# Simplified from fastapi/dependencies/utils.py
dependency = field.field_info.dependency
# Check if there's an override
actual_dependency = app.dependency_overrides.get(dependency, dependency)
# Call actual_dependency instead of dependency
```

The lookup is by callable identity (same object in memory). This means:
- The override key must be the exact same object you used in `Depends()` in the route definition
- Lambda functions defined inline won't match (each definition creates a new object)
- Class instances will match if the same instance was used in `Depends()`

The override is transitive: if the overridden dependency has sub-dependencies, those sub-dependencies are also resolved normally (or overridden if they too have entries in `dependency_overrides`).

---

### Q2: Show the canonical pattern for overriding the DB session in a pytest test suite.

**Model answer:**

```python
# conftest.py
import pytest
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from myapp.main import app
from myapp.dependencies import get_db
from myapp.models import Base

TEST_DATABASE_URL = "sqlite+aiosqlite:///./test.db"
test_engine = create_async_engine(TEST_DATABASE_URL)
TestSessionLocal = async_sessionmaker(test_engine, expire_on_commit=False)


@pytest.fixture(scope="session", autouse=True)
async def create_tables():
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture
async def db_session():
    async with TestSessionLocal() as session:
        yield session
        await session.rollback()  # roll back after each test for isolation


@pytest.fixture
async def client(db_session: AsyncSession):
    async def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as ac:
        yield ac
    app.dependency_overrides.clear()
```

Key design choices:
- `db_session` fixture rolls back after each test → test isolation without dropping/recreating tables
- The override wraps the same session the fixture controls → test can inspect DB state directly
- `app.dependency_overrides.clear()` is in teardown → no cross-test pollution

**Gotcha follow-up:** If you call `app.dependency_overrides.clear()` and a test is running concurrently (pytest-asyncio in parallel mode), what breaks?

Everything. `dependency_overrides` is a global dict on the app instance. Concurrent tests mutating it will step on each other. For parallel tests, you need either separate app instances per test (expensive) or a mechanism like `httpx` + `TestClient` with dependency injection via `scope` fixtures that don't share state. The common solution is to keep test parallelism off for integration tests that use dependency overrides.

---

### Q3: How do you override a dependency that's several levels deep in the dependency tree?

**Model answer:**

You only need to override the leaf dependency, not intermediate ones. Because `solve_dependencies()` applies overrides at every level of the tree, overriding `get_db` also affects `get_current_user` (which depends on `get_db`), without needing to override `get_current_user` too.

```python
# Dependency tree:
# route → get_current_user → get_db
#       → get_db (direct)

# Override only get_db — both the route's direct dep AND get_current_user's dep get the override
app.dependency_overrides[get_db] = lambda: fake_db_session
```

If you need to override `get_current_user` directly (skipping auth entirely):

```python
app.dependency_overrides[get_current_user] = lambda: User(id=1, name="Test User")
```

This is a higher-level override — `get_db` is no longer called at all for that path (since `get_current_user`'s sub-dependencies are also skipped when the override is a simple function, not a `yield` generator with its own `Depends`).

---

### Q4: Can you use `dependency_overrides` in production to change behavior at runtime?

**Model answer:**

Technically yes — it's just a dict mutation. But it's a bad idea:

1. **Race conditions**: the dict is read during every request. Mutating it mid-flight could cause a request to see a partial override state.
2. **No scoping**: overrides apply globally to all requests, not just specific ones.
3. **Not designed for this**: the design intent is test-time configuration, not runtime dispatch.

For production runtime behavior changes, use proper mechanisms:
- **Feature flags**: pass a feature flag service as a dependency
- **Strategy pattern**: the dependency itself reads config to decide which implementation to use
- **A/B routing**: route-level branching, not dependency-level

---

## Code: Full Test Suite with Multiple Override Patterns

```python
# tests/test_users.py
import pytest
from httpx import AsyncClient, ASGITransport
from unittest.mock import AsyncMock

from myapp.main import app
from myapp.dependencies import get_db, get_current_user
from myapp.schemas import User


# Pattern 1: Override auth to inject a fixed user
@pytest.fixture
def authenticated_client():
    fake_user = User(id=42, name="Test User", email="test@example.com")
    app.dependency_overrides[get_current_user] = lambda: fake_user
    
    with TestClient(app) as client:
        yield client
    
    del app.dependency_overrides[get_current_user]


# Pattern 2: Override DB with async mock
@pytest.fixture
async def mock_db_client():
    mock_session = AsyncMock()
    mock_session.get.return_value = None  # simulate "not found"
    
    async def override_db():
        yield mock_session
    
    app.dependency_overrides[get_db] = override_db
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client, mock_session
    
    app.dependency_overrides.clear()


# Pattern 3: Override a class-based dependency
class FakeRateLimiter:
    async def __call__(self) -> None:
        pass  # never rate-limits in tests

@pytest.fixture
def no_rate_limit_client():
    from myapp.dependencies import RateLimiter
    app.dependency_overrides[RateLimiter()] = FakeRateLimiter()
    # NOTE: this only works if the same RateLimiter() INSTANCE was used in Depends()
    # Class-based deps are better overridden at the class level
    ...


# Actual tests
@pytest.mark.asyncio
async def test_get_user_not_found(mock_db_client):
    client, mock_session = mock_db_client
    response = await client.get("/users/99")
    assert response.status_code == 404
    mock_session.get.assert_called_once()


@pytest.mark.asyncio
async def test_get_user_requires_auth():
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/users/me")
    assert response.status_code == 401  # no override — real auth runs
```

---

## Under the Hood

`app.dependency_overrides` is read in `fastapi/dependencies/utils.py:get_dependant()` — specifically in the per-request path when resolving each dependency:

```python
# In solve_dependencies:
dependency = field.default.dependency
override = request.app.dependency_overrides.get(dependency)
actual = override if override is not None else dependency
```

`request.app` is the FastAPI application accessible from the `Request` object. This is why the dict must be mutated on the exact `app` instance that's handling the request — not a parent app or sub-app. If you mount sub-applications, each has its own `dependency_overrides`.
