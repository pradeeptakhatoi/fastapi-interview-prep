# Common FastAPI Pitfalls

## 1. Mutable Default Arguments in Pydantic Models

### The Problem

Python's mutable default argument trap applies to Pydantic models:

```python
# WRONG: all instances share the same list object
class Item(BaseModel):
    tags: list[str] = []        # appears in Pydantic, but...
    metadata: dict = {}
```

**With Pydantic v2, this is actually safe** — Pydantic's `ModelMetaclass` detects mutable defaults and deep-copies them per instance. The classic Python mutable default bug (`def f(x=[])`) doesn't apply to Pydantic `BaseModel` fields.

However, the equivalent bug CAN appear in:

**FastAPI parameter defaults:**
```python
# WRONG: same list instance reused across requests
@app.get("/items/")
async def list_items(filters: list[str] = ["active"]):
    filters.append("other")  # mutates the default!
    ...
```

**Class-based dependency state:**
```python
# WRONG: instance-level mutable shared across requests
class SearchDep:
    def __init__(self):
        self.history = []  # shared if same instance is Depends'd

    async def __call__(self, q: str) -> list:
        self.history.append(q)  # state leaks across requests
        return self.history
```

**The fix:**
```python
# FastAPI default: use None and create inside
@app.get("/items/")
async def list_items(filters: list[str] | None = None):
    if filters is None:
        filters = ["active"]
    ...

# Class-based dep: never store per-request state on the instance
class SearchDep:
    def __init__(self, max_results: int = 10):
        self.max_results = max_results  # config only — OK

    async def __call__(self, q: str) -> list:
        return []  # compute per-call, return new objects
```

---

### Interview Questions

**Q: A Pydantic model with `tags: list[str] = []` — is this safe or a bug?**

In Pydantic v2, it's safe. Pydantic wraps mutable defaults in `default_factory` during schema compilation. Each model instance gets a fresh copy. However, you can also be explicit: `tags: list[str] = Field(default_factory=list)`.

**Q: Where CAN the mutable default bug appear in a FastAPI application?**

1. Python function default arguments in route handlers (not FastAPI params)
2. Class-level mutable attributes on dependency classes used across requests
3. Module-level mutable globals mutated during request handling
4. `@lru_cache`'d functions that cache mutable objects (callers may mutate the cached object)

---

## 2. Circular Imports in Large FastAPI Projects

### The Problem

FastAPI projects commonly grow into circular import structures:

```
# models.py imports from schemas.py (for response_model)
# schemas.py imports from models.py (for ORM validation)
# deps.py imports from both
# routes.py imports from deps.py
# main.py imports from routes.py
# ... and models.py imports something from routes.py for reverse navigation
```

Python's circular import handling: on the second import of a partially-loaded module, you get whatever is defined so far. If `ClassA` in `a.py` is used in `b.py`, but `b.py` is imported before `ClassA` is defined in `a.py`, you get `AttributeError: partially initialized module 'a' has no attribute 'ClassA'`.

### Solutions

**1. Local imports (inside functions):**
```python
# schemas.py
def get_user_schema():
    from myapp.models import User  # imported when function is called, not at module load
    return User
```
Works but makes the import graph implicit and harder to understand.

**2. TYPE_CHECKING guard (for type annotations only):**
```python
from __future__ import annotations
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from myapp.models import User  # only imported by type checkers, not at runtime

def process_user(user: "User") -> None:  # forward reference
    ...
```

**3. Restructure into layers** (best long-term fix):
```
myapp/
  models/     ← no imports from other app layers
  schemas/    ← imports from models/ only
  services/   ← imports from models/, schemas/
  deps/       ← imports from services/, models/
  routes/     ← imports from deps/, schemas/
  main.py     ← imports from routes/
```
Each layer only imports from layers below it. Circular imports become structurally impossible.

---

### Interview Questions

**Q: How do you diagnose a circular import error vs. an `AttributeError` from partial initialization?**

