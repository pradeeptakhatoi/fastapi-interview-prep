# FastAPI with Celery and Async Task Queues

## Concept

FastAPI's `BackgroundTasks` is process-local and ephemeral — tasks die with the worker, can't be retried, and don't distribute across machines. A real task queue solves these problems:

| Feature | BackgroundTasks | Celery | ARQ | Redis Streams |
|---------|----------------|--------|-----|---------------|
| Survives process restart | No | Yes | Yes | Yes (in broker) |
| Retries on failure | No | Yes (configurable) | Yes | Manual |
| Distributed workers | No | Yes | Yes | Yes |
| Result storage | No | Yes (optional) | Yes | Manual |
| Scheduling (cron) | No | Yes (celery-beat) | Yes | No |
| Async-native | No | No (sync tasks) | Yes | Yes |
| Complexity | Low | High | Medium | Low |

**Celery** is the most widely deployed option. It uses a broker (Redis or RabbitMQ) to queue tasks and a result backend (Redis, DB) to store outcomes. Celery workers are *sync* — they run tasks in threads or processes, not on an asyncio event loop. Integrating with FastAPI requires careful handling of async code in Celery tasks.

**ARQ** (Async Redis Queue) is Python-native async — tasks are coroutines, workers are asyncio event loops. Simpler than Celery, Redis-only, ideal for async FastAPI codebases.

**Redis Streams** is the lowest-level option: append-only log with consumer groups, suitable for event-driven pipelines where you control the consumer code.

---

## Interview Questions

### Q1: How do you integrate Celery with a FastAPI application, and what are the session/connection sharing pitfalls?

**Model answer:**

The integration pattern: FastAPI routes submit tasks to Celery via `.delay()` or `.apply_async()`. Celery workers run independently, reading from the broker.

```python
# celery_app.py
from celery import Celery

celery = Celery(
    "myapp",
    broker="redis://localhost:6379/0",
    backend="redis://localhost:6379/1",
    include=["myapp.tasks"],
)

celery.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    task_track_started=True,
    task_acks_late=True,          # ack after task completes, not before (safer)
    worker_prefetch_multiplier=1, # one task at a time per worker (fair distribution)
    result_expires=3600,
)
```

```python
# tasks.py
from myapp.celery_app import celery
from myapp.database import SyncSessionLocal  # sync session for Celery workers

@celery.task(bind=True, max_retries=3, default_retry_delay=60)
def process_report(self, report_id: int) -> dict:
    try:
        with SyncSessionLocal() as db:  # each task gets its own session
            report = db.get(Report, report_id)
            result = generate_report(report)
            db.commit()
            return {"report_id": report_id, "status": "done"}
    except Exception as exc:
        raise self.retry(exc=exc)
```

```python
# FastAPI route — submits task, returns task ID immediately
@app.post("/reports/", status_code=202)
async def create_report(report: ReportIn) -> dict:
    # Save the report request to DB first
    async with AsyncSessionLocal() as db:
        report_obj = Report(**report.model_dump())
        db.add(report_obj)
        await db.commit()
        await db.refresh(report_obj)

    # Submit to Celery (sync call — safe in async context since it's fast)
    task = process_report.delay(report_obj.id)
    return {"task_id": task.id, "status_url": f"/reports/status/{task.id}"}


@app.get("/reports/status/{task_id}")
async def get_report_status(task_id: str) -> dict:
    from celery.result import AsyncResult
    result = AsyncResult(task_id, app=celery)
    return {
        "task_id": task_id,
        "status": result.status,        # PENDING, STARTED, SUCCESS, FAILURE, RETRY
        "result": result.result if result.ready() else None,
    }
```

**The session/connection pitfalls:**

1. **Never share async resources between FastAPI and Celery.** `AsyncSession`, `aioredis` connections, `httpx.AsyncClient` — these all require an event loop. Celery workers don't have one (or have their own). Create separate sync DB sessions in tasks.

