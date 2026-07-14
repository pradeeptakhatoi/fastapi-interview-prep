# Lifespan Context Manager and Background Tasks

## Concept

**Lifespan** manages application startup and shutdown. The modern API is the `@asynccontextmanager` lifespan parameter:

```python
from contextlib import asynccontextmanager
from fastapi import FastAPI

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: initialize resources
    yield
    # Shutdown: clean up resources

app = FastAPI(lifespan=lifespan)
```

Code before `yield` runs once when the ASGI server sends `lifespan.startup`. Code after `yield` runs when the server sends `lifespan.shutdown`. This replaces the deprecated `@app.on_event("startup")` / `@app.on_event("shutdown")`.

**BackgroundTasks** schedules callables to run *after the response is sent* to the client. They run in the same process (same event loop) but after the HTTP response is fully dispatched.

```python
from fastapi import BackgroundTasks

@app.post("/send-email")
async def send_email(background_tasks: BackgroundTasks, email: str):
    background_tasks.add_task(actually_send_email, email)
    return {"status": "queued"}  # returns immediately; email sends after
```

`BackgroundTasks` are not a job queue — no persistence, no retries, no failure handling, no distributed execution.

---

## Interview Questions

### Q1: What's the difference between the deprecated `on_event` and the lifespan context manager? Why was it changed?

**Model answer:**

`@app.on_event("startup")` and `@app.on_event("shutdown")` register separate callbacks:

```python
@app.on_event("startup")
async def startup():
    app.state.db = await create_pool()

@app.on_event("shutdown")
async def shutdown():
    await app.state.db.close()
```

The problems:
1. **No guarantee of pairing**: if startup creates a resource, shutdown must close it — but there's nothing enforcing this linkage. A forgotten `on_event("shutdown")` leaks resources.
2. **Exception handling gap**: if startup raises, there's no mechanism to clean up partial initialization. With the context manager, the `finally` block handles this.
3. **Not composable**: multiple startup/shutdown events don't form a clear dependency ordering. The lifespan context manager is just Python — you can nest context managers, use `async with`, and control ordering explicitly.

The `asynccontextmanager` lifespan:
- Pairs startup and shutdown in one function (clear coupling)
- `finally` block always runs even on startup failure (partial cleanup)
- Multiple resources compose naturally with nested `async with`

```python
@asynccontextmanager
async def lifespan(app):
    async with create_db_pool() as pool, create_redis() as redis:
        app.state.db = pool
        app.state.redis = redis
        yield  # both resources open during app lifetime
    # Both closed automatically on exit, even on exception
```

---

### Q2: What are the failure modes of BackgroundTasks, and when should you use a real job queue instead?

**Model answer:**

**BackgroundTasks failure modes:**

1. **No persistence**: if the worker process dies (SIGKILL, OOM), pending background tasks are lost. No retry, no dead-letter queue.

2. **No failure isolation**: an unhandled exception in a background task is logged but doesn't affect the HTTP response. You may not know tasks are failing unless you have logging/monitoring.

3. **No distributed execution**: background tasks run in the same process. Under Gunicorn with 4 workers, a task queued on worker 1 runs on worker 1. You can't distribute task execution across workers or machines.

4. **Concurrency with event loop**: if your background task does async I/O, it shares the event loop with active requests. A heavy background task (large report generation) can slow down request handling.

5. **No rate limiting or backpressure**: 100 concurrent requests each adding a background task = 100 tasks immediately queued. There's no throttling.

