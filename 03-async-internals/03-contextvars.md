# contextvars and Request-Scoped State Across Async Boundaries

## Concept

`contextvars.ContextVar` provides per-coroutine (task) local storage — the async equivalent of `threading.local()`. Each `asyncio.Task` gets its own copy of the context. A `ContextVar` set in one task is invisible to other tasks unless explicitly copied.

**How context propagates:**
- When a new `asyncio.Task` is created (via `asyncio.create_task()`, `TaskGroup.create_task()`, `asyncio.ensure_future()`), it gets a **copy** of the current context at creation time.
- Changes to ContextVars in the parent after the child task starts are NOT visible to the child.
- Changes in the child are NOT visible to the parent.

**FastAPI request lifecycle:** each request is handled within a single `asyncio.Task` (the Uvicorn connection handler dispatches to `app.__call__()` which is awaited inline, not in a new task). This means ContextVars set in middleware are visible in route handlers and dependencies — they share the same task context.

**BackgroundTasks:** these run in new tasks scheduled after the response is sent. They inherit a copy of the context from when the response was dispatched — so ContextVars set during request handling are visible in background tasks.

---

## Interview Questions

### Q1: Why use contextvars instead of passing request-scoped state through dependency injection?

**Model answer:**

Dependency injection is the right tool for most request-scoped state in FastAPI. But ContextVars solve different problems:

**When DI works poorly:**
1. **Deeply nested code that doesn't participate in FastAPI's DI**: third-party libraries, utilities, or domain services that don't know about FastAPI can't accept `Depends()` arguments. Passing a request ID through 5 layers of function calls "just for logging" is polluting those APIs.

2. **Middleware logging context**: middleware runs outside the DI system. Setting a `ContextVar` in middleware makes values like `request_id` or `user_id` available globally in that request's context — accessible in logging filters, distributed tracing, anywhere.

3. **Interop with non-FastAPI code**: if you have a shared library that reads a ContextVar for trace IDs or tenant IDs, you can set it in FastAPI middleware and it's automatically available.

```python
import contextvars
import logging

request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="")

class RequestIDFilter(logging.Filter):
    def filter(self, record):
        record.request_id = request_id_var.get()
        return True

# Middleware sets it:
class RequestIDMiddleware:
    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            request_id_var.set(str(uuid.uuid4()))
        await self.app(scope, receive, send)
```

The alternative (DI-based) would require adding `request_id: str = Depends(get_request_id)` to every function that needs it for logging — which is impractical at scale.

---

### Q2: A ContextVar set in middleware is not visible in a background task. How do you fix it?

**Model answer:**

By default, a background task created by FastAPI runs in a new asyncio task. If the ContextVar is set *before* the background task is scheduled, the child task inherits the value (because Python copies the context at task creation). But if the ContextVar is set *after* the background task is scheduled, the child task won't see the new value.

FastAPI schedules `BackgroundTasks` *after* the route handler returns and the response is built. ContextVars set during request handling (in middleware, in the route) ARE visible in background tasks because the tasks are created after the context is established.

**The problem case:** if your middleware sets the ContextVar asynchronously AFTER the response starts flowing:

```python
# Fragile: ContextVar might not be set when background task starts
class BadMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response = await call_next(request)  # background tasks may start here
        request_id_var.set(str(uuid.uuid4()))  # set after task creation
        return response
```

**The fix:** set ContextVars BEFORE calling into the inner app:

```python
class GoodMiddleware:
    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            request_id_var.set(str(uuid.uuid4()))  # set before inner app runs
        await self.app(scope, receive, send)
```

For explicit control, use `contextvars.copy_context()` to snapshot and propagate:

```python
ctx = contextvars.copy_context()

async def background_with_context():
    # Runs ctx (the snapshot) explicitly
    ctx.run(lambda: request_id_var.set("explicit"))
    # ... do work
    
asyncio.create_task(background_with_context())
```

---

### Q3: How do ContextVars interact with `run_in_threadpool`?

**Model answer:**

When `run_in_threadpool(func)` calls `anyio.to_thread.run_sync(func)`, anyio copies the current context to the thread via `contextvars.copy_context().run()`. This means ContextVars set on the event loop ARE visible inside the sync function running in the thread pool.

```python
tenant_var: ContextVar[str] = ContextVar("tenant")

def sync_db_query():
    # This works — tenant_var.get() returns the value set in the async context
    tenant = tenant_var.get()
    return f"query for tenant {tenant}"

@app.get("/data")
async def get_data():
    tenant_var.set("acme-corp")
    result = await run_in_threadpool(sync_db_query)
    return {"result": result}
```

**Important nuance:** the thread gets a *copy* of the context. Mutations inside the thread (via `var.set()`) are NOT visible back in the async context after `run_in_threadpool` returns. This is copy-on-write context isolation.

This is the correct behavior for tenant isolation, request ID propagation, and similar patterns. Set the ContextVar before dispatching to threads; reads in threads work automatically.

---

## Code: Complete Request Context Pattern

```python
import contextvars
import logging
import uuid
from typing import Any

from fastapi import FastAPI, Request, Depends
from starlette.types import ASGIApp, Scope, Receive, Send

# Module-level ContextVars — shared across the codebase
request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "request_id", default=""
)
user_id_var: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "user_id", default=None
)
tenant_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "tenant", default="default"
)


# Logging filter that reads from ContextVars
class ContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        record.user_id = user_id_var.get()
        record.tenant = tenant_var.get()
        return True


# Raw ASGI middleware to set request_id
class RequestContextMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            # Use incoming header if present, otherwise generate
            headers = dict(scope.get("headers", []))
            incoming_id = headers.get(b"x-request-id", b"").decode()
            rid = incoming_id or str(uuid.uuid4())
            # Set BEFORE calling inner app so background tasks inherit it
            token = request_id_var.set(rid)
            scope.setdefault("state", {})["request_id"] = rid
            try:
                await self.app(scope, receive, send)
            finally:
                request_id_var.reset(token)  # clean up after request
        else:
            await self.app(scope, receive, send)


app = FastAPI()
app.add_middleware(RequestContextMiddleware)


# Dependency that sets user context (called after auth)
async def set_user_context(request: Request) -> None:
    # In production: extract from JWT
    user_id_var.set(42)


# Utility function — no FastAPI context, reads ContextVar directly
def get_audit_context() -> dict[str, Any]:
    return {
        "request_id": request_id_var.get(),
        "user_id": user_id_var.get(),
        "tenant": tenant_var.get(),
    }


@app.get("/items/", dependencies=[Depends(set_user_context)])
async def list_items(request: Request):
    audit = get_audit_context()  # reads ContextVars — works in any nested function
    return {"request_id": audit["request_id"], "user_id": audit["user_id"]}
```

---

## Under the Hood

Python's `contextvars` module is implemented in C (`Modules/_contextvarsmodule.c`). Each `asyncio.Task` is created with `contextvars.copy_context()` — a shallow copy of the calling context. This is O(1) for context objects with many vars (it's a hash map copy-on-write) and is called implicitly by `asyncio.Task.__init__()`.

`anyio.to_thread.run_sync()` uses `contextvars.copy_context().run(func)` to propagate the context to the thread. This is why ContextVars "just work" across thread pool calls — anyio handles the propagation.

`ContextVar.set()` returns a `Token` object that can be passed to `ContextVar.reset(token)` to undo the set. This is the correct pattern for middleware that needs to restore context after a request — more reliable than setting to an empty value, because it handles nested middleware correctly.
