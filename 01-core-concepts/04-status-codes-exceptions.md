# Status Codes, Custom Exception Handlers, HTTPException vs Custom Exceptions

## Concept

FastAPI's error handling is layered:

1. **`HTTPException`** — Starlette's built-in exception for HTTP errors. Raise it to return structured `{"detail": "..."}` JSON with a specific status code.
2. **Custom exception classes** — plain Python exceptions. Require an `@app.exception_handler()` to translate them into HTTP responses.
3. **`RequestValidationError`** — raised automatically by FastAPI when input validation fails (422). Can be overridden.
4. **`StarletteHTTPException`** — the Starlette base class that `HTTPException` inherits from. Overriding the handler for this catches all HTTP exceptions, including FastAPI's own 404/405.

The exception handler search order: FastAPI checks handlers from most-specific to least-specific type, using MRO. A handler registered for `HTTPException` catches `HTTPException` and its subclasses.

Status codes live in `fastapi.status` (re-exported from `starlette.status`) as constants like `HTTP_200_OK`, `HTTP_422_UNPROCESSABLE_ENTITY`. Using constants over integers makes code self-documenting and avoids typos.

---

## Interview Questions

### Q1: What's the difference between raising `HTTPException` and returning a `JSONResponse` with an error status code?

**Model answer:**

**`raise HTTPException(status_code=404, detail="Not found")`:**
- Control flow exits the route function immediately
- Starlette's `ExceptionMiddleware` catches it
- The registered exception handler (default or custom) formats the response
- Any `yield` dependency teardown still runs (the exception propagates through the dependency stack)

