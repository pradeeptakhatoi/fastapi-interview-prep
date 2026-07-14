# ASGI Spec Deep Dive: scope, receive, send

## Concept

ASGI (Asynchronous Server Gateway Interface) is the protocol between an ASGI server (Uvicorn, Hypercorn, Granian) and a Python application (FastAPI/Starlette). An ASGI app is any callable with the signature:

```python
async def app(scope: dict, receive: Callable, send: Callable) -> None:
    ...
```

- **`scope`**: a dict describing the connection. Keys vary by connection type.
- **`receive`**: an async callable that returns the next event from the client (a request body chunk, a WebSocket message, a disconnect notice).
- **`send`**: an async callable that sends events to the client (response start, response body, WebSocket message).

**Connection types** (the `scope["type"]` key):
- `"http"` — a single HTTP request/response
- `"websocket"` — a WebSocket connection lifecycle
- `"lifespan"` — startup/shutdown events from the ASGI server

**HTTP scope keys:**
```python
{
    "type": "http",
    "asgi": {"version": "3.0", "spec_version": "2.4"},
    "http_version": "1.1",  # or "2"
    "method": "GET",
    "path": "/items/",
    "raw_path": b"/items/",
    "query_string": b"q=hello",
    "root_path": "",
    "headers": [(b"host", b"example.com"), ...],  # list of (bytes, bytes) tuples
    "server": ("127.0.0.1", 8000),
    "client": ("127.0.0.1", 54321),
    "state": {},  # app-level state passed through
    "extensions": {"http.response.push": {}, ...},
}
```

**Event flow for HTTP:**
1. Server calls `app(scope, receive, send)`
2. App calls `await receive()` → gets `{"type": "http.request", "body": b"...", "more_body": False}`
3. App calls `await send({"type": "http.response.start", "status": 200, "headers": [...]})` 
4. App calls `await send({"type": "http.response.body", "body": b"...", "more_body": False})`
5. Return from `app()` — connection closes

---

## Interview Questions

### Q1: What does Starlette/FastAPI actually do when it receives an ASGI call?

**Model answer:**

FastAPI's `__call__` is Starlette's `__call__` (FastAPI inherits from Starlette). The entry point is `starlette/applications.py:Starlette.__call__()`:

```python
async def __call__(self, scope, receive, send):
    scope["app"] = self
    await self.middleware_stack(scope, receive, send)
```

`self.middleware_stack` is a chain of nested ASGI callables built at startup:

```
ServerErrorMiddleware
  └── ExceptionMiddleware
        └── user middleware 1
              └── user middleware 2
                    └── Router.__call__()
                          └── Route.handle()
                                └── your endpoint function
```

Each layer calls the next one's `__call__(scope, receive, send)`. The innermost layer (the `Router`) matches the path to a `Route`, which creates a Starlette `Request` object wrapping `scope`/`receive`/`send`, runs the route handler, and calls `send()` with the response.

**The critical insight:** at the ASGI level, everything is a coroutine passing `scope`, `receive`, and `send` through a chain. Middleware is just a wrapper that intercepts these calls. The request body isn't read until someone calls `await receive()` — which Pydantic body parsing does via `await request.body()`.

---

### Q2: Why can't BaseHTTPMiddleware read a streaming request body, and how do you fix it?

**Model answer:**

`BaseHTTPMiddleware` (from Starlette) buffers the entire response body to pass it to the `dispatch()` method. This is implemented by wrapping `send` with an internal buffer. The fundamental problem:

1. `dispatch()` calls `response = await call_next(request)`
2. `call_next()` starts the inner app in a background task and waits for it to produce a response
3. The inner app reads the request body via `receive`, processes it, and calls `send()` with the response
4. `BaseHTTPMiddleware`'s wrapped `send` buffers the response body

**Problem with streaming responses:**
`StreamingResponse` in the inner app yields body chunks lazily. But `BaseHTTPMiddleware`'s buffering requires *consuming all chunks* before `dispatch()` can return the response. For large streaming responses (file downloads, SSE, etc.), this defeats the purpose of streaming — the entire response is buffered in memory.

**Problem with background tasks:**
If the route uses `BackgroundTasks`, those tasks are attached to the `Response` object. `BaseHTTPMiddleware` wraps the response in its own `Response`, losing the background tasks — they never execute.

**The fix:** write raw ASGI middleware:

```python
class RawMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        
        # Intercept send to inspect/modify response events
        async def modified_send(message):
            if message["type"] == "http.response.start":
                # Modify headers here
                headers = MutableHeaders(scope=message)
                headers["X-Custom"] = "value"
            await send(message)
        
        await self.app(scope, receive, modified_send)
```

