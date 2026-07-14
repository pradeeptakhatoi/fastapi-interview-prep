# async def vs def Endpoints — FastAPI's Threadpool Behavior

## Concept

FastAPI runs on an async event loop (via `uvicorn`/`anyio`). How it handles your route function depends on whether it's `async def` or `def`:

- **`async def` route**: called directly with `await` on the event loop. Any blocking call inside it stalls the entire event loop.
- **`def` route**: wrapped by FastAPI and executed in a thread pool via `anyio.to_thread.run_sync()`. This offloads the blocking execution to a worker thread, freeing the event loop.

This behavior is **not optional** and not something you configure per-route. FastAPI detects sync vs async at route registration using `asyncio.iscoroutinefunction()` (and checking for generator functions for `yield` deps).

The thread pool used is `anyio`'s default thread pool — backed by Python's `concurrent.futures.ThreadPoolExecutor`. The default thread count is typically `min(32, os.cpu_count() + 4)`.

---

## Interview Questions

### Q1: I have a FastAPI route defined with `def` (sync). Is it safe to make it a sync SQLAlchemy call?

**Model answer:**

Yes, because FastAPI runs sync routes in a thread pool, not on the event loop. A blocking sync SQLAlchemy `session.execute()` call will block its worker thread, but not the event loop — other async routes continue to handle requests concurrently.

The caveats:

**Thread pool exhaustion:** if all worker threads are busy with long-running DB calls, incoming `def` routes queue up waiting for a free thread. The default thread pool has O(30) threads. Under high load with slow DB queries, this is a real bottleneck.