Circular import causes one of:
- `ImportError: cannot import name 'X' from partially initialized module 'Y'`
- `AttributeError: module 'Y' has no attribute 'X'` (when the attribute wasn't defined yet)

Diagnosis: add `print(f"importing {__name__}")` at the top of suspected modules. The order of prints reveals the import chain. Tools: `importlib` traceback, or `python -c "import myapp.main" 2>&1 | grep -i import`.

---

## 3. Misusing Global State vs app.state vs contextvars

### The Problem

Three mechanisms for stateful data in FastAPI, each with different scopes:

| Mechanism | Scope | Thread/Task Safe? | Initialized When? |
|-----------|-------|-------------------|-------------------|
| Module-level global | Process (per-worker) | No (needs locking) | Import time |
| `app.state` | Process (per-worker) | Read-safe after startup | Lifespan startup |
| `contextvars.ContextVar` | Per-request (per-task) | Yes (per-task isolation) | Per-request |

**Wrong: mutable global mutated during requests:**
```python
request_count = 0  # global

@app.get("/")
async def root():
    global request_count
    request_count += 1  # race condition in async context!
    return {"count": request_count}
```

Two concurrent async requests both read `request_count=5`, both write `6` — the counter is off.

**Wrong: `app.state` used for per-request data:**
```python
@app.get("/")
async def root(request: Request):
    request.app.state.current_user = get_user()  # shared across ALL requests!
    ...
```
`app.state` is shared across requests and workers — never write per-request data there.

**Correct patterns:**
```python
# Process-level singletons (read-only after startup): app.state
app.state.db_pool = await create_pool()  # in lifespan
app.state.redis = ...

# Per-request state: request.state (Starlette's per-request state dict)
request.state.user = await get_current_user(token)

# Cross-cutting per-request state (without threading through deps): ContextVar
request_id_var: ContextVar[str] = ContextVar("request_id")
request_id_var.set(str(uuid.uuid4()))  # in middleware

# Thread-safe counter: use atomic operations or Redis
# Never mutate a module-level counter in async code without a lock
```

---

### Interview Questions

**Q: Your app is counting requests in a global variable. Works in development, wrong numbers in production. Why?**

In production with Gunicorn + 4 workers, each worker has its own counter. Counts are per-process, not global. The total is the sum of all workers. For true global counting, use Redis `INCR`.

Additionally, within one async worker, concurrent requests can race on the counter — but in Python's asyncio, the GIL and `+=` being roughly equivalent to three bytecodes means this is less likely (though still possible between two coroutines that switch between reads and writes).

---

## 4. Memory Leaks from Unclosed Async Resources

### The Problem

```python
# WRONG: new client created per request, never closed
@app.get("/fetch")
async def fetch(url: str):
    client = httpx.AsyncClient()  # connection pool created
    resp = await client.get(url)
    return resp.json()
    # client never closed → connection pool and file descriptors leak
```

`httpx.AsyncClient` creates a connection pool when initialized. If not closed, connections stay open and eventually exhaust the OS file descriptor limit (`Too many open files` error in production).

**Common leak patterns:**
1. Creating `httpx.AsyncClient` per request without `async with`
2. SQLAlchemy `AsyncSession` created without `async with` or `yield` dep
3. `aiofiles` handles opened but not closed on error paths
4. Redis connections from pool not released after exceptions
5. `asyncio.Queue` consumers not cancelled on app shutdown

**Fixes:**

```python
# Pattern 1: shared client in lifespan (best for HTTP clients)
@asynccontextmanager
async def lifespan(app):
    app.state.http = httpx.AsyncClient()
    yield
    await app.state.http.aclose()

@app.get("/fetch")
async def fetch(url: str, request: Request):
    resp = await request.app.state.http.get(url)  # shared, no leak
    return resp.json()

# Pattern 2: context manager for per-request clients (if needed)
@app.get("/fetch")
async def fetch(url: str):
    async with httpx.AsyncClient() as client:  # always closed
        resp = await client.get(url)
    return resp.json()

# Pattern 3: yield dependency for any resource
async def get_http_client() -> AsyncGenerator[httpx.AsyncClient, None]:
    async with httpx.AsyncClient() as client:
        yield client

@app.get("/fetch")
async def fetch(url: str, client: httpx.AsyncClient = Depends(get_http_client)):
    resp = await client.get(url)
    return resp.json()
```

---

### Interview Questions

**Q: How do you detect HTTP client connection leaks in production?**

1. **`ss -s` or `netstat`**: count `ESTABLISHED` and `CLOSE_WAIT` connections. Growing `CLOSE_WAIT` (client closed its end but server didn't) often signals unclosed HTTP clients.

2. **Application metrics**: track open file descriptor count via `/proc/<pid>/fd` on Linux. Steady growth = leak.

3. **httpx internals**: `client._pool._connections` shows active connections in the pool. Add logging in dev.

4. **Memory profiling**: `tracemalloc` or `memray` can show where `httpx.AsyncClient` objects are being created without being garbage collected.

**Q: Why does `async with httpx.AsyncClient() as client:` per request work but is suboptimal?**

It's correct (no leak) but inefficient. Each request creates a new TCP connection to the target server (no connection pooling across requests), adds TLS handshake latency, and creates/destroys a connection pool on every request. The shared lifespan pattern reuses connections via the pool, dramatically reducing latency and overhead for high-traffic scenarios.

---

## Code: Pitfall-Free FastAPI Service Pattern

```python
import asyncio
from contextlib import asynccontextmanager
from contextvars import ContextVar
from typing import Annotated, AsyncGenerator

import httpx
from fastapi import Depends, FastAPI, Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

# ContextVar for request-scoped data (not global mutation)
request_id_var: ContextVar[str] = ContextVar("request_id", default="")


@asynccontextmanager
async def lifespan(app: FastAPI):
    engine = create_async_engine("postgresql+asyncpg://user:pass@localhost/db")
    app.state.session_factory = async_sessionmaker(engine, expire_on_commit=False)
    app.state.http_client = httpx.AsyncClient()  # shared, pooled
    yield
    await app.state.http_client.aclose()
    await engine.dispose()


app = FastAPI(lifespan=lifespan)


# DB session — properly scoped with yield dependency
async def get_db(request: Request) -> AsyncGenerator[AsyncSession, None]:
    async with request.app.state.session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


# HTTP client — shared singleton, never per-request creation
def get_http_client(request: Request) -> httpx.AsyncClient:
    return request.app.state.http_client


DbSession = Annotated[AsyncSession, Depends(get_db)]
HttpClient = Annotated[httpx.AsyncClient, Depends(get_http_client)]


@app.get("/items/")
async def list_items(db: DbSession, http: HttpClient) -> list:
    # db: fresh session, properly scoped, will be committed/rolled back
    # http: shared client, connection pool reused across requests
    return []
```

---

## Under the Hood

**Mutable default safety in Pydantic v2:** `ModelMetaclass.__new__()` inspects each field's default. If the default is a mutable type (list, dict, set), it's replaced with a `default_factory` that creates a new instance per model creation. This happens during class definition via `pydantic._internal._fields.collect_model_fields()`.

**Resource leaks and `__del__`:** Python's garbage collector will eventually call `__del__` on unclosed `httpx.AsyncClient` objects. But `__del__` cannot `await` — it's a sync method. `httpx` logs a warning about unclosed clients but cannot properly close async resources. The connections remain open at the OS level until the process exits or the TCP keepalive timeout fires.