**`return JSONResponse(status_code=404, content={"detail": "Not found"})`:**
- Normal return from the route function
- `response_model` filtering is bypassed
- Background tasks still run (they're scheduled on normal return)
- Dependency teardown behaves as success (no exception propagated)

In practice: raise `HTTPException` for conditions that represent errors (wrong auth, not found, bad input). Return `JSONResponse` when you want to conditionally return different success-shaped responses or when you're managing background tasks that should run even on "soft errors."

**Gotcha follow-up:** Does raising `HTTPException` inside a `yield` dependency's teardown section behave the same way?

No. If you raise in the teardown (after `yield`), `ExceptionMiddleware` has already caught the original exception and is processing the response. A second exception from the dependency teardown will be logged but will not change the HTTP response — it effectively becomes an unhandled exception in a background context. Never raise `HTTPException` in dependency teardown; raise it only before `yield`, or log the error and swallow it after `yield`.

---

### Q2: How do you override the default 422 validation error response format?

**Model answer:**

Register a handler for `RequestValidationError` from `fastapi.exceptions`:

```python
from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

app = FastAPI()

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    errors = []
    for error in exc.errors():
        errors.append({
            "field": " -> ".join(str(loc) for loc in error["loc"]),
            "message": error["msg"],
            "type": error["type"],
        })
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={"errors": errors, "body": exc.body},
    )
```

`exc.errors()` returns a list of Pydantic `ErrorDetails` dicts. `exc.body` contains the raw request body that failed validation.

Note: if you register a handler for `StarletteHTTPException` (the base class), it overrides FastAPI's default handler for all HTTP exceptions including 404 and 405. Be specific about which exception you're handling.

---

### Q3: How do you create domain-specific exception classes and map them to HTTP responses?

**Model answer:**

Define plain Python exception classes and register handlers:

```python
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

app = FastAPI()


class AppError(Exception):
    def __init__(self, message: str, code: str):
        self.message = message
        self.code = code


class ResourceNotFoundError(AppError):
    pass


class PermissionDeniedError(AppError):
    pass


@app.exception_handler(ResourceNotFoundError)
async def not_found_handler(request: Request, exc: ResourceNotFoundError):
    return JSONResponse(
        status_code=404,
        content={"error": exc.code, "message": exc.message},
    )


@app.exception_handler(PermissionDeniedError)
async def permission_handler(request: Request, exc: PermissionDeniedError):
    return JSONResponse(
        status_code=403,
        content={"error": exc.code, "message": exc.message},
    )


@app.get("/items/{item_id}")
async def get_item(item_id: int):
    if item_id == 0:
        raise ResourceNotFoundError("Item does not exist", code="ITEM_NOT_FOUND")
    return {"id": item_id}
```

The advantage over `HTTPException` everywhere: business logic code raises domain errors without knowing about HTTP. The translation to HTTP concepts is in one place (the handlers), making it easy to change status codes or response shape without touching route functions.

---

### Q4: What happens to exception handlers when using `APIRouter`? Does a handler registered on the router propagate to the app?

**Model answer:**

No. `APIRouter` does **not** support `exception_handler` registration. The `@router.exception_handler()` decorator does not exist. Exception handlers must be registered on the `FastAPI` app instance itself:

```python
# This does NOT work:
router = APIRouter()
@router.exception_handler(MyError)  # AttributeError
async def handler(...): ...

# This is the correct pattern:
app = FastAPI()
@app.exception_handler(MyError)
async def handler(...): ...
```

The architectural implication: exception handler registration is global to the application. If you need per-router exception handling, use middleware or wrap routes in try/except within a dependency.

**Gotcha follow-up:** If you have a router included with a prefix and an exception handler on the main app, does the handler fire for exceptions raised in the router?

Yes. Exception handling happens at the middleware layer, not the routing layer. After a request is routed to a handler in any `APIRouter`, if an exception propagates, it bubbles up through middleware and is caught by `ExceptionMiddleware` which consults the app-level handlers. Router inclusion doesn't create isolation boundaries for exceptions.

---

### Q5: What's the behavior when both a middleware and an exception handler could handle the same exception?

**Model answer:**

It depends on the exception type and where it's raised:

- **Validation errors, `HTTPException`**: caught by `ExceptionMiddleware` (which is added by Starlette as part of the ASGI stack). App-level exception handlers are consulted.
- **Exceptions raised *inside* middleware**: depends on where in the middleware stack the exception occurs. An exception raised in `BaseHTTPMiddleware.dispatch()` *before* calling `call_next()` will propagate outward through middleware wrappers — outer middleware can catch it, but the app-level `ExceptionMiddleware` may not see it depending on stack order.

The general rule: **middleware executes outside-in** around the request. `ExceptionMiddleware` is added by Starlette near the inner end of the stack, so exceptions from routes and inner middleware reach it. Exceptions from *outer* middleware are not caught by `ExceptionMiddleware`.

This is why adding custom error logging middleware that wraps the entire stack (at `app.add_middleware()` level) needs its own try/except — it can't rely on `ExceptionMiddleware` catching its internal failures.

---

## Code: Robust Exception Handling Setup

```python
from fastapi import FastAPI, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel


app = FastAPI()


# Domain errors — HTTP-agnostic
class DomainError(Exception):
    status_code: int = 500
    error_code: str = "INTERNAL_ERROR"

    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


class NotFoundError(DomainError):
    status_code = 404
    error_code = "NOT_FOUND"


class ConflictError(DomainError):
    status_code = 409
    error_code = "CONFLICT"


class UnauthorizedError(DomainError):
    status_code = 401
    error_code = "UNAUTHORIZED"


# Single generic handler for all DomainErrors
@app.exception_handler(DomainError)
async def domain_error_handler(request: Request, exc: DomainError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": exc.error_code, "message": exc.message},
    )


# Override 422 format to match our error envelope
@app.exception_handler(RequestValidationError)
async def validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={
            "error": "VALIDATION_ERROR",
            "message": "Request validation failed",
            "fields": [
                {
                    "loc": list(e["loc"]),
                    "msg": e["msg"],
                    "type": e["type"],
                }
                for e in exc.errors()
            ],
        },
    )


# Override the generic HTTPException handler to match our envelope
@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": f"HTTP_{exc.status_code}", "message": exc.detail},
        headers=exc.headers,
    )


# Route using domain errors
@app.get("/users/{user_id}")
async def get_user(user_id: int):
    if user_id == 0:
        raise NotFoundError("User not found")
    if user_id < 0:
        raise UnauthorizedError("Access denied")
    return {"id": user_id, "name": "Alice"}
```

---

## Under the Hood

FastAPI's exception handling is wired in `fastapi/applications.py:build_middleware_stack()`. Starlette's `ExceptionMiddleware` is added to the middleware stack, and FastAPI populates it with all `@app.exception_handler()` registrations at startup.

At request time: `ExceptionMiddleware.__call__()` wraps the inner ASGI call in a try/except. If an exception matches a registered handler type (via `isinstance()` check), the handler coroutine is called and its `Response` is returned. If no handler matches, the exception propagates outward.

The `ServerErrorMiddleware` (outermost Starlette middleware) handles unhandled exceptions — returning a plain 500 in production and showing a debug page in `debug=True` mode.
