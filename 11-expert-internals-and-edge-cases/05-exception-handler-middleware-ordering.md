# Exception Handlers and Middleware Order

## Concept

Exception handling in FastAPI is not a simple try/except. It's a middleware layer (`ExceptionMiddleware`) positioned within the ASGI middleware stack. Understanding where it sits determines which exceptions it can catch and which it can't.

**Default Starlette middleware stack (innermost to outermost):**
```
Request arrives
  → ServerErrorMiddleware         (outermost — catches everything that escapes below)
    → [user middleware added via app.add_middleware(), in reverse add order]
      → ExceptionMiddleware       (registered exception handlers live here)
        → Router.__call__()
          → Route.handle()
            → endpoint()
```

`ServerErrorMiddleware`: converts unhandled exceptions to 500 responses. In debug mode, shows a traceback. In production, returns a plain "Internal Server Error."

`ExceptionMiddleware`: holds the dict of registered exception handlers. When an exception propagates up from the inner ASGI call, it checks `isinstance(exc, registered_type)` for each handler.

**Key consequence:** user middleware added via `app.add_middleware()` sits **between** `ServerErrorMiddleware` and `ExceptionMiddleware`. Exceptions raised in user middleware do NOT go through `ExceptionMiddleware` — they're only caught by `ServerErrorMiddleware` (returning 500).

---

## Interview Questions

### Q1: Your custom exception handler for `MyError` isn't firing — the client gets 500 instead of your custom response. What are the likely causes?

**Model answer:**

**Cause 1: Exception raised in middleware (not in the route)**

If `MyError` is raised in a middleware that sits *outside* `ExceptionMiddleware` in the stack, the handler registered with `@app.exception_handler(MyError)` won't see it. Only exceptions from the route/inner app pass through `ExceptionMiddleware`.

```python
class BadMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        raise MyError("broken")  # bypasses ExceptionMiddleware → 500

app.add_middleware(BadMiddleware)  # added OUTSIDE ExceptionMiddleware
```

**Fix:** catch the exception in the middleware itself and return a proper Response.

**Cause 2: Exception type doesn't match registered type**

`isinstance()` check fails. Maybe a different `MyError` class is being imported in two places (common in large apps with circular import workarounds using local imports).

```python
# handler registered for myapp.errors.MyError
# but route raises mymodule.errors.MyError (different class object)
```

**Cause 3: Another exception handler is swallowing it**

A broader handler registered before the specific one. FastAPI checks handlers in registration order? No — it checks by type, most-specific first using MRO. But if a parent class handler returns a Response without re-raising, the `MyError` handler is never consulted.

**Cause 4: `HTTPException` handler catches it first**

If `MyError` inherits from `HTTPException` (or `StarletteHTTPException`), FastAPI's default `HTTPException` handler fires. Register a handler specifically for `MyError` and it will win over the parent class handler (FastAPI uses the most-specific type).

---

### Q2: What's the difference between registering an exception handler for `HTTPException` vs `StarletteHTTPException`?

**Model answer:**

`fastapi.HTTPException` inherits from `starlette.exceptions.HTTPException` (aliased as `StarletteHTTPException` in FastAPI). They are the same class chain.

When you register `@app.exception_handler(HTTPException)`, FastAPI installs it as the handler for `StarletteHTTPException` as well, because FastAPI internally remaps the `HTTPException` key to `StarletteHTTPException`. This means your handler also catches Starlette's own 404/405 responses.

```python
# These are equivalent in practice:
@app.exception_handler(HTTPException)
@app.exception_handler(StarletteHTTPException)
```

The nuance: if you want to handle FastAPI's `HTTPException` but NOT Starlette's routing 404, you need to check `exc.status_code` inside the handler rather than registering separate handlers.

```python
from starlette.exceptions import HTTPException as StarletteHTTPException

@app.exception_handler(StarletteHTTPException)
async def http_error_handler(request: Request, exc: StarletteHTTPException):
    if exc.status_code == 404 and "path" not in str(request.url):
        # Route-level 404 from Starlette router
        ...
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
```

---

### Q3: How do you ensure cleanup code runs even when an exception bypasses your exception handlers?

**Model answer:**

Use `yield` dependencies for resource cleanup — they always run via `AsyncExitStack`, even if exceptions bypass the exception handler:

```python
async def managed_resource():
    resource = await acquire_resource()
    try:
        yield resource
    finally:
        await resource.release()  # ALWAYS runs, regardless of exception path
```

For cleanup that must happen even when exceptions occur in middleware:

```python
class CleanupMiddleware:
    async def __call__(self, scope, receive, send):
        resource = await acquire()
        try:
            await self.app(scope, receive, send)
        finally:
            await resource.release()  # runs even if inner app raises
```

