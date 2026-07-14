# Raw ASGI Middleware vs BaseHTTPMiddleware

## Concept

FastAPI/Starlette offers two middleware patterns:

**`BaseHTTPMiddleware`**: a convenient class-based API that hides the ASGI protocol. You override `dispatch(request, call_next)` and work with `Request`/`Response` objects. Under the hood it uses `asyncio.Queue` to bridge the ASGI send/receive interface into synchronous-looking request/response objects. This bridging introduces:
- Response body buffering (all streaming is broken)
- Background task loss
- Context var propagation issues
- A hidden `anyio.TaskGroup` for streaming responses

**Raw ASGI middleware**: a class (or function) that directly implements `__call__(scope, receive, send)`. No buffering, no bridging, full control over the event stream.

**When to use each:**

| Use Case | BaseHTTPMiddleware | Raw ASGI |
|----------|--------------------|----------|
| Simple request header modification | ✅ | Works too |
| Response header modification | ✅ | ✅ |
| Request body parsing/validation | ✅ (buffered) | ✅ (streaming) |
| Streaming response passthrough | ❌ breaks streaming | ✅ |
| Background task preservation | ❌ loses tasks | ✅ |
| WebSocket handling | ❌ skips non-HTTP | ✅ (explicit) |
| Authentication (header check only) | ✅ | ✅ |

---

## Interview Questions

### Q1: Explain exactly why BaseHTTPMiddleware breaks streaming responses.

**Model answer:**

`BaseHTTPMiddleware.dispatch()` calls `await call_next(request)` to get a `Response` object. The `call_next()` implementation:

1. Creates an `asyncio.Queue` 
2. Starts the inner ASGI app in a background task, passing a `receive` wrapper and a `send` wrapper that puts events into the queue
3. Waits for the first `http.response.start` event from the queue
4. Returns a `StreamingResponse`-like `_CachedRequest` object

The problem: the `StreamingResponse` returned from `dispatch()` needs to be consumed by the response serialization layer *after* `dispatch()` returns. But the inner app may still be generating body chunks in its background task. This creates a race condition and breaks several invariants:

1. **Memory**: for a large file download, all `http.response.body` events accumulate in the queue until the client reads them — defeating streaming's memory efficiency
2. **Backpressure**: proper streaming relies on the client reading at a pace that matches generation. The queue breaks this flow-control
3. **Background tasks**: FastAPI attaches `BackgroundTasks` to the `Response` returned by the endpoint. When `BaseHTTPMiddleware` wraps this in a new `Response`, the `BackgroundTasks` attribute is lost — tasks never run

The `anyio.TaskGroup` in recent Starlette versions partially addresses some of these issues but the fundamental buffering problem remains for large payloads.

---

### Q2: Write a raw ASGI middleware that adds request ID tracing. What would break if you used BaseHTTPMiddleware instead?

**Model answer:**

```python
import uuid
from starlette.types import ASGIApp, Scope, Receive, Send
from starlette.datastructures import MutableHeaders


class RequestIDMiddleware:
    def __init__(self, app: ASGIApp, header_name: str = "X-Request-ID") -> None:
        self.app = app
        self.header_name = header_name.lower().encode()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        request_id = str(uuid.uuid4())
        # Store in scope state — available as request.state.request_id in routes
        scope.setdefault("state", {})
        scope["state"]["request_id"] = request_id

        async def send_with_request_id(message):
            if message["type"] == "http.response.start":
                # Add the request ID to the response headers
                headers = MutableHeaders(scope=message)
                headers.append(self.header_name.decode(), request_id)
            await send(message)

        await self.app(scope, receive, send_with_request_id)
```

**Registration:**
```python
app.add_middleware(RequestIDMiddleware, header_name="X-Request-ID")
```

**What BaseHTTPMiddleware would break here:**
Nothing, actually — this specific middleware only modifies headers, which is fine with `BaseHTTPMiddleware`. The streaming and background task problems only surface when you use `call_next()` and inspect/modify the response body, or when the inner route uses streaming or background tasks. A request-ID middleware that only reads request scope and adds a response header is a safe use case for `BaseHTTPMiddleware`.

The problematic pattern to avoid:
```python
class BadMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        body = b""
        async for chunk in response.body_iterator:  # BREAKS streaming
            body += chunk
        # process body...
        return Response(content=body, ...)  # background tasks LOST
```

---

### Q3: How do you correctly pass contextvars through raw ASGI middleware?

**Model answer:**

`contextvars.ContextVar` values are propagated to child tasks via `contextvars.copy_context()`. In raw ASGI middleware, if you set a ContextVar and then call the inner app, the value is visible to the inner app *within the same task* — no special handling needed for sequential calls.

The problem arises with `BackgroundTasks`: they run in a *separate* async task after the response is sent. By default, asyncio copies the context at task creation time (Python 3.7+), so ContextVars set before the background task is scheduled will be visible to it.

