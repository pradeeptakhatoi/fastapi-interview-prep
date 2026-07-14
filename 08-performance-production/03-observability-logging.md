# FastAPI Observability and Logging

## Concept

Observability in production FastAPI means three signals working together:

- **Logs** — discrete events (request received, error raised, query executed). Structured JSON for machine parsing.
- **Metrics** — aggregated numbers over time (request rate, latency percentiles, error rate, DB pool utilization). Pulled by Prometheus or pushed to StatsD/Datadog.
- **Traces** — causal chains across services and async boundaries (a single request's path from FastAPI → DB → Redis → external API). OpenTelemetry is the standard.

**The FastAPI-specific challenges:**

1. Python's `logging` module is synchronous. Calling it from `async def` handlers blocks the event loop if the handler flushes to disk (rare in practice — `StreamHandler` is fast enough — but worth knowing).
2. Correlation IDs (request IDs, trace IDs) must propagate across `ContextVar` boundaries, thread pool calls, and background tasks — not just be passed as function arguments.
3. Uvicorn's access log format is not structured JSON — in production you want to replace it.
4. OpenTelemetry auto-instrumentation patches SQLAlchemy, httpx, Redis clients — understanding what it instruments (and what it misses) prevents gaps in traces.

---

## Interview Questions

### Q1: How do you implement structured JSON logging in a FastAPI app that includes request context (request ID, user ID, path) on every log line?

**Model answer:**

The pattern combines `ContextVar` for request-scoped state with a custom `logging.Filter` that reads from those vars:

```python
# logging_config.py
import json
import logging
import time
from contextvars import ContextVar

# Request-scoped context vars — set in middleware, read by logging filter
request_id_var: ContextVar[str] = ContextVar("request_id", default="-")
user_id_var: ContextVar[str] = ContextVar("user_id", default="-")
route_var: ContextVar[str] = ContextVar("route", default="-")


class ContextFilter(logging.Filter):
    """Injects per-request context into every log record."""
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        record.user_id = user_id_var.get()
        record.route = route_var.get()
        return True


class JSONFormatter(logging.Formatter):
    """Outputs log records as single-line JSON — parseable by Datadog/Splunk/CloudWatch."""
    def format(self, record: logging.LogRecord) -> str:
        log_entry = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(record.created)),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "request_id": getattr(record, "request_id", "-"),
            "user_id": getattr(record, "user_id", "-"),
            "route": getattr(record, "route", "-"),
        }
        if record.exc_info:
            log_entry["exc"] = self.formatException(record.exc_info)
        # Include any extra= kwargs passed to logger.info("...", extra={...})
        for key, val in record.__dict__.items():
            if key not in logging.LogRecord.__dict__ and key not in log_entry:
                log_entry[key] = val
        return json.dumps(log_entry, default=str)


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler()
    handler.addFilter(ContextFilter())
    handler.setFormatter(JSONFormatter())

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)

    # Quieten noisy third-party loggers
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
```

```python
# middleware: set ContextVars before inner app runs
import uuid
from starlette.types import ASGIApp, Scope, Receive, Send

class LoggingContextMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = (
            dict(scope.get("headers", [])).get(b"x-request-id", b"")
            .decode() or str(uuid.uuid4())
        )
        token_rid = request_id_var.set(request_id)
        token_route = route_var.set(scope.get("path", "-"))

        try:
            await self.app(scope, receive, send)
        finally:
            request_id_var.reset(token_rid)
            route_var.reset(token_route)
```

Now every `logger.info("...")` anywhere in the codebase — in services, deps, ORM callbacks — automatically includes `request_id` and `route`. No argument passing required.

**Gotcha follow-up:** Does this work inside background tasks?

Yes, if ContextVars are set before the background task is scheduled. Python copies the current context to new `asyncio.Task` objects at creation time. Background tasks are scheduled after the route handler returns — by which point `request_id_var` is set. The task inherits the value. Mutations in the task don't flow back to the parent.

---

### Q2: How do you replace Uvicorn's default access log with a structured JSON access log?

**Model answer:**

Uvicorn's access log format (`%(h)s %(l)s "%(r)s" %(s)s %(b)s`) is not JSON. In production you want `{"method": "GET", "path": "/items/", "status": 200, "duration_ms": 12.3}`.

**Option 1: Disable Uvicorn access log and emit your own in middleware:**

```python
# gunicorn.conf.py / uvicorn launch:
# --no-access-log

# Middleware that logs the access event in JSON
import time, logging

logger = logging.getLogger("access")

class AccessLogMiddleware:
    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        start = time.perf_counter()
        status_holder = [0]

        async def capture_send(message):
            if message["type"] == "http.response.start":
                status_holder[0] = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, capture_send)
        finally:
            duration_ms = (time.perf_counter() - start) * 1000
            logger.info(
                "request",
                extra={
                    "method": scope.get("method"),
                    "path": scope.get("path"),
                    "status": status_holder[0],
                    "duration_ms": round(duration_ms, 2),
                    "client": (scope.get("client") or [""])[0],
                },
            )
```

**Option 2: Override Uvicorn's access logger formatter:**

```python
# In your lifespan or startup:
import logging
uvicorn_access = logging.getLogger("uvicorn.access")
uvicorn_access.handlers[0].setFormatter(JSONFormatter())
```

Option 1 gives you full control over the log schema and timing (you control when the log fires). Option 2 is a lighter touch but you're tied to Uvicorn's `%(...)s` formatting variables.

---

### Q3: How do you integrate OpenTelemetry tracing into a FastAPI app, and what does it instrument automatically vs. what requires manual spans?

**Model answer:**

```bash
pip install opentelemetry-sdk opentelemetry-instrumentation-fastapi \
            opentelemetry-instrumentation-sqlalchemy \
            opentelemetry-instrumentation-httpx \
            opentelemetry-exporter-otlp
```

**Auto-instrumented (zero code changes):**
- FastAPI route entry/exit (span per request with method, path, status)
- SQLAlchemy queries (span per query with sanitized SQL)
- httpx outbound requests (span per external HTTP call)
- Redis commands via `opentelemetry-instrumentation-redis`

```python
from contextlib import asynccontextmanager
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from fastapi import FastAPI

def setup_tracing(service_name: str, otlp_endpoint: str) -> None:
    provider = TracerProvider()
    provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=otlp_endpoint))
    )
    trace.set_tracer_provider(provider)

@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_tracing("my-fastapi-service", "http://otel-collector:4317")
    FastAPIInstrumentor.instrument_app(app)
    SQLAlchemyInstrumentor().instrument(engine=engine)
    HTTPXClientInstrumentor().instrument()
    yield
    FastAPIInstrumentor().uninstrument_app(app)

app = FastAPI(lifespan=lifespan)
```

**Requires manual spans:**
- Business logic boundaries ("payment processing", "image resize", "cache lookup")
- Background tasks (auto-instrumentation doesn't trace across the task boundary)
- Celery/ARQ tasks running in separate processes
- Custom async operations not covered by an instrumentation library

```python
from opentelemetry import trace

tracer = trace.get_tracer(__name__)

async def process_payment(order_id: int, amount: float) -> dict:
    with tracer.start_as_current_span("process_payment") as span:
        span.set_attribute("order.id", order_id)
        span.set_attribute("payment.amount", amount)
        try:
            result = await charge_card(amount)
            span.set_attribute("payment.status", result["status"])
            return result
        except PaymentError as e:
            span.record_exception(e)
            span.set_status(trace.StatusCode.ERROR, str(e))
            raise
```

**Trace context propagation:** OpenTelemetry propagates trace context via `traceparent` HTTP header (W3C Trace Context standard). Incoming requests carry the parent span ID; outgoing httpx calls inject the current span ID. Background tasks need manual context propagation if they run in a different process.

---

### Q4: How do you expose Prometheus metrics from a FastAPI application without blocking the event loop?

**Model answer:**

Use `prometheus-fastapi-instrumentator` for automatic HTTP metrics, then add custom metrics for business KPIs:

```bash
pip install prometheus-fastapi-instrumentator prometheus-client
```

```python
from contextlib import asynccontextmanager
from fastapi import FastAPI
from prometheus_fastapi_instrumentator import Instrumentator
from prometheus_client import Counter, Histogram, Gauge
import time

# Custom business metrics
orders_created = Counter(
    "orders_created_total",
    "Total orders created",
    ["payment_method", "region"],
)
payment_duration = Histogram(
    "payment_processing_seconds",
    "Payment processing latency",
    buckets=[0.1, 0.25, 0.5, 1.0, 2.5, 5.0],
)
active_sse_connections = Gauge(
    "sse_active_connections",
    "Number of active SSE connections",
    ["topic"],
)

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Sets up /metrics endpoint and instruments all routes
    Instrumentator().instrument(app).expose(app, endpoint="/metrics")
    yield

app = FastAPI(lifespan=lifespan)


@app.post("/orders/")
async def create_order(order: OrderIn) -> dict:
    start = time.perf_counter()
    result = await process_payment(order)
    payment_duration.observe(time.perf_counter() - start)
    orders_created.labels(
        payment_method=order.payment_method,
        region=order.region,
    ).inc()
    return result
```

**Is Prometheus scraping blocking?** No. `prometheus_client` builds the metrics text response in memory (reading atomic counters). The `/metrics` route is a normal FastAPI route — it's served async and doesn't block other requests. The overhead per scrape is a few milliseconds of CPU to serialize all metric families.

**Multi-worker concern:** each Gunicorn worker process has its own in-memory metric counters. Prometheus scrapes one worker at a time (whichever handles the `/metrics` request). The result: counters appear inconsistent across scrapes — one scrape sees worker 1's counts, the next sees worker 2's.

**Fix:** use `prometheus_client`'s multiprocess mode with a shared directory:

```bash
PROMETHEUS_MULTIPROC_DIR=/tmp/prometheus_multiproc uvicorn myapp.main:app
```

```python
from prometheus_client import multiprocess, CollectorRegistry

def metrics_endpoint():
    registry = CollectorRegistry()
    multiprocess.MultiProcessCollector(registry)
    # Returns merged metrics from all worker processes
    ...
```

---

### Q5: What's the difference between error logging, exception tracking (Sentry), and distributed tracing for a production incident?

**Model answer:**

They answer different questions during an incident:

**Error logging** (`logger.exception("Payment failed", extra={"order_id": oid})`):
- What: "at 14:23:11, order 789 failed with PaymentGatewayError: card declined"
- Who: which service, which logger, which request
- Stored in: CloudWatch, Splunk, Datadog Logs
- Good for: tailing logs, searching for patterns, counting errors

**Sentry / error tracking:**
- What + context: full stack trace, local variables at each frame, release version, affected user count
- Groups similar errors into "issues" automatically
- Alerts on error rate spikes
- Good for: debugging specific errors, tracking error frequency, seeing which code paths fail

**Distributed traces (OpenTelemetry → Jaeger/Tempo/Honeycomb):**
- The full causal chain: `POST /orders` → `SQLAlchemy SELECT users` (2ms) → `httpx POST stripe.com` (450ms) → `SQLAlchemy INSERT orders` (3ms)
- Answers "where did the latency go?" for any individual request
- Good for: performance debugging, understanding slow P99s, finding which downstream service is the bottleneck

**In practice, use all three:**

```python
from contextlib import asynccontextmanager
import sentry_sdk
from sentry_sdk.integrations.fastapi import FastApiIntegration
from sentry_sdk.integrations.sqlalchemy import SqlalchemyIntegration

@asynccontextmanager
async def lifespan(app: FastAPI):
    sentry_sdk.init(
        dsn=settings.SENTRY_DSN,
        integrations=[FastApiIntegration(), SqlalchemyIntegration()],
        traces_sample_rate=0.1,   # 10% of requests get traces sent to Sentry
        send_default_pii=False,
    )
    yield
```

Sentry can double as both error tracking and lightweight tracing (via `traces_sample_rate`). For high-volume apps, use OpenTelemetry for full traces and Sentry only for errors (to avoid Sentry's per-event pricing at scale).

---

## Code: Production Observability Stack

```python
# observability.py — single module to set up all three pillars

import json
import logging
import time
import uuid
from contextlib import asynccontextmanager
from contextvars import ContextVar

from fastapi import FastAPI, Request
from opentelemetry import trace
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from prometheus_fastapi_instrumentator import Instrumentator
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Scope, Receive, Send

# --- ContextVars ---
request_id_var: ContextVar[str] = ContextVar("request_id", default="-")
user_id_var: ContextVar[str] = ContextVar("user_id", default="-")


# --- Structured JSON logging ---
class ContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        record.user_id = user_id_var.get()
        return True


class JSONFormatter(logging.Formatter):
    SKIP_FIELDS = frozenset(logging.LogRecord.__dict__) | {
        "message", "asctime", "args",
    }

    def format(self, record: logging.LogRecord) -> str:
        record.message = record.getMessage()
        entry: dict = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.message,
            "request_id": getattr(record, "request_id", "-"),
            "user_id": getattr(record, "user_id", "-"),
        }
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)
        for k, v in record.__dict__.items():
            if k not in self.SKIP_FIELDS and k not in entry:
                entry[k] = v
        return json.dumps(entry, default=str)


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler()
    handler.addFilter(ContextFilter())
    handler.setFormatter(JSONFormatter())
    logging.root.handlers = [handler]
    logging.root.setLevel(level)
    logging.getLogger("uvicorn.access").propagate = False  # silence default access log


# --- Access log + request ID middleware ---
class ObservabilityMiddleware:
    """Sets request_id ContextVar and emits structured access log."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self._logger = logging.getLogger("access")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers", []))
        request_id = headers.get(b"x-request-id", b"").decode() or str(uuid.uuid4())

        token = request_id_var.set(request_id)
        scope.setdefault("state", {})["request_id"] = request_id

        start = time.perf_counter()
        status_holder = [0]

        async def send_with_id(message: dict) -> None:
            if message["type"] == "http.response.start":
                status_holder[0] = message["status"]
                mh = MutableHeaders(scope=message)
                mh.append("x-request-id", request_id)
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        finally:
            duration_ms = (time.perf_counter() - start) * 1000
            self._logger.info(
                "request",
                extra={
                    "method": scope.get("method"),
                    "path": scope.get("path"),
                    "status": status_holder[0],
                    "duration_ms": round(duration_ms, 2),
                },
            )
            request_id_var.reset(token)


# --- App assembly ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()

    # Tracing
    provider = TracerProvider()
    provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint="http://otel-collector:4317"))
    )
    trace.set_tracer_provider(provider)
    FastAPIInstrumentor.instrument_app(app)

    # Metrics
    Instrumentator().instrument(app).expose(app, endpoint="/metrics", include_in_schema=False)

    yield


app = FastAPI(lifespan=lifespan)
app.add_middleware(ObservabilityMiddleware)


# Health check — excluded from access logs if desired
@app.get("/health", include_in_schema=False)
async def health() -> dict:
    return {"status": "ok"}


# Example route — request_id visible in all logs emitted here
logger = logging.getLogger(__name__)

@app.get("/items/{item_id}")
async def get_item(item_id: int, request: Request) -> dict:
    logger.info("fetching item", extra={"item_id": item_id})
    # Logs: {"ts": "...", "level": "INFO", "msg": "fetching item",
    #         "request_id": "abc-123", "user_id": "-", "item_id": 42}
    return {"id": item_id}
```

---

## Under the Hood

**Python logging and the event loop:** `logging.StreamHandler` writes to `sys.stderr` via `stream.write()` + `stream.flush()`. For `stderr` backed by a pipe (Docker, systemd), `write()` is typically a non-blocking `write(2)` syscall that completes in microseconds — not enough to stall the event loop in practice. For file-backed handlers (`FileHandler`), a slow disk can block for milliseconds. Production deployments should log to stdout/stderr (captured by the container runtime) rather than directly to files.

**OpenTelemetry context propagation in asyncio:** the OTel Python SDK stores the active span in a `ContextVar`. When `FastAPIInstrumentor` wraps a route, it reads the `traceparent` header, creates a child span, and sets it as the active span via `ContextVar.set()`. Because asyncio tasks inherit context, any span created inside the route's coroutine is automatically a child of the route span — including spans created inside dependencies and service functions. The span is finished in a `finally` block after the route handler returns.

**Prometheus and asyncio:** `prometheus_client` uses `threading.Lock` for metric updates (not asyncio locks). This is safe — the GIL ensures atomic increments for CPython, and `threading.Lock` is non-blocking for the common uncontested case. The `/metrics` endpoint calls `generate_latest()` which reads all registered metric families into a bytes buffer — no I/O, pure CPU, completes in < 5ms even with hundreds of metric series.
