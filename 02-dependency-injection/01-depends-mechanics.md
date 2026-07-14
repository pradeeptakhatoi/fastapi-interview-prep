# Depends() Mechanics, Sub-dependencies, Dependency Caching

## Concept

`Depends()` is FastAPI's dependency injection system. It takes a callable (function, class, or any object with `__call__`) and tells FastAPI to call it before the route handler, passing the result as an argument. The callable can itself have parameters annotated with `Depends()`, creating a dependency tree.

**Caching within a request scope:** By default, a dependency is called *once per request* even if multiple route parameters or sub-dependencies declare it. The result is cached and reused. This is what `use_cache=True` (the default) means.

Setting `Depends(my_dep, use_cache=False)` disables caching — the dependency is called every time it's declared, even within the same request. This is rarely needed but useful for dependencies with side effects you want to run multiple times.

**The dependency graph** is built at startup (at route registration time), not at request time. FastAPI inspects the function signatures recursively to build a `Dependant` object. At request time it executes the graph in the correct order.

---

## Interview Questions

### Q1: How does FastAPI's dependency caching work? Can you break it?

**Model answer:**

FastAPI caches dependency results per-request using a dict keyed by the dependency callable itself. The first time `Depends(get_db)` is resolved in a request, `get_db()` is called and the result stored in `dependency_cache: dict[Callable, Any]` in the `solve_dependencies()` scope. Every subsequent `Depends(get_db)` in the same request gets the cached value.

The cache key is the callable object (by identity, not by name). This means:
- Two routes using the same `get_db` function share the cache — expected
- If you pass a lambda or a locally-defined function each time, each has a different identity → no caching even with `use_cache=True`
- Class instances used as callables: each instance is a distinct key

```python
def get_db() -> Session:  # same object every call — cached
    return Session()

# This breaks caching:
@app.get("/")
async def route(db: Session = Depends(lambda: get_db())):  # new lambda every route registration
    ...
```

`use_cache=False` is the correct way to bypass caching intentionally. Using lambdas to bypass it accidentally is a bug pattern.

**Gotcha follow-up:** If `get_current_user` depends on `get_db`, and the route also depends on `get_db` directly, how many times does `get_db` execute per request?

Once. The dependency cache ensures `get_db` is called once and the session is shared between `get_current_user` and the route's direct `Depends(get_db)`. This is the intended behavior — they share the same transaction scope.

---

### Q2: Trace through what happens when `solve_dependencies()` executes for a route with nested dependencies.

**Model answer:**

`solve_dependencies()` in `fastapi/dependencies/utils.py` is called once per request. It works as follows:

1. Look at the route's `Dependant` object (built at startup, cached)
2. For each dependency in `path_params`, `query_params`, etc., call `solve_dependencies()` recursively for that dependency's own `Dependant`
3. Before calling a dependency's callable, check `dependency_cache` — if present, skip the call and use the cached result
4. Call the callable (handling sync vs async, handling `yield` generators)
5. Store the result in `dependency_cache` keyed by the callable
6. Continue up the tree, eventually calling the route function with all resolved values

The execution is **depth-first**: sub-dependencies are fully resolved before the parent dependency is called. This is why if `get_current_user` depends on `get_db`, `get_db` completes first, then `get_current_user` receives a valid session.

For `yield` dependencies: a context manager stack (`contextlib.AsyncExitStack`) is maintained. Each `yield` dep's generator is entered via `__aenter__` and stored on the stack. After the route handler returns, the stack is exited in reverse order (LIFO), running the teardown of each `yield` dep.

---

### Q3: What is `use_cache=False` for, and when is it actually appropriate?

**Model answer:**

`Depends(my_dep, use_cache=False)` forces re-execution of the dependency on every call site, bypassing the per-request cache. Legitimate use cases are rare:

**Counters / rate limiters with side effects:** If the dependency increments a request counter, you might want it to run once per invocation, not once per request.

**Dependencies with random or time-sensitive output:** If a dependency generates a nonce or reads `time.time()` and you want fresh values at each injection point.

