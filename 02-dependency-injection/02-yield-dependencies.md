# Yield Dependencies — Setup/Teardown, DB Session Pattern, Exception Propagation

## Concept

A `yield` dependency is a generator function used as a dependency. FastAPI treats it as a context manager: code before `yield` is setup (runs before the route handler), the yielded value is injected, and code after `yield` is teardown (runs after the route handler returns, whether successfully or with an exception).

FastAPI uses `contextlib.asynccontextmanager` and `contextlib.contextmanager` internally to convert these generators into context managers, then enters them via an `AsyncExitStack` maintained in `solve_dependencies()`.

**Exception propagation rules:**
- If the route raises an exception, it is `throw()`n into the generator at the `yield` point
- The generator can catch it with `try/except` around `yield`
- If the generator re-raises (or raises a different exception), that exception propagates
- If the generator swallows the exception (no re-raise), the exception is lost

This means teardown code in a `yield` dep always runs — similar to `finally` in try/except.

---

## Interview Questions

### Q1: Trace the execution order of a yield dependency with a try/except/finally block when the route raises an HTTPException.

**Model answer:**

```python
async def get_db():
    session = AsyncSession(engine)
    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()
```

Execution order when the route raises `HTTPException(404)`:

1. `get_db()` starts → `session = AsyncSession(engine)` → `yield session`
2. Route handler receives `session`, begins execution
3. Route raises `HTTPException(404)`
4. FastAPI's `solve_dependencies()` `throw()`s the exception into the generator at the `yield` point
5. Execution jumps to `except Exception:` → `await session.rollback()` → `raise` (re-raises the original exception)
6. `finally:` block → `await session.close()`
7. The exception continues propagating → caught by `ExceptionMiddleware` → 404 response returned

**Critical**: if you omit `raise` in the except block, the exception is swallowed. The 404 handler is never reached and the client gets a 500 (or no response, depending on framework internals). Always re-raise from dependency teardown unless you specifically intend to suppress the error.

**Gotcha follow-up:** What happens if the teardown code itself raises an exception?

If the generator raises during teardown (after `yield`), `AsyncExitStack` catches it and the framework logs it as a background error. The *original* response (from the route handler or the original exception) is what the client receives — the teardown exception doesn't replace it. In practical terms: a crash in `finally:` after `session.close()` is silently discarded from the client's perspective. This makes teardown errors hard to observe without proper logging.

---

### Q2: Why is the DB session pattern implemented with `yield` rather than injecting a session factory?

**Model answer:**

The `yield` pattern guarantees the session lifetime is scoped exactly to one request, with automatic cleanup even on exceptions. The alternatives:

**Session factory injection:**
```python
def get_session_factory():
    return AsyncSessionLocal

@app.get("/")
async def route(factory = Depends(get_session_factory)):
    async with factory() as session:
        ...  # route must manage its own context
```

This forces session management into every route — forgetting `async with` leaks a session. It also makes it harder to share the session between the route and other dependencies (like auth middleware that also needs the DB).

**`yield` pattern:**
```python
async def get_db():
    async with AsyncSessionLocal() as session:
        yield session

@app.get("/")
async def route(session: AsyncSession = Depends(get_db)):
    # session is guaranteed to be open, will be closed after this returns
    ...
```

With caching, this session is shared with all sub-dependencies in the same request. There's no risk of forgetting cleanup, and the session lifetime is guaranteed to not outlive the request.

---

### Q3: Can you have multiple `yield` statements in a single dependency? What happens?

**Model answer:**

No. FastAPI's implementation uses `next()` / `send()` to drive the generator exactly once: it calls `next(gen)` to get the yielded value, then either `gen.close()` (no exception) or `gen.throw(exc)` (exception case). A second `yield` statement would require another `next()` call that FastAPI never makes — the generator would remain suspended at the second `yield` point and never finish, leaking whatever resource was opened after the first yield.