**No async context:** inside a `def` route, you cannot `await` anything. You can't use `aiohttp`, `asyncpg`, or any async client. If you need to call an async function from sync code, you're in trouble (you can't just call `asyncio.run()` from inside a running event loop).

**GIL contention:** sync routes run in threads. CPU-bound work holding the GIL will slow down other threads. I/O-bound sync routes (DB, HTTP) release the GIL during I/O — that's fine.

The practical recommendation: use `async def` for new endpoints and async libraries. Use `def` only when integrating with sync-only libraries (legacy ORMs, PDF generators, etc.).

---

### Q2: You convert a sync route to `async def` but don't change the internals — the route still calls `requests.get()`. What breaks?

**Model answer:**

The route now runs on the event loop. `requests.get()` is a blocking network call — it blocks the entire event loop thread while waiting for the HTTP response. During that time, no other async routes can execute, WebSocket messages can't be processed, and health check endpoints don't respond.

The event loop is single-threaded. One blocking call in an `async def` route blocks everyone.

**The fix:** replace `requests` with `httpx.AsyncClient`:

```python
# Wrong: blocks event loop
@app.get("/proxy")
async def proxy():
    response = requests.get("https://api.example.com/data")
    return response.json()

# Correct: non-blocking
@app.get("/proxy")
async def proxy():
    async with httpx.AsyncClient() as client:
        response = await client.get("https://api.example.com/data")
    return response.json()
```

Or, if you must use `requests`, run it in a thread:

```python
from fastapi.concurrency import run_in_threadpool

@app.get("/proxy")
async def proxy():
    response = await run_in_threadpool(requests.get, "https://api.example.com/data")
    return response.json()
```

**Gotcha follow-up:** Is `requests.get()` in a `def` route safe?

Yes. A sync `def` route runs in a worker thread. `requests.get()` blocks that thread (releases the GIL during I/O), but the event loop is free. This is the safe and correct way to use sync I/O libraries in FastAPI.

---

### Q3: How does FastAPI decide whether to call `await endpoint()` or `run_in_threadpool(endpoint)`?

**Model answer:**

At route registration, FastAPI calls `asyncio.iscoroutinefunction(endpoint)`. If True → `await endpoint(**kwargs)` at request time. If False → `await run_in_threadpool(endpoint, **kwargs)`.

`run_in_threadpool` is `anyio.to_thread.run_sync(func, *args)` with a cancellation scope. The function runs in `anyio`'s default thread pool.

The same logic applies to dependencies: async deps are `await`ed; sync deps are run in the thread pool. This means a sync `def get_db()` dependency runs in the thread pool before its yielded value is passed to the route.

```python
from fastapi.concurrency import run_in_threadpool
import inspect

# FastAPI's internal check (simplified)
if inspect.iscoroutinefunction(endpoint):
    result = await endpoint(**kwargs)
else:
    result = await run_in_threadpool(endpoint, **kwargs)
```

**Gotcha:** `functools.wraps` and decorator chains. If you wrap an `async def` function with a decorator that returns a plain `def` (a common mistake), FastAPI sees a sync function and runs it in the thread pool. The inner coroutine would never be awaited — it would be created and immediately garbage-collected, and your route would return `None`. Always ensure decorators preserve coroutine status.

---

### Q4: What's the performance tradeoff between a single-worker async app and a multi-worker sync app?

**Model answer:**

**Single-worker async app (1 Uvicorn process, `async def` routes):**
- Handles many concurrent I/O-bound requests on one CPU core
- Zero thread switching overhead
- Memory efficient (no per-thread stack)
- Broken by any blocking call (one bad route kills everyone)
- CPU-bound work blocks the event loop

**Multi-worker sync app (N Gunicorn workers, `def` routes with sync SQLAlchemy):**
- Each worker handles one request at a time (GIL-limited)
- N workers = N concurrent requests max (ignoring I/O wait)
- Context switching overhead between threads/processes
- Isolated: a blocking call in one worker doesn't affect others
- Easy to reason about — sequential execution per worker

**The real world:** the production pattern is a hybrid — Gunicorn + UvicornWorker (N workers, each with async event loop) + `async def` routes + async libraries. You get both: multiple CPU cores and async I/O within each worker.

---

## Code: Demonstrating Sync vs Async Behavior

```python
import asyncio
import time
from fastapi import FastAPI
from fastapi.concurrency import run_in_threadpool

app = FastAPI()


# Safe: async def + async I/O
@app.get("/async-safe")
async def async_safe():
    await asyncio.sleep(0.1)  # releases event loop during sleep
    return {"done": True}


# Dangerous: async def + sync I/O
@app.get("/async-dangerous")
async def async_dangerous():
    time.sleep(0.1)  # BLOCKS EVENT LOOP — all other routes stall
    return {"done": True}


# Safe: def route — FastAPI runs this in thread pool
@app.get("/sync-safe")
def sync_safe():
    time.sleep(0.1)  # blocks only its thread, not the event loop
    return {"done": True}


# Safe: async def + sync I/O explicitly moved to thread pool
@app.get("/async-with-blocking-io")
async def async_with_blocking_io():
    result = await run_in_threadpool(time.sleep, 0.1)
    return {"done": True}


# Pattern: async route that calls a sync-only library
import requests as sync_requests

@app.get("/weather")
async def get_weather(city: str) -> dict:
    # requests is sync-only; offload to thread pool
    data = await run_in_threadpool(
        lambda: sync_requests.get(
            f"https://api.weather.example.com/{city}"
        ).json()
    )
    return data
```

---

## Under the Hood

The thread pool dispatch is in `fastapi/routing.py:run_endpoint_function()`:

```python
async def run_endpoint_function(dependant, values, is_coroutine):
    if is_coroutine:
        return await dependant.call(**values)
    else:
        return await run_in_threadpool(dependant.call, **values)
```

`run_in_threadpool` is `starlette.concurrency.run_in_threadpool`, which delegates to `anyio.to_thread.run_sync()`. `anyio` uses the backend's thread pool — for asyncio, this is `asyncio.get_event_loop().run_in_executor(None, func)` under the hood, where `None` means the default `ThreadPoolExecutor`.

The `is_coroutine` flag is computed once at route registration and stored on the `Dependant` object — no per-request `isinstance` check.