With raw ASGI middleware the flow is:
1. Middleware sets `request_ctx_var.set(value)`
2. Calls inner app `await self.app(scope, receive, send)` — same task, ContextVar visible
3. Inner app's route returns — `BackgroundTasks` are attached to the response
4. Background tasks are scheduled via `asyncio.ensure_future()` or `anyio.to_thread.run_sync()` — Python copies the current context to the new task
5. ContextVar is visible in the background task

The danger zone: if middleware creates a new task group and runs the inner app inside it:
```python
async with anyio.create_task_group() as tg:
    tg.start_soon(self.app, scope, receive, send)
```

The child task in the task group gets a copy of the context at the point `tg.start_soon()` is called — so ContextVars set before this call are visible, but those set after are not. This is a subtle bug in middleware that uses `anyio.TaskGroup` internally (which `BaseHTTPMiddleware` does).

The correct pattern: set all ContextVars **before** calling the inner app or creating child tasks.

---

### Q4: What's the correct way to handle WebSocket connections in raw ASGI middleware?

**Model answer:**

`scope["type"]` distinguishes connection types. WebSocket scopes have `type == "websocket"` and a different event protocol than HTTP. A middleware that only handles HTTP must pass WebSocket scopes through unmodified:

```python
async def __call__(self, scope, receive, send):
    if scope["type"] == "http":
        # HTTP-specific middleware logic
        await self.app(scope, receive, modified_send)
    else:
        # Pass through for "websocket" and "lifespan"
        await self.app(scope, receive, send)
```

For WebSocket-aware middleware:
```python
async def __call__(self, scope, receive, send):
    if scope["type"] == "websocket":
        # WebSocket events:
        # receive: {"type": "websocket.connect"}
        #          {"type": "websocket.receive", "text": ..., "bytes": ...}
        #          {"type": "websocket.disconnect", "code": 1000}
        # send:    {"type": "websocket.accept", "subprotocol": ..., "headers": [...]}
        #          {"type": "websocket.send", "text": ..., "bytes": ...}
        #          {"type": "websocket.close", "code": 1000}
        
        async def ws_receive():
            event = await receive()
            if event["type"] == "websocket.receive":
                # Log or validate WebSocket messages
                pass
            return event
        
        await self.app(scope, ws_receive, send)
    else:
        await self.app(scope, receive, send)
```

---

## Code: Production-Grade Raw ASGI Middleware

```python
import time
import uuid
import logging
from starlette.types import ASGIApp, Scope, Receive, Send
from starlette.datastructures import MutableHeaders

logger = logging.getLogger(__name__)


class ObservabilityMiddleware:
    """
    Request logging + timing + request-ID injection.
    Raw ASGI: preserves streaming, background tasks, WebSocket pass-through.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = str(uuid.uuid4())
        scope.setdefault("state", {})
        scope["state"]["request_id"] = request_id

        start = time.perf_counter()
        method = scope["method"]
        path = scope["path"]

        status_code_holder = [0]

        async def send_with_observability(message):
            if message["type"] == "http.response.start":
                status_code_holder[0] = message["status"]
                headers = MutableHeaders(scope=message)
                headers.append("x-request-id", request_id)
            elif message["type"] == "http.response.body" and not message.get("more_body"):
                elapsed_ms = (time.perf_counter() - start) * 1000
                logger.info(
                    "request completed",
                    extra={
                        "method": method,
                        "path": path,
                        "status": status_code_holder[0],
                        "duration_ms": round(elapsed_ms, 2),
                        "request_id": request_id,
                    },
                )
            await send(message)

        try:
            await self.app(scope, receive, send_with_observability)
        except Exception:
            elapsed_ms = (time.perf_counter() - start) * 1000
            logger.exception(
                "request failed with unhandled exception",
                extra={"method": method, "path": path, "duration_ms": round(elapsed_ms, 2)},
            )
            raise
```

```python
from fastapi import FastAPI

app = FastAPI()
app.add_middleware(ObservabilityMiddleware)

# Routes see request.state.request_id
@app.get("/")
async def root(request: Request):
    return {"request_id": request.state.request_id}
```

---

## Under the Hood

`BaseHTTPMiddleware.__call__()` in `starlette/middleware/base.py` creates a `_CachedRequest` (which wraps the ASGI `receive` callable) and a response future. It uses `anyio.create_task_group()` to run the inner app concurrently with consuming the response. This is why it works at all — without the task group, `call_next()` would deadlock (waiting for the response while the inner app is waiting for someone to consume its `send()` events).

The raw ASGI approach has no such complexity. `await self.app(scope, receive, modified_send)` is a single awaited call — the inner app runs to completion (including sending all response events through `modified_send`) before the middleware `__call__` returns. Streaming works because `modified_send` is called for each chunk in sequence, without buffering.