2. **Don't pass SQLAlchemy model instances to tasks.** Celery serializes task arguments to JSON. An ORM object is not JSON-serializable and detaches from its session. Pass IDs only.

3. **Connection pool fragmentation.** If you use `--preload` with Gunicorn AND run Celery workers in the same process namespace, database connections from the FastAPI pool can be inherited by forked Celery workers — leading to connection sharing violations. Keep FastAPI and Celery workers as completely separate processes.

---

### Q2: How does ARQ differ from Celery architecturally, and when would you choose it for a FastAPI project?

**Model answer:**

**ARQ (Async Redis Queue):**
- Tasks are `async def` functions — they run on the ARQ worker's event loop
- Workers are asyncio programs — they can share async resources (DB pools, HTTP clients) efficiently
- Redis-only broker (no RabbitMQ support)
- Simpler API — no `@celery.task` decorator, no separate app object
- Built-in job deduplication via `job_id`
- No built-in periodic tasks (needs external scheduler like APScheduler)

```python
# tasks/report.py — async task, runs in ARQ worker
async def process_report(ctx: dict, report_id: int) -> dict:
    db: AsyncSession = ctx["db"]  # injected by worker startup
    report = await db.get(Report, report_id)
    result = await generate_report_async(report)
    return {"report_id": report_id, "status": "done"}
```

```python
# worker.py — ARQ worker startup
from arq import create_pool
from arq.connections import RedisSettings

async def startup(ctx):
    ctx["db"] = AsyncSession(engine)  # shared across tasks in this worker
    ctx["http"] = httpx.AsyncClient()

async def shutdown(ctx):
    await ctx["db"].close()
    await ctx["http"].aclose()

class WorkerSettings:
    functions = [process_report]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings(host="localhost")
```

```python
# FastAPI route — enqueue job
@app.post("/reports/", status_code=202)
async def create_report(report: ReportIn, request: Request) -> dict:
    redis = request.app.state.arq_pool
    job = await redis.enqueue_job("process_report", report_id=42)
    return {"job_id": job.job_id}
```

**Choose ARQ when:**
- Your codebase is already async-native (FastAPI + asyncpg + httpx)
- You want to share a single async DB pool between FastAPI and worker tasks (same engine, separate sessions)
- You don't need RabbitMQ, complex routing, or Celery-beat scheduling
- Lower operational complexity is valued over Celery's ecosystem size

**Choose Celery when:**
- You need RabbitMQ for guaranteed delivery / complex routing
- Your team already operates Celery in production
- You need celery-beat for complex scheduling
- Tasks call sync-only libraries (legacy ORMs, PDF generators) — Celery's sync nature is a feature

---

### Q3: How do you implement a job status polling pattern — 202 Accepted → poll for completion — correctly?

**Model answer:**

The pattern: route accepts the request and returns `202 Accepted` with a `Location` header pointing to the status endpoint. Client polls until status is `completed` or `failed`.

```python
from enum import StrEnum

class JobStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"

class JobResult(BaseModel):
    job_id: str
    status: JobStatus
    result: dict | None = None
    error: str | None = None
    created_at: datetime
    completed_at: datetime | None = None
```

**Implementation concerns:**

**1. Where to store status:** Celery result backend (Redis) works for simple cases. For richer status (progress percentage, partial results, error details), store in your own DB table alongside the task.

**2. Race condition on result retrieval:** if the client polls immediately after receiving 202, the task may not be in the broker yet. Status should start as `PENDING` (unknown to broker) and transition to `STARTED` when the worker picks it up.

**3. Expiry:** Celery results expire (default 1 day). After expiry, `AsyncResult.status` returns `PENDING` even for completed tasks — indistinguishable from "not yet started." Use `task_result_expires` or store results in your DB with a proper timestamp.

**4. Webhook alternative:** instead of client polling, the task POSTs to a client-provided callback URL on completion. More efficient but requires the client to be reachable.