```python
import time

def get_timestamp() -> float:
    return time.time()

@app.get("/")
async def route(
    t1: float = Depends(get_timestamp, use_cache=False),  # independent calls
    t2: float = Depends(get_timestamp, use_cache=False),
) -> dict:
    return {"t1": t1, "t2": t2, "delta": t2 - t1}
```

In practice, if you find yourself reaching for `use_cache=False`, it's often a sign the dependency has too many side effects and could be refactored into a direct function call inside the route.

---

### Q4: How do class-based dependencies compare to function-based ones?

**Model answer:**

Any callable works as a dependency. A class is called by instantiating it (`MyDep()`), but if you pass a class instance (not the class itself), FastAPI will call `instance.__call__()`.

**Class-based dependency (stateful configuration):**
```python
class Paginator:
    def __init__(self, max_limit: int = 100):
        self.max_limit = max_limit
    
    def __call__(
        self,
        page: int = Query(default=1, ge=1),
        limit: int = Query(default=20, ge=1),
    ) -> dict:
        limit = min(limit, self.max_limit)
        return {"offset": (page - 1) * limit, "limit": limit}

# Different endpoints with different max limits:
standard_paginator = Paginator(max_limit=100)
admin_paginator = Paginator(max_limit=1000)

@app.get("/items/")
async def list_items(pagination: dict = Depends(standard_paginator)):
    ...

@app.get("/admin/items/")
async def admin_list_items(pagination: dict = Depends(admin_paginator)):
    ...
```

**Function-based dependency (simpler):**
```python
def get_pagination(
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=20, ge=1, le=100),
) -> dict:
    return {"offset": (page - 1) * limit, "limit": limit}
```

Class-based shines when you need configuration injected at app startup time (connection strings, feature flags, limits) while keeping the per-request behavior dynamic. The class instance is the configuration carrier; `__call__` is the per-request logic.

---

## Code: Dependency Tree with Shared Session

```python
from fastapi import FastAPI, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

DATABASE_URL = "postgresql+asyncpg://user:pass@localhost/db"
engine = create_async_engine(DATABASE_URL)
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)

app = FastAPI()


# Level 0: DB session (used by multiple deps and routes)
async def get_db() -> AsyncSession:
    async with AsyncSessionLocal() as session:
        yield session


# Level 1: Auth (depends on DB)
async def get_current_user(
    token: str = Depends(oauth2_scheme),  # assume oauth2_scheme defined
    db: AsyncSession = Depends(get_db),   # same session as route's direct Depends(get_db)
) -> User:
    user = await db.get(User, decode_token(token))
    if not user:
        raise HTTPException(status_code=401, detail="Invalid token")
    return user


# Level 2: Permission check (depends on user)
class RequirePermission:
    def __init__(self, permission: str):
        self.permission = permission
    
    async def __call__(self, user: User = Depends(get_current_user)) -> User:
        if self.permission not in user.permissions:
            raise HTTPException(status_code=403, detail="Forbidden")
        return user

require_admin = RequirePermission("admin")


# Route: three deps, but get_db executes ONCE (cached)
@app.delete("/users/{user_id}")
async def delete_user(
    user_id: int,
    current_user: User = Depends(require_admin),  # → get_current_user → get_db
    db: AsyncSession = Depends(get_db),           # cached — same session
):
    user = await db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404)
    await db.delete(user)
    await db.commit()
    return {"deleted": user_id}
```

---

## Under the Hood

The dependency graph is built in `fastapi/dependencies/utils.py:get_dependant()`. This function:
1. Inspects `inspect.signature(callable)`
2. For each parameter with a `Depends()` default, recursively calls `get_dependant()` on the dependency callable
3. Builds a `Dependant` dataclass that holds lists of `ModelField` objects for each parameter category plus a list of sub-`Dependant` objects

The result is stored in `route.dependant` at startup. At request time, `solve_dependencies(dependant, ...)` walks this pre-built tree, which is why FastAPI's per-request overhead is so low despite the complex dependency system — the expensive reflection happens once at startup.
