# What Blocks the Event Loop and Why

## Concept

The asyncio event loop is a single-threaded scheduler. It can juggle thousands of concurrent I/O operations by switching between coroutines whenever one `await`s. But if a coroutine runs Python code without `await`ing, it holds the loop for the entire duration — blocking all other coroutines.

**What blocks:**

| Operation | Blocks? | Fix |
|-----------|---------|-----|
| `time.sleep(n)` | Yes | `await asyncio.sleep(n)` |
| `requests.get(url)` | Yes | `httpx.AsyncClient.get()` |
| `psycopg2` / sync SQLAlchemy | Yes | `asyncpg` / SQLAlchemy async |
| `open(file).read()` | Yes (small files OK, large files problem) | `aiofiles` or `run_in_threadpool` |
| CPU-bound loop | Yes | `run_in_threadpool` or `ProcessPoolExecutor` |
| `json.loads(large_str)` | Yes (CPU) | Usually fast enough; move to thread if >10ms |
| `subprocess.run()` | Yes | `asyncio.create_subprocess_exec()` |
| Pydantic validation | Minimal (Rust, fast) | Generally not a concern |
| Redis `await client.get()` | No — async | Correct |

**The 5ms rule of thumb:** any synchronous operation taking more than ~5ms in an async context is worth moving to a thread pool or redesigning with async primitives.

---

## Interview Questions

### Q1: How do you identify blocking calls in a production FastAPI app?

**Model answer:**

**1. py-spy sampling profiler:**
```bash
py-spy top --pid $(pgrep uvicorn)
```
Shows which functions are taking CPU time. A blocking sync call will show up as a Python frame holding execution time, not waiting in async I/O.

**2. Event loop monitoring:**
Detect when the event loop is blocked for more than a threshold:
```python
import asyncio
import time

async def monitor_loop(threshold_ms: float = 50):
    while True:
        before = time.monotonic()
        await asyncio.sleep(0)  # yield to event loop
        elapsed_ms = (time.monotonic() - before) * 1000
        if elapsed_ms > threshold_ms:
            print(f"Event loop blocked for {elapsed_ms:.1f}ms")
        await asyncio.sleep(1)
```

**3. `asyncio.set_event_loop_policy` with debug mode:**
```python
asyncio.get_event_loop().set_debug(True)
# Logs coroutines that take >0.1s (configurable via asyncio.coroutine slow_callback_duration)
```

**4. OpenTelemetry / Datadog APM traces:** slow spans on route handlers that show CPU time rather than I/O wait time.

**Gotcha follow-up:** A route is slow but py-spy shows nothing — it's "waiting." What's happening?

It's an async I/O wait — the coroutine is suspended at an `await`, not consuming CPU. Slow SQL queries, slow external HTTP calls, or a Redis latency spike won't show CPU time. Use distributed tracing (OpenTelemetry) to attribute latency to specific I/O operations.

---

### Q2: You have a FastAPI app that occasionally freezes for 2-3 seconds and then recovers. All routes become unresponsive during the freeze. What's the most likely cause?

**Model answer:**

A periodic blocking call on the event loop. Common culprits:

**1. Python garbage collection:** The cyclic GC runs periodically. If you have large object graphs (Pydantic models, SQLAlchemy result sets), GC can pause for hundreds of milliseconds. Check with `gc.set_debug(gc.DEBUG_STATS)`.

**2. DNS resolution:** `socket.getaddrinfo()` is synchronous. If an HTTP client (even an async one) resolves a hostname that's slow or times out, it blocks. `asyncio.get_event_loop().getaddrinfo()` is async — use it or ensure your async HTTP client uses async DNS.

**3. A periodic background task:** scheduled via `asyncio.sleep()` loops that occasionally run CPU-intensive work.

**4. File system operations:** writing logs to a slow disk, or `open()` on an NFS mount that stalls.

**5. `pickle` serialization:** pickling/unpickling large objects in a Celery task dispatcher or cache that runs on the event loop.

The 2-3 second pattern suggests either a DNS timeout or a periodic task with heavy computation.

---

### Q3: How does CPU-bound work differ from I/O-bound blocking, and how do you handle each?

**Model answer:**

