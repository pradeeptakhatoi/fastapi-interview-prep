# APIRouter, Prefix/Tags, Versioning Patterns

## Concept

`APIRouter` is a mini-application that accumulates route definitions. When included in a `FastAPI` app via `app.include_router()`, its routes are merged into the app's route list with optional prefix, tags, and dependency injection modifications.

`include_router()` parameters:
- `prefix`: prepended to all routes in the router (e.g., `/v1`)
- `tags`: added to all routes for OpenAPI grouping
- `dependencies`: applied to every route in the router (without modifying route signatures)
- `responses`: merged into every route's response definitions
- `default_response_class`: overrides the default for all routes

**Versioning patterns:**

| Pattern | Mechanism | Tradeoffs |
|---------|-----------|-----------|
| URL path versioning | `/v1/items`, `/v2/items` | Explicit, cacheable, most common |
| Header versioning | `Accept: application/vnd.api+json;version=2` | Clean URLs, harder to test, poor browser support |
| Query param | `/items?version=2` | Simple but pollutes query string |
| Subdomain | `v2.api.example.com` | Requires DNS/infra setup |

URL versioning is the FastAPI-natural approach and is what `APIRouter` + `prefix` supports cleanly.

---

## Interview Questions

### Q1: How do you structure a large FastAPI project with multiple domains and API versions?

**Model answer:**

The canonical structure:

```
myapp/
  api/
    v1/
      __init__.py
      items.py     ← APIRouter()
      users.py     ← APIRouter()
      orders.py    ← APIRouter()
    v2/
      __init__.py
      items.py     ← APIRouter() with updated contracts
  main.py          ← FastAPI() + include_router() calls
```

```python
# api/v1/items.py
from fastapi import APIRouter

router = APIRouter(prefix="/items", tags=["items"])

@router.get("/")
async def list_items(): ...

@router.post("/", status_code=201)
async def create_item(): ...
```

```python
# main.py
from fastapi import FastAPI
from myapp.api.v1 import items as items_v1, users as users_v1
from myapp.api.v2 import items as items_v2

app = FastAPI()

app.include_router(items_v1.router, prefix="/v1")
app.include_router(users_v1.router, prefix="/v1")
app.include_router(items_v2.router, prefix="/v2")
```

Routes become: `GET /v1/items/`, `POST /v1/items/`, `GET /v2/items/`.

**The versioning architecture decision:** v2 should only re-define endpoints that actually changed. All unchanged v1 routes should be reused (import from v1 module, include with v2 prefix). This avoids duplicating identical logic across versions.

---

### Q2: How do you apply authentication to all routes in a router without adding `Depends()` to every route?

**Model answer:**

Pass `dependencies` to `include_router()`:

```python
from fastapi import Depends
from myapp.auth import require_auth

app.include_router(
    private_router,
    prefix="/api",
    dependencies=[Depends(require_auth)],
)
```

Or at the router definition level:

```python
router = APIRouter(
    prefix="/admin",
    dependencies=[Depends(require_admin)],
)
```

**Scope and override:** the `dependencies` list is additive — route-level dependencies are still applied. You cannot remove a router-level dependency on a specific route; you can only add more. This is important for the "opt-out" pattern — if some routes in a router need to be public, either:
1. Separate them into a different (unauthenticated) router
2. Override the dependency with `app.dependency_overrides` in tests (but this doesn't help in production opt-outs)

**Gotcha follow-up:** Do router-level `dependencies` appear in the OpenAPI spec?

Yes, but only if the dependency exposes security scheme information (e.g., via `SecurityBase` like `OAuth2PasswordBearer`). Plain dependencies (like `Depends(check_ip_whitelist)`) are executed but don't add security requirements to the OpenAPI schema. To document the security requirement, use `Security()` or `Depends()` with `scopes`.

---

### Q3: How does prefix stacking work when you nest routers?

**Model answer:**

Routers can include other routers. Prefixes stack:

```python
v1_router = APIRouter(prefix="/v1")
items_router = APIRouter(prefix="/items")
items_router.get("/")(list_items)       # defines GET /items/

v1_router.include_router(items_router)  # items_router prefix prepended → /v1/items/
app.include_router(v1_router)           # v1 prefix prepended → /v1/items/
```

Final route: `GET /v1/items/`

Tags also stack (merged lists). Dependencies also stack (both lists executed). This allows hierarchical routing with nested auth levels:

```python
api_router = APIRouter(prefix="/api", dependencies=[Depends(require_valid_token)])
admin_router = APIRouter(prefix="/admin", dependencies=[Depends(require_admin)])
api_router.include_router(admin_router)
# routes: /api/admin/... with BOTH require_valid_token AND require_admin applied
app.include_router(api_router)
```

---

## Code: Versioned API with Router Reuse

```python
from fastapi import FastAPI, APIRouter, Depends, HTTPException
from pydantic import BaseModel


# --- Shared auth ---
async def require_auth() -> dict:
    return {"user_id": 1}  # simplified


# --- V1 items ---
class ItemV1(BaseModel):
    id: int
    name: str

items_v1_router = APIRouter(prefix="/items", tags=["Items"])

@items_v1_router.get("/", response_model=list[ItemV1])
async def list_items_v1() -> list[ItemV1]:
    return [ItemV1(id=1, name="Widget")]

@items_v1_router.get("/{item_id}", response_model=ItemV1)
async def get_item_v1(item_id: int) -> ItemV1:
    return ItemV1(id=item_id, name="Widget")


# --- V2 items (extended schema, backward-compatible) ---
class ItemV2(BaseModel):
    id: int
    name: str
    description: str | None = None  # new field
    tags: list[str] = []            # new field

items_v2_router = APIRouter(prefix="/items", tags=["Items"])

@items_v2_router.get("/", response_model=list[ItemV2])
async def list_items_v2() -> list[ItemV2]:
    return [ItemV2(id=1, name="Widget", description="A fine widget", tags=["sale"])]

# V2 reuses V1's get endpoint (unchanged)
items_v2_router.include_router(
    APIRouter(routes=[
        route for route in items_v1_router.routes
        if hasattr(route, "path") and "{item_id}" in route.path
    ])
)


# --- App assembly ---
app = FastAPI(title="Versioned API")

v1 = APIRouter(prefix="/v1", dependencies=[Depends(require_auth)])
v1.include_router(items_v1_router)

v2 = APIRouter(prefix="/v2", dependencies=[Depends(require_auth)])
v2.include_router(items_v2_router)

app.include_router(v1)
app.include_router(v2)

# Health check — no auth, no version prefix
@app.get("/health", tags=["Ops"])
async def health() -> dict:
    return {"status": "ok"}
```

---

## Under the Hood

`app.include_router(router)` calls `router.generate_unique_id_function` on each route and then calls `app.add_api_route()` (or `app.add_api_websocket_route()`) for each route in the router. It does NOT copy the router object — it copies each route definition individually with modified prefix/tags/dependencies. After `include_router()`, changes to the original router do not affect the app's route list.

The `tags` and `dependencies` parameters are merged at include time. For `tags`, it's list concatenation. For `dependencies`, it's list concatenation — executed in order (router-level deps first, then route-level deps). This means router-level auth dependency failures raise before route-level business logic dependencies even run.
