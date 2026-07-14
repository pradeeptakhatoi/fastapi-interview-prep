# ASGI Server Tradeoffs, uvloop, and Profiling FastAPI Apps

## Concept

**ASGI servers comparison:**

| Server | Language | HTTP/2 | HTTP/3 | uvloop | Notes |
|--------|----------|--------|--------|--------|-------|
| Uvicorn | Python | ✅ (h2 pkg) | ❌ | ✅ opt-in | Most common, easiest to configure |
| Hypercorn | Python | ✅ | ✅ (QUIC) | ✅ opt-in | Best protocol coverage |
| Granian | Rust | ✅ | ❌ | N/A (no GIL) | Fastest raw throughput; newer |
| Daphne | Python | ❌ | ❌ | ❌ | Django Channels legacy |

**uvloop**: a Cython-based drop-in replacement for Python's default asyncio event loop, implemented on top of libuv (the C event loop library also used by Node.js). It's typically 2–4x faster than the default asyncio loop for I/O-heavy workloads.

**Granian**: a Rust ASGI/WSGI server that bypasses Python's GIL for the I/O and event loop layer. It doesn't use uvloop (no need — the Rust runtime handles I/O natively). Shows better raw throughput in benchmarks but is less mature.

---

## Interview Questions

### Q1: What makes uvloop faster than the default asyncio event loop?

**Model answer:**

Python's default asyncio event loop is implemented in pure Python (`asyncio/selector_events.py`). It uses `selectors.DefaultSelector` (which wraps `epoll` on Linux or `kqueue` on macOS, but with Python overhead).

uvloop is implemented in Cython on top of libuv. The advantages:

1. **C-level I/O multiplexing**: epoll/kqueue calls happen in C, not Python. The overhead of the Python function call stack is eliminated for the hot path.

2. **Buffer management in C**: socket read/write buffers are managed in C memory, avoiding Python bytes object allocation on every I/O operation.

3. **Timer resolution**: libuv's timer wheel has better resolution and lower overhead than Python's `heapq`-based timer implementation.

4. **DNS resolution**: libuv has its own async DNS resolver (c-ares); Python's asyncio uses a thread pool for DNS.

Typical speedup: 2–4x for socket I/O throughput, 1.5–2x for overall async application throughput. The speedup is more pronounced for low-latency, high-connection-count scenarios (WebSockets, many short-lived connections) than for applications dominated by DB query latency.

**Installation and use:**
```bash
pip install uvloop
```
```python
# In gunicorn.conf.py
worker_class = "uvicorn.workers.UvicornWorker"
# Uvicorn uses uvloop automatically if installed

# Or: directly in an asyncio app
import uvloop
asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
```

---

### Q2: How do you profile a FastAPI application to find blocking calls in production?

**Model answer:**

**1. py-spy (sampling profiler, zero overhead):**
```bash
pip install py-spy
py-spy top --pid $(pgrep -f "uvicorn")     # live view, like `top`
py-spy record -o profile.svg --pid <pid> --duration 30  # flame graph
```
py-spy is a sampling profiler that attaches to a running Python process without modifying it. It works across threads and can distinguish CPU time from I/O wait. Flame graphs reveal which Python frames are hot.

**2. asyncio debug mode (development only):**
```python
import asyncio
asyncio.get_event_loop().slow_callback_duration = 0.05  # warn on >50ms callbacks
```
```bash
PYTHONASYNCIODEBUG=1 uvicorn myapp.main:app
```
Logs warnings for coroutines that take more than 50ms without yielding to the event loop.

**3. OpenTelemetry tracing:**
Instrument with automatic instrumentation (`opentelemetry-instrumentation-fastapi`) to get per-request traces with span timings. This shows latency breakdown between FastAPI processing, DB queries, external HTTP calls, etc.

```python
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
FastAPIInstrumentor.instrument_app(app)
```