**When BackgroundTasks is appropriate:**
- Fire-and-forget non-critical work (audit logging, cache warming, simple email send)
- Tasks that must succeed together with the request (transactional semantics aren't needed)
- Single-worker development/testing environments

**When to use a real queue (Celery, ARQ, Dramatiq, Redis Streams):**
- Tasks that must survive process restarts
- Retry on failure required
- Rate limiting or priority queuing
- CPU-heavy work that should run on separate workers
- Tasks that take longer than the HTTP request timeout

---

### Q3: How does BackgroundTasks interact with yield dependencies?

**Model answer:**

This is a critical gotcha. Background tasks run AFTER the HTTP response is sent AND after yield dependency teardown.

The timeline:
1. Route handler executes (session from `get_db` is open)
2. `background_tasks.add_task(some_func)` — task is registered
3. Route returns → response is sent to client
4. `yield` dependencies tear down (session commits and closes)
5. Background tasks execute

**The problem:** if your background task tries to use the DB session from a `yield` dependency, the session is already closed by the time the task runs.

```python
@app.post("/orders")
async def create_order(
    order: OrderIn,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
) -> dict:
    new_order = Order(**order.model_dump())
    db.add(new_order)
    await db.flush()
    
    # WRONG: db session will be closed when this task runs
    background_tasks.add_task(send_confirmation_email, db, new_order.id)
    return {"order_id": new_order.id}
```

**Fixes:**
1. Pass only data (IDs, values) to the background task, not session objects. The task opens its own session.
2. Have the background task create its own DB connection.

```python
@app.post("/orders")
async def create_order(
    order: OrderIn,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
) -> dict:
    new_order = Order(**order.model_dump())
    db.add(new_order)
    await db.flush()
    order_id = new_order.id  # extract ID before session closes
    
    # Correct: task gets ID, creates its own DB connection
    background_tasks.add_task(send_confirmation_email, order_id)
    return {"order_id": order_id}

async def send_confirmation_email(order_id: int) -> None:
    async with AsyncSessionLocal() as db:  # own session
        order = await db.get(Order, order_id)
        # ... send email
```

---

## Code: Lifespan with Multiple Resources + Background Task Pattern

```python
import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Any

import httpx
import redis.asyncio as aioredis
from fastapi import BackgroundTasks, FastAPI, Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Initialize all resources before yield
    engine = create_async_engine("postgresql+asyncpg://user:pass@localhost/db")
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    redis = await aioredis.from_url("redis://localhost:6379")
    http_client = httpx.AsyncClient(timeout=10.0)

    app.state.engine = engine
    app.state.session_factory = session_factory
    app.state.redis = redis
    app.state.http_client = http_client

    logger.info("Application startup complete")

    yield  # ← server accepts requests here

    # Teardown in reverse order of initialization
    await http_client.aclose()
    await redis.aclose()
    await engine.dispose()
    logger.info("Application shutdown complete")


app = FastAPI(lifespan=lifespan)


# Background task functions — manage their own resources
async def send_welcome_email(user_id: int, email: str) -> None:
    # Creates its own session — request's session is already closed
    async with AsyncSessionLocal() as db:  # type: ignore
        user = await db.get(User, user_id)  # type: ignore
        # ... send email via HTTP
        logger.info(f"Sent welcome email to {email}")


async def update_analytics(event: str, data: dict) -> None:
    # Analytics can be best-effort; log failures but don't raise
    try:
        # In real code: use app.state.http_client or a queue
        pass
    except Exception:
        logger.exception(f"Analytics update failed for event {event}")


@app.post("/users", status_code=201)
async def create_user(
    email: str,
    request: Request,
    background_tasks: BackgroundTasks,
) -> dict:
    # ... create user in DB
    user_id = 42  # from DB

    # Safe: pass IDs/values, not session objects
    background_tasks.add_task(send_welcome_email, user_id, email)
    background_tasks.add_task(update_analytics, "user_created", {"user_id": user_id})

    return {"user_id": user_id, "email": email}
```

---

## Under the Hood

**Lifespan protocol:** Starlette's `Router` implements the lifespan ASGI protocol in `starlette/routing.py:Router.lifespan()`. When the ASGI server sends `{"type": "lifespan.startup"}`, Starlette enters the lifespan context manager and signals completion. The `yield` in the context manager is where Starlette waits — it suspends the lifespan coroutine until `{"type": "lifespan.shutdown"}` arrives.

**BackgroundTasks execution:** defined in `starlette/background.py`. The `BackgroundTasks.add_task()` just appends to a list. After the route handler returns, `starlette/routing.py:Route.handle()` calls `await response(scope, receive, send)` which:
1. Sends the response headers and body via `send()`
2. After the last `http.response.body` event, calls `await response.background()` if background tasks were added

`response.background()` is `BackgroundTask.__call__()` which iterates the task list and calls each one. For async tasks, it `await`s them directly; for sync tasks, it uses `anyio.to_thread.run_sync()`. Tasks run sequentially in registration order.