This is by design. A `yield` dependency is a single-entry, single-exit context manager. If you need a resource that requires multiple `yield` points, compose two separate dependencies.

**Practical pattern:** If you need both a DB session and a Redis connection:

```python
async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSessionLocal() as session:
        yield session

async def get_redis() -> AsyncGenerator[Redis, None]:
    async with aioredis.from_url(REDIS_URL) as redis:
        yield redis

@app.get("/")
async def route(
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
):
    ...
```

Each dependency manages its own resource. The `AsyncExitStack` in `solve_dependencies` enters both context managers and exits both on cleanup — in reverse order of registration (LIFO).

---

### Q4: How do async generators work as dependencies vs. sync generators? When does FastAPI use `run_in_threadpool` for them?

**Model answer:**

FastAPI distinguishes between:

- `async def` with `yield` → `asynccontextmanager`, entered with `await`
- `def` with `yield` → `contextmanager`, entered synchronously

For sync generator dependencies (`def get_db() -> Generator`), FastAPI wraps the execution in `run_in_threadpool` — the dependency runs in a thread pool, not the event loop. This is consistent with how FastAPI handles sync route handlers.

In practice: always use `async def` with `yield` if the setup/teardown involves any I/O (DB, Redis, HTTP). Use sync generators only for pure Python resource management that has no I/O:

```python
# Correct: sync generator for non-I/O resource
def get_temp_dir() -> Generator[Path, None, None]:
    with tempfile.TemporaryDirectory() as d:
        yield Path(d)

# Wrong: sync generator with async I/O — will block the event loop
def get_db() -> Generator:
    session = SyncSession(engine)  # blocking!
    try:
        yield session
    finally:
        session.close()
```

If you're using a sync ORM (standard SQLAlchemy), you need to accept that the session operations will block the event loop unless you move them to `run_in_threadpool` explicitly (or use SQLAlchemy's async extension).

---

## Code: Production DB Session Dependency

```python
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Depends, HTTPException
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


DATABASE_URL = "postgresql+asyncpg://user:pass@localhost/mydb"

engine = create_async_engine(
    DATABASE_URL,
    pool_size=10,
    max_overflow=20,
    pool_pre_ping=True,  # validate connections before use
)
AsyncSessionLocal = async_sessionmaker(
    engine,
    expire_on_commit=False,  # prevents lazy-load AttributeError after commit
    autoflush=False,
)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        # AsyncSessionLocal context manager handles session.close() on __aexit__


# Second-level dependency that also uses DB — gets SAME session via caching
async def get_current_user(
    db: AsyncSession = Depends(get_db),
    # token: str = Depends(oauth2_scheme),
) -> dict:
    # user = await db.get(User, user_id_from_token)
    return {"id": 1, "name": "Alice"}


app = FastAPI()


@app.get("/users/me")
async def get_me(
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),  # same session — cached
) -> dict:
    # Both current_user and this route share one session, one transaction
    return current_user


# Nested transaction pattern (savepoint)
async def get_db_with_savepoint(
    db: AsyncSession = Depends(get_db),
) -> AsyncGenerator[AsyncSession, None]:
    async with db.begin_nested():  # savepoint
        yield db
    # savepoint released on success, rolled back on exception
```

---

## Under the Hood

Yield dependency handling lives in `fastapi/dependencies/utils.py:solve_dependencies()`. The key mechanism:

1. `solve_dependencies()` creates a `contextlib.AsyncExitStack`
2. For each `yield` dependency, it calls `_prepare_response_with_background_tasks()` which uses `stack.enter_async_context(asynccontextmanager(dep_gen)())` to enter the generator
3. The stack is passed around and exited after the route handler returns (or raises)
4. If the route raises, `stack.__aexit__(exc_type, exc_val, exc_tb)` is called, which `throw()`s the exception into each generator in reverse entry order

The `AsyncExitStack` is attached to the request's `state` so it can be exited at the right point in the ASGI call — after the route handler but before the response is fully sent. This ensures teardown happens before the connection is closed.