**4. Event loop lag monitoring (custom):**
```python
import asyncio, time

async def loop_lag_monitor():
    while True:
        t = time.monotonic()
        await asyncio.sleep(0)
        lag = time.monotonic() - t
        if lag > 0.05:
            # Log or metric — event loop was blocked for `lag` seconds
            metrics.gauge("event_loop_lag_ms", lag * 1000)
        await asyncio.sleep(1)
```

---

### Q3: How do you choose between Uvicorn and Granian for a production FastAPI deployment?

**Model answer:**

**Choose Uvicorn if:**
- You need `--preload` with Gunicorn (Granian has its own process model, not Gunicorn-compatible)
- You need HTTP/2 or need to match your team's operational familiarity
- You're using Kubernetes or other container orchestration that manages restarts (Gunicorn's worker management is less critical)
- You need mature tooling, debugging, and community support

**Choose Granian if:**
- Raw throughput is the primary concern and you've benchmarked your specific workload
- You want to avoid Python's GIL overhead in the I/O layer
- You're deploying as a standalone process (Granian has built-in multi-worker support)
- You're on an I/O-heavy, CPU-light workload where the GIL is the bottleneck

**Practical advice:** For most production FastAPI deployments, Uvicorn + uvloop + Gunicorn is the proven stack. Granian is worth benchmarking for high-throughput APIs, but the maturity difference matters in production operations.

**Granian usage:**
```bash
pip install granian
granian --interface asgi myapp.main:app --workers 4 --host 0.0.0.0 --port 8000
```

---

## Code: Connection Pooling in FastAPI Lifecycle

```python
import asyncio
import os
from contextlib import asynccontextmanager

import httpx
import redis.asyncio as aioredis
from fastapi import FastAPI, Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine


@asynccontextmanager
async def lifespan(app: FastAPI):
    # DB connection pool — one pool per worker process
    engine = create_async_engine(
        os.environ["DATABASE_URL"],
        pool_size=10,
        max_overflow=5,
        pool_pre_ping=True,
        pool_recycle=3600,
        echo=False,
    )
    app.state.db_session_factory = async_sessionmaker(engine, expire_on_commit=False)

    # Redis connection pool
    redis = aioredis.ConnectionPool.from_url(
        os.environ["REDIS_URL"],
        max_connections=20,
        decode_responses=True,
    )
    app.state.redis_pool = redis

    # HTTP client — shared, not per-request (connection pool reused)
    http_client = httpx.AsyncClient(
        timeout=httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=5.0),
        limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
    )
    app.state.http_client = http_client

    yield

    await http_client.aclose()
    await redis.disconnect()
    await engine.dispose()


app = FastAPI(lifespan=lifespan)


# Dependencies that use app.state resources
async def get_db(request: Request) -> AsyncSession:
    async with request.app.state.db_session_factory() as session:
        yield session


async def get_redis(request: Request) -> aioredis.Redis:
    return aioredis.Redis(connection_pool=request.app.state.redis_pool)


async def get_http(request: Request) -> httpx.AsyncClient:
    return request.app.state.http_client  # return shared client, don't close it
```

---

## Under the Hood

**uvloop's architecture:** libuv uses an event loop with an I/O watcher table. Socket events are registered with epoll (Linux) or kqueue (macOS). When a socket becomes readable/writable, libuv fires the corresponding callback. uvloop implements the CPython asyncio `AbstractEventLoop` protocol on top of libuv, replacing the pure-Python implementations of `create_connection()`, `sock_recv()`, `sock_sendall()`, etc., with Cython calls to libuv.

**Granian's architecture:** written in Rust using `tokio` (Rust's async runtime). The Python ASGI app is called via PyO3, the Rust-Python FFI library. The I/O layer (socket management, HTTP parsing) runs entirely in Rust/tokio. Python code is only invoked for the application logic — the event loop overhead is zero from Python's perspective. The GIL is acquired when calling into Python and released when returning to Rust. For FastAPI apps where the Python code is async and largely waiting on I/O, this architecture can significantly reduce overhead.