```python
@app.post("/reports/", status_code=202)
async def create_report(
    report: ReportIn,
    request: Request,
    response: Response,
) -> dict:
    async with get_db_session() as db:
        # Create a DB record for the job — persistent status, not Celery-only
        job_record = JobRecord(
            status=JobStatus.PENDING,
            input=report.model_dump(),
            created_at=datetime.utcnow(),
        )
        db.add(job_record)
        await db.flush()
        job_id = str(job_record.id)

    # Enqueue — pass job_id so task can update our DB record
    process_report.delay(job_id=job_id, report_data=report.model_dump())

    response.headers["Location"] = f"/reports/status/{job_id}"
    return {"job_id": job_id, "status": "pending"}


@app.get("/reports/status/{job_id}", response_model=JobResult)
async def get_job_status(job_id: str) -> JobResult:
    async with get_db_session() as db:
        record = await db.get(JobRecord, job_id)
        if not record:
            raise HTTPException(status_code=404, detail="Job not found")
        return JobResult(
            job_id=job_id,
            status=record.status,
            result=record.result,
            error=record.error,
            created_at=record.created_at,
            completed_at=record.completed_at,
        )
```

---

### Q4: What are the failure modes of `task_acks_late=True` in Celery, and when is it essential?

**Model answer:**

By default (`task_acks_late=False`), Celery acknowledges (acks) the message from the broker as soon as the worker receives it — before the task runs. If the worker crashes mid-task, the task is gone.

`task_acks_late=True` defers the ack until the task completes. If the worker crashes, the broker redelivers the task to another worker.

**Failure modes of `task_acks_late=True`:**

**1. Duplicate execution on worker restart:** if the task completes but the ack hasn't been sent yet (worker killed after task body, before ack), the task runs again on redelivery. Your tasks must be **idempotent** — running twice produces the same outcome.

**2. Message visibility timeout:** Redis's broker doesn't have visibility timeouts (unlike SQS). If `task_acks_late=True` with Redis, a crashed worker causes the task to be requeued only after the worker's heartbeat expires (typically `broker_transport_options={'visibility_timeout': ...}`). The default Redis visibility timeout is 1 hour — tasks may be delayed that long after a crash.

**3. Interaction with `worker_prefetch_multiplier`:** if a worker prefetches multiple tasks (default `worker_prefetch_multiplier=4`), a crash causes all prefetched tasks to be redelivered. Setting `worker_prefetch_multiplier=1` limits blast radius.

