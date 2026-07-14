# Middleware Order of Execution, Middleware Stack as Nested ASGI Apps

## Concept

Middleware in FastAPI/Starlette is implemented as nested ASGI applications. Each middleware wraps the next one in the stack. The "stack" is built once at startup; at request time, requests flow in and responses flow out through nested `__call__` invocations.

**`app.add_middleware(MiddlewareClass, **kwargs)`** prepends to the middleware list. The list is reversed during stack construction (in `build_middleware_stack()`). The result: **the last `add_middleware()` call creates the outermost wrapper**.

**Execution order for request:**  outer → inner → route  
**Execution order for response:** route → inner → outer

```
add_middleware(LoggingMW)      # added first → innermost
add_middleware(AuthMW)         # added second
add_middleware(RateLimitMW)    # added last → outermost

Request flow:  RateLimitMW → AuthMW → LoggingMW → ExceptionMW → Router
Response flow: Router → LoggingMW → AuthMW → RateLimitMW
```

**Fixed layers** (always present, built by Starlette):
- `ServerErrorMiddleware` (outermost)
- User middleware (in reverse add order)
- `ExceptionMiddleware` (innermost before Router)

---

## Interview Questions

### Q1: You have logging middleware and auth middleware. Which should be added first and why?

**Model answer:**

Add **logging middleware first** (it becomes innermost), **auth middleware second** (it becomes outermost).

**Rationale for outermost auth:**
- Auth failures should be caught before any other processing. An unauthenticated request should be rejected at the boundary without hitting logging, rate limiting, or the route.
- But wait — if auth is outermost, logging doesn't see auth failures. This is actually a problem.

**Real production answer:**
- **Rate limiting** → outermost (reject spam before any processing)
- **Auth** → second (reject invalid tokens before touching business logic)
- **Logging/tracing** → innermost (log every request including auth failures, with context)

```python
app.add_middleware(TracingMiddleware)   # first → innermost — logs everything
app.add_middleware(AuthMiddleware)      # auth before business logic
app.add_middleware(RateLimitMiddleware) # outermost — reject early
```

**Request flow:** `RateLimit → Auth → Tracing → route`  
**Effect:** rate limit blocks spam; auth blocks invalid tokens; tracing logs whatever reaches it (which is valid requests post-auth). If you want to log *all* requests including rejected ones, tracing needs to be outer than auth.

The correct order depends on your observability requirements. There's no universal answer — understand the flow and decide.

---

### Q2: How do you add middleware that only applies to a subset of routes?

**Model answer:**

FastAPI's `app.add_middleware()` applies globally — there's no per-route middleware. Options:

**1. Filter by path inside the middleware:**
```python
class PathFilteredMiddleware:
    def __init__(self, app, prefix: str):
        self.app = app
        self.prefix = prefix

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["path"].startswith(self.prefix):
            # Apply middleware logic
            ...
        await self.app(scope, receive, send)
```

**2. Use `APIRouter` dependencies** (for DI-style middleware behavior):
```python
# Dependency that acts like middleware for specific routes
router = APIRouter(dependencies=[Depends(require_admin_ip)])
```

**3. Mount a sub-application** with its own middleware stack:
```python
admin_app = FastAPI()
admin_app.add_middleware(AdminMiddleware)

app.mount("/admin", admin_app)
# AdminMiddleware only runs for /admin/* requests
```

Option 3 is the cleanest architectural approach for complex per-subsystem middleware, but comes with the sub-app isolation caveats (own dependency overrides, own lifespan, not in main OpenAPI schema).

---

### Q3: Explain a real-world bug caused by middleware order.

**Model answer:**

**CORS + Auth middleware ordering bug:**

```python
app.add_middleware(AuthMiddleware)  # added first → innermost
app.add_middleware(CORSMiddleware, allow_origins=["*"])  # added second → outermost
```

A browser preflight `OPTIONS` request arrives. The flow:
1. `CORSMiddleware` (outer) intercepts OPTIONS → returns `200` with CORS headers (correct)
2. BUT if the order is reversed:
   - `AuthMiddleware` (outer) sees the OPTIONS request → checks for Authorization header
   - No auth header on preflight → returns `401`
   - Browser gets 401 on preflight → CORS error for the actual request

**Fix:** CORS middleware must be outer than auth middleware:
```python
app.add_middleware(AuthMiddleware)      # first → inner
app.add_middleware(CORSMiddleware, ...) # second → outer (handles preflight before auth)
```

This is a frequently hit bug in FastAPI apps. The symptom is CORS errors only appearing when auth is enabled.

---

## Code: Middleware Stack with Correct Ordering

```python
import time
import uuid
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.gzip import GZipMiddleware
from starlette.types import ASGIApp, Scope, Receive, Send
from fastapi import FastAPI, Request
from starlette.datastructures import MutableHeaders

app = FastAPI()


class RateLimitMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # Outermost: reject overloaded clients immediately
        await self.app(scope, receive, send)


class RequestTracingMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = str(uuid.uuid4())
        scope.setdefault("state", {})["request_id"] = request_id
        start = time.perf_counter()

        async def timed_send(message: dict) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers.append("x-request-id", request_id)
            await send(message)

        await self.app(scope, receive, timed_send)
        # Log after request completes — duration available here


# Add order (first=innermost, last=outermost):
app.add_middleware(RequestTracingMiddleware)  # innermost: traces all valid requests
app.add_middleware(GZipMiddleware, minimum_size=1000)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://app.example.com"],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(RateLimitMiddleware)  # outermost: first to reject, last to respond

# Resulting stack (outer → inner):
# RateLimitMiddleware
#   CORSMiddleware        ← handles OPTIONS preflight before auth/tracing
#     GZipMiddleware
#       RequestTracingMiddleware
#         ExceptionMiddleware
#           Router
```

---

## Under the Hood

`build_middleware_stack()` in `starlette/applications.py`:

```python
middleware = (
    [Middleware(ServerErrorMiddleware, ...)]
    + self.user_middleware    # in the order they were appended (first-added = leftmost)
    + [Middleware(ExceptionMiddleware, ...)]
)
# Build by wrapping innermost first
app = self.router
for cls, options in reversed(middleware):  # reversed → last in list wraps first
    app = cls(app=app, **options)
return app
```

`reversed()` means: the last item in `middleware` list becomes the innermost (wraps `self.router` first). Since `user_middleware` is built by prepending (`insert(0, ...)`), the first-added middleware ends up at index 0 (last in the reversed iteration) → innermost.

In plain terms: `app.add_middleware(A)` then `app.add_middleware(B)` → B is outermost, A is innermost. The code wraps `router` in A first, then wraps that in B.
