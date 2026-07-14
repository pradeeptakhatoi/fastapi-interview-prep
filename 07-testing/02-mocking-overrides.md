# Overriding Dependencies in Tests, Mocking External Async Calls

## Concept

FastAPI testing has two distinct levels of interception:

1. **`app.dependency_overrides`**: replaces a FastAPI dependency callable with a test version. The override is respected at the FastAPI DI layer — correct for testing route behavior with fake services, fake sessions, fake users.

2. **`unittest.mock.patch` / `AsyncMock`**: patches Python objects at module level. Correct for mocking external library calls (HTTP clients, email senders, S3 clients) that aren't FastAPI dependencies but are called inside your service layer.

Both are necessary in a real test suite. They solve different problems.

**`AsyncMock`** (Python 3.8+): a mock that returns coroutines when called, allowing `await mock_func()` to work. Essential for mocking any `async def` function.

---

## Interview Questions

### Q1: You have a route that calls an external payment API via `httpx`. How do you mock it in tests?

**Model answer:**

The payment API call happens inside a service function — it's not a FastAPI dependency. Use `patch`:

```python
# services/payment.py
import httpx

async def charge_card(amount: float, token: str) -> dict:
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://api.stripe.com/v1/charges",
            data={"amount": amount, "source": token},
        )
        return resp.json()
```

```python
# tests/test_orders.py
from unittest.mock import patch, AsyncMock
import pytest
from httpx import AsyncClient, ASGITransport
from myapp.main import app


@pytest.mark.asyncio
async def test_order_charges_card():
    mock_response = AsyncMock()
    mock_response.json.return_value = {"id": "ch_123", "status": "succeeded"}
    mock_response.raise_for_status = AsyncMock()

    with patch("myapp.services.payment.httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client_cls.return_value = mock_client

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post("/orders/", json={"amount": 100.0, "card_token": "tok_visa"})

    assert resp.status_code == 201
    mock_client.post.assert_called_once()
```

Alternatively, use `respx` (httpx mock library) for cleaner API:

```python
import respx
import httpx

@pytest.mark.asyncio
@respx.mock
async def test_order_charges_card():
    respx.post("https://api.stripe.com/v1/charges").mock(
        return_value=httpx.Response(200, json={"id": "ch_123", "status": "succeeded"})
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/orders/", json={"amount": 100.0, "card_token": "tok_visa"})
    assert resp.status_code == 201
```

---

### Q2: How do you test that a background task actually ran?

**Model answer:**

Background tasks run after the response is sent. With `TestClient`, they complete before the request method returns. With `AsyncClient` as a context manager, they complete before `__aexit__`.

**Strategy 1: Mock the background task function and assert it was called:**

```python
from unittest.mock import patch, call

def test_order_queues_email():
    with patch("myapp.tasks.send_confirmation_email") as mock_email:
        with TestClient(app) as client:
            resp = client.post("/orders/", json={"product_id": 1})
        # Background tasks ran before TestClient context exited
    
    mock_email.assert_called_once_with(order_id=ANY)
```

**Strategy 2: Check DB side effects (integration test):**

```python
@pytest.mark.asyncio
async def test_order_creates_audit_log(client, db):
    await client.post("/orders/", json={"product_id": 1})
    # Background task writes audit log to DB
    from myapp.models import AuditLog
    logs = (await db.execute(select(AuditLog).where(AuditLog.action == "order_created"))).scalars().all()
    assert len(logs) == 1
```

**Gotcha:** `AsyncClient` without a context manager may NOT wait for background tasks. Always use `async with AsyncClient(...) as client` — the `__aexit__` waits for the full ASGI response including background tasks.

---

### Q3: How do you override a class-based dependency in tests?

**Model answer:**

The override key must be the exact same callable object used in `Depends()`. For class-based dependencies:

```python
# Production:
class EmailService:
    async def send(self, to: str, subject: str, body: str) -> None:
        # Real implementation
        ...

email_service = EmailService()  # module-level singleton

@app.post("/users/")
async def create_user(
    user: UserCreate,
    email: EmailService = Depends(lambda: email_service),  # returns the singleton
):
    ...
```

**Problem:** if the dependency is `lambda: email_service`, each call creates a new lambda — no way to match it in `dependency_overrides`.

**Better pattern for testability:**

```python
def get_email_service() -> EmailService:
    return email_service  # named function — stable reference

@app.post("/users/")
async def create_user(user: UserCreate, email: EmailService = Depends(get_email_service)):
    ...

# Test:
class FakeEmailService:
    sent: list[dict] = []
    async def send(self, to: str, subject: str, body: str) -> None:
        self.sent.append({"to": to, "subject": subject})

fake_email = FakeEmailService()
app.dependency_overrides[get_email_service] = lambda: fake_email
```

The key insight: **always use named functions (not lambdas) as dependency factories** if you need to override them in tests.

---

## Code: Complete Test Pattern with Multiple Override Types

```python
# tests/conftest.py
import pytest
from httpx import AsyncClient, ASGITransport
from unittest.mock import AsyncMock
from myapp.main import app
from myapp.dependencies import get_db, get_current_user, get_email_service
from myapp.schemas import User

# Fixture: fake user (no real auth needed)
@pytest.fixture
def fake_user():
    return User(id=1, email="test@example.com", scopes=["read", "write"])

# Fixture: fake email service with capture
@pytest.fixture
def fake_email():
    class _FakeEmail:
        calls: list[dict] = []
        async def send(self, to, subject, body):
            self.calls.append({"to": to, "subject": subject})
    return _FakeEmail()

# Fixture: full client with all fakes wired
@pytest.fixture
async def client(db_session, fake_user, fake_email):
    async def override_db():
        yield db_session
    
    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_current_user] = lambda: fake_user
    app.dependency_overrides[get_email_service] = lambda: fake_email
    
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as ac:
        yield ac
    
    app.dependency_overrides.clear()


# tests/test_users.py
@pytest.mark.asyncio
async def test_create_user_sends_email(client, fake_email):
    resp = await client.post("/users/", json={"email": "new@example.com", "name": "Alice"})
    
    assert resp.status_code == 201
    assert len(fake_email.calls) == 1
    assert fake_email.calls[0]["to"] == "new@example.com"
    assert "Welcome" in fake_email.calls[0]["subject"]


@pytest.mark.asyncio
async def test_create_user_validates_email(client):
    resp = await client.post("/users/", json={"email": "not-an-email", "name": "Bob"})
    assert resp.status_code == 422
    errors = resp.json()["detail"]
    assert any("email" in str(e["loc"]) for e in errors)


@pytest.mark.asyncio
async def test_list_users_requires_auth():
    # No overrides — real auth runs
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.get("/users/")
    assert resp.status_code == 401
```

---

## Under the Hood

`patch()` modifies the attribute on the target module at the point of patching. For `patch("myapp.services.payment.httpx.AsyncClient")`, it replaces `httpx.AsyncClient` within the `myapp.services.payment` module's namespace. This is why you must patch where the name is *used*, not where it's *defined* — the patching happens by replacing the module attribute lookup.

`AsyncMock` configures a `Mock` to return a coroutine from `__call__`. When you `await mock_func()`, `mock_func()` returns a coroutine object (not `MagicMock`), and `await`ing it returns the `return_value`. For context managers (`async with`), you need to configure `__aenter__` and `__aexit__` as `AsyncMock` instances separately.