**I/O-bound blocking** (network, disk): the CPU is idle, waiting for external data. Moving to `run_in_threadpool` works because the worker thread releases the GIL during I/O, letting other threads and the event loop run. Solution: async I/O or `run_in_threadpool`.

**CPU-bound blocking** (image resizing, ML inference, data processing): the CPU is fully occupied. Moving to `run_in_threadpool` still blocks OTHER threads due to the GIL. Python threads don't achieve true parallelism for CPU work. Solution: `ProcessPoolExecutor` to bypass the GIL:

```python
import asyncio
from concurrent.futures import ProcessPoolExecutor

executor = ProcessPoolExecutor(max_workers=4)

def cpu_heavy(data: bytes) -> bytes:
    # Runs in a separate process — no GIL contention
    import hashlib
    return hashlib.sha256(data).digest()

@app.post("/process")
async def process(request: Request) -> dict:
    data = await request.body()
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(executor, cpu_heavy, data)
    return {"hash": result.hex()}
```

For ML inference specifically: frameworks like PyTorch release the GIL during C extension computation, so `run_in_threadpool` may be sufficient. Measure before assuming you need processes.

**Memory note:** `ProcessPoolExecutor` serializes arguments/results via `pickle`. Large numpy arrays or Pydantic models have significant pickling overhead. For ML, serving behind a separate process (Triton, TorchServe) via HTTP is often cleaner.

---

## Code: Async-Safe Patterns for Common Blocking Operations

```python
import asyncio
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import aiofiles
import httpx
from fastapi import FastAPI, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from sqlalchemy.ext.asyncio import AsyncSession

app = FastAPI()
_process_pool = ProcessPoolExecutor(max_workers=2)


# File I/O — use aiofiles
@app.get("/read-file")
async def read_file(path: str) -> dict:
    async with aiofiles.open(path, "r") as f:
        content = await f.read()
    return {"length": len(content)}


# External HTTP — use httpx.AsyncClient (create once, reuse via lifespan)
@app.get("/fetch")
async def fetch(url: str) -> dict:
    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.get(url)
        return resp.json()


# Sync-only library (e.g., PDF generation)
def generate_pdf_sync(content: str) -> bytes:
    # Hypothetical sync PDF library
    return b"%PDF-..."

@app.post("/pdf")
async def generate_pdf(content: str) -> bytes:
    pdf_bytes = await run_in_threadpool(generate_pdf_sync, content)
    from fastapi.responses import Response
    return Response(content=pdf_bytes, media_type="application/pdf")


# CPU-bound: image processing
def resize_image_sync(image_bytes: bytes, width: int, height: int) -> bytes:
    from PIL import Image
    import io
    img = Image.open(io.BytesIO(image_bytes))
    img = img.resize((width, height))
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    return buf.getvalue()

@app.post("/resize")
async def resize_image(file: UploadFile, width: int = 800, height: int = 600):
    image_bytes = await file.read()
    loop = asyncio.get_event_loop()
    resized = await loop.run_in_executor(
        _process_pool, resize_image_sync, image_bytes, width, height
    )
    from fastapi.responses import Response
    return Response(content=resized, media_type="image/jpeg")


# Event loop health monitor — run as a background task
async def loop_health_monitor(warn_threshold_ms: float = 100):
    import time
    while True:
        t0 = time.monotonic()
        await asyncio.sleep(0)
        lag_ms = (time.monotonic() - t0) * 1000
        if lag_ms > warn_threshold_ms:
            import logging
            logging.warning(f"Event loop lag: {lag_ms:.1f}ms")
        await asyncio.sleep(1)
```

---

## Under the Hood

The event loop's "blocking detection" in debug mode is implemented in `asyncio/base_events.py`. When `loop.slow_callback_duration` is set (default 0.1 seconds), any callback that runs longer than this threshold logs a warning: `"Executing <Task> took X.XXX seconds"`. This is how you catch blocking calls in development.

In production, the right tool is the event loop's built-in lag detection: the difference between when you scheduled `asyncio.sleep(0)` and when it actually returned. This lag = time the event loop was busy with other coroutines (or blocked). A persistent lag > 50ms signals a blocking call somewhere in the app.