Raw ASGI middleware passes `send` directly to the inner app — no buffering, no wrapping, streaming preserved.

**Gotcha follow-up:** Is there any case where `BaseHTTPMiddleware` is safe to use?

Yes — when you're only reading/modifying request data (path, headers, query string from `scope`) and not intercepting the response body. For simple auth checks, request ID injection, or logging that only reads the request, `BaseHTTPMiddleware` works fine. The problems only manifest when you use `call_next(request)` and manipulate the resulting response.

---

### Q3: Explain the lifespan ASGI event type and how FastAPI uses it.

**Model answer:**

The `lifespan` scope type is how ASGI servers signal application startup and shutdown. The protocol:

1. Server sends `{"type": "lifespan.startup"}`
2. App does initialization, then sends `{"type": "lifespan.startup.complete"}` (or `"lifespan.startup.failed"`)
3. Server starts handling requests
4. On shutdown, server sends `{"type": "lifespan.shutdown"}`
5. App cleans up, sends `{"type": "lifespan.shutdown.complete"}`

FastAPI (via Starlette) wraps this in the `@asynccontextmanager` lifespan parameter:

```python
from contextlib import asynccontextmanager
from fastapi import FastAPI

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: runs before lifespan.startup.complete is sent
    db_pool = await create_pool()
    app.state.db = db_pool
    
    yield  # Signal startup complete; server starts accepting requests
    
    # Shutdown: runs when lifespan.shutdown is received
    await db_pool.close()

app = FastAPI(lifespan=lifespan)
```

The old `@app.on_event("startup")` / `@app.on_event("shutdown")` decorators are deprecated in favor of the lifespan context manager, which is a single `asynccontextmanager` that cleanly scopes resource lifetime.

**Startup failure:** if an exception is raised before `yield` in the lifespan context manager, Starlette sends `lifespan.startup.failed` and the server process exits (or the worker is killed). This prevents requests from hitting an application in a broken state.

---

### Q4: What's the difference between `scope["state"]` and `app.state`?

**Model answer:**

- **`app.state`**: a `State` object on the `FastAPI` instance. Set at startup (e.g., in lifespan). Shared across **all requests and all workers of the same process**. This is for truly global, process-level state: connection pools, ML models, config objects.

- **`scope["state"]`**: a dict that travels with the request's scope through the ASGI call chain. It's per-request state that middleware and the app can mutate as the request passes through them. Starlette exposes it as `request.state`.

```python
# Middleware sets per-request state:
class RequestIDMiddleware:
    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            scope["state"]["request_id"] = str(uuid4())
        await self.app(scope, receive, send)

# Route reads it:
@app.get("/")
async def route(request: Request):
    rid = request.state.request_id  # set by middleware
    ...
```

`request.state` is completely isolated per request and does not need thread safety. `app.state` is shared and must only be written to during startup (when there's one writer). Reading during requests is fine — it's effectively read-only once the app is initialized.

---

## Code: Minimal Raw ASGI Application

```python
import json

# The simplest possible ASGI app — no FastAPI, no Starlette
async def minimal_asgi_app(scope, receive, send):
    if scope["type"] == "lifespan":
        while True:
            event = await receive()
            if event["type"] == "lifespan.startup":
                # Initialize resources here
                await send({"type": "lifespan.startup.complete"})
            elif event["type"] == "lifespan.shutdown":
                # Clean up resources here
                await send({"type": "lifespan.shutdown.complete"})
                return
    
    elif scope["type"] == "http":
        # Read the full request body
        body = b""
        while True:
            event = await receive()
            body += event.get("body", b"")
            if not event.get("more_body", False):
                break
        
        method = scope["method"]
        path = scope["path"]
        
        response_body = json.dumps({
            "method": method,
            "path": path,
            "body_length": len(body),
        }).encode()
        
        await send({
            "type": "http.response.start",
            "status": 200,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(response_body)).encode()),
            ],
        })
        await send({
            "type": "http.response.body",
            "body": response_body,
            "more_body": False,
        })
    
    elif scope["type"] == "websocket":
        await send({"type": "websocket.close", "code": 1000})
```

---

## Under the Hood

Uvicorn's implementation: when a connection arrives, `uvicorn/protocols/http/h11_impl.py` or `h2_impl.py` creates an ASGI cycle and calls `await app(scope, receive, send)`. The `receive` callable is a coroutine that awaits data from the underlying transport; `send` immediately writes to the transport. Uvicorn manages the event loop and I/O multiplexing via `asyncio` (or `uvloop`), but the ASGI app sees only the clean `scope`/`receive`/`send` interface.

This clean separation means any ASGI app can run on any ASGI server without modification — Uvicorn, Hypercorn, Granian, Daphne all expose the same interface.