The `finally` in a raw ASGI middleware is the safest pattern. The `yield` dependency's `finally` is also reliable but only covers exceptions from within the ASGI app (not middleware failures above `ExceptionMiddleware`).

---

### Q4: Why does adding middleware via `app.add_middleware()` in a specific order affect exception handler behavior?

**Model answer:**

`app.add_middleware()` prepends to the middleware list. The middleware added *last* is the *outermost* wrapper. Middleware added *first* is *innermost* (closest to the route).

```python
app.add_middleware(AuthMiddleware)    # added first → innermost
app.add_middleware(LoggingMiddleware) # added last → outermost
```

Stack (outermost → innermost):
```
ServerErrorMiddleware
  LoggingMiddleware
    AuthMiddleware
      ExceptionMiddleware
        Router
```

An exception in `LoggingMiddleware` is not caught by `ExceptionMiddleware` (it's outside). An exception in `AuthMiddleware` is also not caught by `ExceptionMiddleware` if `AuthMiddleware` sits outside it.

**The subtle trap:** `ExceptionMiddleware` is added by Starlette *during application startup* before user middleware. But `app.add_middleware()` wraps *around* the existing stack. So ALL user middleware sits outside `ExceptionMiddleware`. Exceptions in user middleware are only caught by `ServerErrorMiddleware`.

This is why authentication middleware that raises `HTTPException` for invalid tokens doesn't always trigger the custom `HTTPException` handler — it depends on whether the middleware uses `BaseHTTPMiddleware` (which has its own exception handling) or raw ASGI.

---

## Code: Exception Handler Registration and Stack Visualization

```python
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import BaseHTTPMiddleware


app = FastAPI()


# Custom domain exception
class DatabaseError(Exception):
    def __init__(self, message: str, query: str):
        self.message = message
        self.query = query


# Exception handlers — registered on ExceptionMiddleware
@app.exception_handler(DatabaseError)
async def db_error_handler(request: Request, exc: DatabaseError) -> JSONResponse:
    return JSONResponse(
        status_code=503,
        content={"error": "DATABASE_ERROR", "message": exc.message},
    )


@app.exception_handler(RequestValidationError)
async def validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={"error": "VALIDATION", "fields": exc.errors()},
    )


@app.exception_handler(HTTPException)
async def http_handler(request: Request, exc: HTTPException) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": f"HTTP_{exc.status_code}", "detail": exc.detail},
        headers=exc.headers,
    )


# This middleware sits OUTSIDE ExceptionMiddleware
# Exceptions here go to ServerErrorMiddleware → 500
class RequestSizeLimitMiddleware:
    def __init__(self, app, max_bytes: int = 1_000_000):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        body_size = 0

        async def limited_receive():
            nonlocal body_size
            event = await receive()
            if event["type"] == "http.request":
                body_size += len(event.get("body", b""))
                if body_size > self.max_bytes:
                    # Return 413 directly — cannot raise HTTPException here
                    # because ExceptionMiddleware won't see it
                    await send({
                        "type": "http.response.start",
                        "status": 413,
                        "headers": [(b"content-type", b"application/json")],
                    })
                    await send({
                        "type": "http.response.body",
                        "body": b'{"error": "REQUEST_TOO_LARGE"}',
                        "more_body": False,
                    })
                    # Signal inner app to stop
                    raise RuntimeError("body too large")  # caught by our own try/except below
            return event

        try:
            await self.app(scope, limited_receive, send)
        except RuntimeError:
            pass  # Already sent the 413 response


app.add_middleware(RequestSizeLimitMiddleware, max_bytes=5_000_000)


@app.get("/items/{item_id}")
async def get_item(item_id: int):
    if item_id < 0:
        raise DatabaseError("connection failed", query=f"SELECT * FROM items WHERE id={item_id}")
    if item_id == 0:
        raise HTTPException(status_code=404, detail="Not found")
    return {"id": item_id}
```

---

## Under the Hood

The middleware stack is built in `starlette/applications.py:build_middleware_stack()`:

```python
def build_middleware_stack(self) -> ASGIApp:
    debug = self.debug
    error_handler = None
    exception_handlers = {}
    
    for key, value in self.exception_handlers.items():
        if key in (500, Exception):
            error_handler = value
        else:
            exception_handlers[key] = value
    
    middleware = (
        [Middleware(ServerErrorMiddleware, handler=error_handler, debug=debug)]
        + self.user_middleware
        + [Middleware(ExceptionMiddleware, handlers=exception_handlers, debug=debug)]
    )
    
    app = self.router
    for cls, options in reversed(middleware):
        app = cls(app=app, **options)
    return app
```

The `reversed()` call is because middleware added via `add_middleware()` is prepended to `user_middleware` — reversing during stack construction means the last-added middleware becomes outermost. This is the source of much middleware ordering confusion.
