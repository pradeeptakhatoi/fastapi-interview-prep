# TestClient (Sync) vs httpx.AsyncClient (Async) Testing

## Concept

**`fastapi.testclient.TestClient`** (wraps `starlette.testclient.TestClient`): a synchronous test client backed by `httpx`. It runs the ASGI app in a thread, allowing sync test code to call async routes. Under the hood, it starts an event loop in the thread, runs the ASGI app, and bridges the sync/async boundary.

**`httpx.AsyncClient` with `ASGITransport`**: the async alternative. Used with `pytest-asyncio` or `anyio`. The transport adapts `httpx` to call the ASGI app directly without network I/O. Necessary when your tests are themselves async and need to share the same event loop as the app (e.g., to share DB state).

| Feature | TestClient | AsyncClient |
|---------|-----------|-------------|
| Test syntax | Sync | Async (requires `pytest-asyncio`) |
| Event loop | Spawns its own | Shares test's event loop |
| WebSocket support | Yes | Yes (different API) |
| Lifespan events | Yes (via `with TestClient()`) | Yes |
| DB state sharing | Hard (different loop) | Easy (same loop) |
| Simpler setup | Yes | No |

---

## Interview Questions

### Q1: When should you use `TestClient` vs `httpx.AsyncClient`? What breaks if you choose wrong?

**Model answer:**

**Use `TestClient` when:**
- All route logic and dependencies are sync (no async I/O needed in tests)
- You have existing sync test infrastructure
- You don't need to share async resources (DB sessions, Redis connections) between tests and the app

**Use `AsyncClient` when:**
- You need to share an async DB session between the test and the application (for in-transaction rollback isolation)
- Your test code itself is async (using pytest-asyncio)
- You need to test WebSocket connections asynchronously
- You're testing streaming responses that require async iteration

**What breaks if you use `TestClient` for async-heavy tests:**

`TestClient` runs the ASGI app in a thread with its own event loop. If your test creates an `asyncio.Queue` or shares a ContextVar with the app, they won't be in the same event loop context. Trying to `await` inside a `TestClient` test (outside a separate `asyncio.run()`) will fail.

**What breaks if you use `AsyncClient` for simple tests:**

Nothing breaks technically, but the setup is heavier (requires `pytest-asyncio`, `asyncio` markers, potentially fixture scope changes). Over-engineering for tests that don't need async.

---

### Q2: How do you properly test lifespan events (startup/shutdown) with both client types?

**Model answer:**

**With `TestClient`:** use as a context manager — lifespan runs on `__enter__` and `__exit__`:

```python
from fastapi.testclient import TestClient
from myapp.main import app

def test_startup_initializes_db():
    with TestClient(app) as client:
        # Lifespan startup has run; app.state is initialized
        response = client.get("/health")
        assert response.status_code == 200
    # Lifespan shutdown has run here

# Without context manager: lifespan does NOT run
client = TestClient(app)  # no lifespan
response = client.get("/health")  # app.state may be uninitialized → AttributeError
```

**With `AsyncClient`:**

```python
import pytest
from httpx import AsyncClient, ASGITransport
from myapp.main import app

@pytest.mark.asyncio
async def test_startup():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # Lifespan startup runs during AsyncClient __aenter__
        response = await client.get("/health")
        assert response.status_code == 200
    # Lifespan shutdown runs during AsyncClient __aexit__
```

**Gotcha:** `ASGITransport(app=app)` does NOT automatically trigger lifespan. You must use `AsyncClient` as a context manager (`async with`) for lifespan to fire. Using `AsyncClient(transport=...)` without entering the context manager skips lifespan.

---

### Q3: How do you structure a test that needs both DB isolation and full lifespan?

**Model answer:**

The challenge: lifespan creates the real DB pool. Tests need their own session (for rollback isolation). Solution: override the `get_db` dependency while keeping the rest of lifespan intact.

```python
# conftest.py
import pytest
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from myapp.main import app
from myapp.dependencies import get_db
from myapp.models import Base

TEST_DB_URL = "sqlite+aiosqlite:///./test.db"
test_engine = create_async_engine(TEST_DB_URL)
TestSession = async_sessionmaker(test_engine, expire_on_commit=False)


@pytest.fixture(scope="session", autouse=True)
async def setup_db():
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture
async def db():
    async with TestSession() as session:
        yield session
        await session.rollback()  # isolation: each test starts clean


@pytest.fixture
async def client(db: AsyncSession):
    async def override_db():
        yield db

    app.dependency_overrides[get_db] = override_db
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as ac:
        yield ac
    app.dependency_overrides.clear()


# Test
@pytest.mark.asyncio
async def test_create_item(client: AsyncClient, db: AsyncSession):
    response = await client.post("/items/", json={"name": "Widget", "price": 9.99})
    assert response.status_code == 201

    # Directly inspect DB state — same session as the route used
    from myapp.models import Item
    items = (await db.execute(select(Item))).scalars().all()
    assert len(items) == 1
    assert items[0].name == "Widget"
```

---

## Code: Testing WebSockets and Streaming Responses

```python
# tests/test_websocket.py
import pytest
from fastapi.testclient import TestClient
from httpx import AsyncClient, ASGITransport
from myapp.main import app  # has ws route and SSE route


# WebSocket test with TestClient (sync)
def test_websocket_echo():
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as ws:
            ws.send_text("hello")
            data = ws.receive_text()
            assert data == "echo: hello"


# WebSocket test with AsyncClient (async)
@pytest.mark.asyncio
async def test_websocket_echo_async():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        async with client.websocket_connect("/ws") as ws:
            await ws.send_text("hello")
            data = await ws.receive_text()
            assert data == "echo: hello"


# Streaming response test
@pytest.mark.asyncio
async def test_streaming_response():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        async with client.stream("GET", "/stream") as response:
            assert response.status_code == 200
            chunks = []
            async for chunk in response.aiter_bytes():
                chunks.append(chunk)
            assert len(chunks) > 0


# Testing with dependency override + background task verification
from unittest.mock import AsyncMock, patch


@pytest.mark.asyncio
async def test_order_sends_email():
    mock_email = AsyncMock()

    with patch("myapp.services.send_email", mock_email):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post("/orders/", json={"product_id": 1, "qty": 2})
        # Background tasks ran before client closed (AsyncClient __aexit__ waits)

    mock_email.assert_called_once()
```

---

## Under the Hood

`TestClient` uses `starlette.testclient.TestClient` which wraps `httpx.Client`. Under the hood:
1. A new thread is created with `threading.Thread`
2. A new event loop is created in that thread: `asyncio.new_event_loop()`
3. The ASGI app is called with `scope/receive/send` — all within the thread's event loop
4. The thread synchronizes results back to the main test thread via `queue.Queue`

This is why `TestClient` cannot share async resources with the test — they're in different event loops on different threads.

`ASGITransport` in `httpx` is cleaner: it directly calls `await app(scope, receive, send)` using the current event loop. No threading, no queue bridges. The `httpx` request is converted to ASGI format, the response is captured from the `send()` calls, and returned as an `httpx.Response`. This is why async testing with `AsyncClient` + `ASGITransport` is the recommended approach for async-heavy FastAPI apps.
