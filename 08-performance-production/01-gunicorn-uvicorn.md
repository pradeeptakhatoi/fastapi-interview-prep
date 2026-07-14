# Gunicorn + UvicornWorker Configuration

## Concept

**Uvicorn alone**: single-process ASGI server. One event loop per process. Scales vertically (more async concurrency) but not horizontally across CPU cores.

**Gunicorn + UvicornWorker**: Gunicorn is a process manager. Each worker is a full Uvicorn ASGI server running in its own process with its own event loop. Gunicorn handles:
- Worker lifecycle (spawn, restart on crash)
- Graceful shutdown and rolling restarts
- Signal handling (SIGHUP for config reload, SIGTERM for graceful shutdown)
- Worker timeout detection and restart

The `UvicornWorker` class (`uvicorn.workers.UvicornWorker`) is what Gunicorn loads instead of a sync worker. Each process handles many concurrent requests via async I/O.

**Worker count formula:**
- I/O-bound APIs: `2 × CPU_COUNT + 1` (Gunicorn's classic rule)
- Memory-constrained systems: `floor(AVAILABLE_RAM_GB / RAM_PER_WORKER_GB)`
- Reality: profile under load; the formula is a starting point

---

## Interview Questions

### Q1: What is `--preload` in Gunicorn, and when should you use or avoid it?

**Model answer:**

`--preload` loads the application code in the Gunicorn master process before forking workers. Workers inherit the pre-loaded code via copy-on-write memory pages.

**Benefits:**
- **Faster startup**: workers fork instead of loading modules from scratch (significant for heavy deps like PyTorch, NumPy, SQLAlchemy)
- **Memory savings**: shared code pages (copy-on-write) reduce per-worker memory if the application code isn't mutated after fork
- **Import validation**: if your app fails to import, `--preload` catches it before workers start, rather than all workers crashing on startup

**Hazards:**
- **Async resources created before fork**: if your lifespan creates DB pools, event loop handles, or open sockets in the master process, they're invalid in child processes (different event loops, OS file descriptors may be shared unsafely)
- **Solution**: always initialize async resources INSIDE lifespan (which runs after fork, once per worker)

```python
# Good: async resources initialized in lifespan (post-fork)
@asynccontextmanager
async def lifespan(app):
    pool = await create_async_pool()  # runs in each worker after fork
    app.state.pool = pool
    yield
    await pool.close()

# Bad with --preload:
pool = asyncio.get_event_loop().run_until_complete(create_async_pool())  # pre-fork, shared across all workers → invalid after fork
```

---

### Q2: What is `--graceful-timeout` in Gunicorn, and how do you tune it?

**Model answer:**

`--graceful-timeout` is the number of seconds Gunicorn waits for workers to finish their current requests before killing them during a graceful shutdown (triggered by `SIGTERM`). Default: 30 seconds.

After `SIGTERM`:
1. Workers stop accepting new connections
2. Gunicorn waits `graceful-timeout` seconds for in-flight requests to complete
3. Workers still running after the timeout receive `SIGKILL`

**Tuning:**
- Set to slightly more than your 99th percentile request latency
- For typical CRUD APIs (P99 < 1 second): 10s is generous
- For long-running operations (report generation, file processing): set to match maximum expected operation duration, or better — move long operations to a job queue

```bash
gunicorn myapp.main:app \
  -k uvicorn.workers.UvicornWorker \
  -w 4 \
  --timeout 60 \                # worker timeout (kills workers that haven't responded in 60s)
  --graceful-timeout 30 \       # wait 30s for in-flight requests on shutdown
  --keep-alive 5 \              # HTTP keep-alive timeout
  --bind 0.0.0.0:8000
```

**`--timeout` vs `--graceful-timeout`:**
- `--timeout`: kills a worker that hasn't sent any response for N seconds (stuck/deadlocked worker)
- `--graceful-timeout`: how long to wait on *planned* shutdown (SIGTERM)

---

### Q3: A Gunicorn worker is killed with `[CRITICAL] WORKER TIMEOUT`. What are the causes?

**Model answer:**

A worker timeout happens when the Gunicorn master sends a heartbeat signal (`SIGUSR1`) to the worker and the worker doesn't respond within `--timeout` seconds. This indicates the worker is stuck — typically due to:

**1. Blocking call on the event loop:**
A sync library call (`time.sleep()`, `requests.get()`, a sync ORM call in `async def`) blocking the event loop. The event loop can't respond to the Gunicorn heartbeat while blocked.

**2. Actual computation taking too long:**
CPU-bound route (image processing, ML inference, large data manipulation) running on the event loop for > `--timeout` seconds.

**3. Deadlock:**
Two coroutines waiting on each other (rare but possible with poorly designed async primitives).

**4. Infinite loop:**
A bug causing a coroutine to never `await` and never return.

**Diagnosis:**
```bash
# Check which routes are slow under load
py-spy dump --pid <gunicorn_worker_pid>
```

The `py-spy dump` gives a stack trace of the hanging process — this immediately shows whether it's blocked on I/O, in computation, or truly deadlocked.

**Quick fix:** increase `--timeout` to buy time for diagnosis. Long-term fix: move blocking code to `run_in_threadpool` or switch to async equivalents.

---

## Code: Production Gunicorn Config File

```python
# gunicorn.conf.py
import multiprocessing
import os

# Workers
workers = int(os.getenv("GUNICORN_WORKERS", multiprocessing.cpu_count() * 2 + 1))
worker_class = "uvicorn.workers.UvicornWorker"
worker_connections = 1000  # max concurrent connections per worker

# Timeouts
timeout = int(os.getenv("GUNICORN_TIMEOUT", 60))       # worker timeout
graceful_timeout = int(os.getenv("GRACEFUL_TIMEOUT", 30))
keepalive = 5

# Binding
bind = f"0.0.0.0:{os.getenv('PORT', '8000')}"

# Logging
loglevel = os.getenv("LOG_LEVEL", "info")
accesslog = "-"   # stdout
errorlog = "-"    # stderr
access_log_format = '%(h)s %(l)s %(u)s %(t)s "%(r)s" %(s)s %(b)s "%(f)s" "%(a)s" %(D)s'

# Process naming
proc_name = "myapp"

# Preload — set to True only if no async resources initialized at module level
preload_app = os.getenv("GUNICORN_PRELOAD", "false").lower() == "true"

# Hooks
def on_starting(server):
    server.log.info("Gunicorn master starting")

def worker_exit(server, worker):
    server.log.info(f"Worker {worker.pid} exiting")
```

```bash
# Run:
gunicorn myapp.main:app -c gunicorn.conf.py
```

---

## Under the Hood

Gunicorn's `UvicornWorker` (in `uvicorn/workers.py`) inherits from Gunicorn's `Worker` base class and overrides `run()` to start an asyncio event loop via `anyio.run()` (or directly via `asyncio.run()` in older versions). The worker:
1. Creates a fresh event loop
2. Starts a `uvicorn.Server` with the application
3. Runs until the Gunicorn master sends `SIGTERM` (graceful) or `SIGKILL` (forced)

The Gunicorn heartbeat mechanism: the master sends `SIGUSR1` to each worker periodically. Workers must "notify" the master (by writing to a pipe) to confirm they're alive. A blocking call that prevents the worker from running its event loop also prevents it from writing to the heartbeat pipe → master detects the worker as hung → kills and restarts it.