**When `task_acks_late=True` is essential:**
- Tasks perform irreversible operations (payments, emails, external API calls that can't be undone)
- Correctness matters more than throughput
- Tasks are idempotent by design (always safe to retry)

**When to avoid it:**
- Non-idempotent tasks (generating unique IDs, incrementing counters)
- Extremely fast tasks where crash redelivery overhead is acceptable at default settings

---

## Code: Complete FastAPI + ARQ Integration

```python
# myapp/worker.py — ARQ worker definition
import asyncio
import httpx
from arq import create_pool
from arq.connections import RedisSettings
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from myapp.config import settings

engine = create_async_engine(settings.DATABASE_URL)
SessionFactory = async_sessionmaker(engine, expire_on_commit=False)


async def generate_report(ctx: dict, job_id: str, report_type: str) -> dict:
    """ARQ task — runs as a coroutine on the worker's event loop."""
    db: AsyncSession = ctx["db_session"]
    http: httpx.AsyncClient = ctx["http_client"]

    # Update status in DB
    async with SessionFactory() as db:
        job = await db.get(JobRecord, job_id)
        job.status = "running"
        await db.commit()

    try:
        # Async I/O in tasks — works because ARQ workers are async
        data = await http.get(f"https://data.example.com/{report_type}")
        result = process_data(data.json())

        async with SessionFactory() as db:
            job = await db.get(JobRecord, job_id)
            job.status = "completed"
            job.result = result
            job.completed_at = datetime.utcnow()
            await db.commit()

        return result
    except Exception as e:
        async with SessionFactory() as db:
            job = await db.get(JobRecord, job_id)
            job.status = "failed"
            job.error = str(e)
            await db.commit()
        raise


async def startup(ctx: dict) -> None:
    ctx["http_client"] = httpx.AsyncClient(timeout=30.0)


async def shutdown(ctx: dict) -> None:
    await ctx["http_client"].aclose()
    await engine.dispose()


class WorkerSettings:
    functions = [generate_report]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(settings.REDIS_URL)
    max_jobs = 10
    job_timeout = 300       # 5 minute task timeout
    keep_result = 3600      # keep job results for 1 hour


# myapp/main.py — FastAPI app
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, Response, HTTPException
from arq import create_pool
from arq.connections import RedisSettings
from pydantic import BaseModel
from datetime import datetime


class ReportRequest(BaseModel):
    report_type: str


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.arq = await create_pool(
        RedisSettings.from_dsn(settings.REDIS_URL)
    )
    yield
    await app.state.arq.close()


app = FastAPI(lifespan=lifespan)


@app.post("/reports/", status_code=202)
async def create_report(
    body: ReportRequest,
    request: Request,
    response: Response,
) -> dict:
    # Create DB record
    async with SessionFactory() as db:
        job = JobRecord(
            status="pending",
            report_type=body.report_type,
            created_at=datetime.utcnow(),
        )
        db.add(job)
        await db.flush()
        job_id = str(job.id)
        await db.commit()

    # Enqueue — job_id links the ARQ job to our DB record
    await request.app.state.arq.enqueue_job(
        "generate_report",
        job_id=job_id,
        report_type=body.report_type,
        _job_id=f"report:{job_id}",  # deduplication key
    )

    response.headers["Location"] = f"/reports/{job_id}/status"
    return {"job_id": job_id}


@app.get("/reports/{job_id}/status")
async def report_status(job_id: str) -> dict:
    async with SessionFactory() as db:
        job = await db.get(JobRecord, job_id)
        if not job:
            raise HTTPException(status_code=404)
        return {
            "job_id": job_id,
            "status": job.status,
            "result": job.result,
            "error": job.error,
            "created_at": job.created_at.isoformat(),
            "completed_at": job.completed_at.isoformat() if job.completed_at else None,
        }
```

```bash
# Run the ARQ worker:
arq myapp.worker.WorkerSettings

# Run FastAPI:
uvicorn myapp.main:app --reload
```

---

## Under the Hood

**Celery's task execution model:** each Celery worker process has a pool of execution slots (threads or processes, depending on `--pool prefork|eventlet|gevent|solo`). Tasks are deserialized from the broker message, executed in a pool slot, and the result is stored in the backend. Celery has no native asyncio support — `async def` tasks are wrapped and run via `asyncio.run()` in an executor, which creates a new event loop per task. This is inefficient and loses the ability to share async resources across tasks in the same worker.

**ARQ's execution model:** the ARQ worker is a single asyncio program. It connects to Redis and polls for jobs using `BRPOPLPUSH` (atomic pop from queue, push to in-progress list). Each job runs as a coroutine on the worker's event loop — `asyncio.gather()` or a `TaskGroup` runs multiple jobs concurrently up to `max_jobs`. The `ctx` dict is shared across all tasks in the worker, allowing one DB connection pool and one HTTP client to serve all concurrent tasks. This is the async-native design that Celery fundamentally can't replicate without a full rewrite.

**Redis Streams as an alternative:** `XADD` appends to a stream; consumer groups (`XREADGROUP`) allow multiple workers to consume from the same stream with at-least-once delivery. Unlike Celery/ARQ, there's no retry logic or result storage built in — you implement those yourself. Appropriate for high-throughput event pipelines where you own the consumer code and want zero external dependencies beyond Redis.
