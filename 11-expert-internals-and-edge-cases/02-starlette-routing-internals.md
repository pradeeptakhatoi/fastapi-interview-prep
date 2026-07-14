# How Starlette's Router Resolves Path Matching

## Concept

Starlette's routing is not a naive string comparison. Path templates like `/items/{item_id}` are compiled into regex patterns at startup. Path converters define how each `{param}` portion is matched and converted.

**Built-in path converters:**

| Syntax | Converter | Regex | Python type |
|--------|-----------|-------|-------------|
| `{param}` | `StringConverter` | `[^/]+` | `str` |
| `{param:int}` | `IntegerConverter` | `[0-9]+` | `int` |
| `{param:float}` | `FloatConverter` | `[0-9]+(.[0-9]+)?` | `float` |
| `{param:uuid}` | `UUIDConverter` | UUID regex | `uuid.UUID` |
| `{param:path}` | `PathConverter` | `.+` | `str` (with slashes) |

The compiled regex for `/items/{item_id:int}/details` becomes something like `^/items/(?P<item_id>[0-9]+)/details$`.

Route matching order matters: Starlette tries routes in the order they were added. First match wins. This is why static routes (`/items/me`) must be declared before parameterized ones (`/items/{item_id}`) or the param route will match `/items/me` first.

---

## Interview Questions

### Q1: Why does the order of route declaration matter in FastAPI, and how do you diagnose a routing conflict?

**Model answer:**

Starlette's `Router.routes` is a list. Matching iterates this list in order. The first route whose compiled regex matches the request path wins.

Classic conflict:
```python
@app.get("/users/{user_id}")   # matches /users/me ← WRONG
@app.get("/users/me")          # never reached
```

Fix: declare specific (static) routes before parameterized ones:
```python
@app.get("/users/me")          # checked first — matches /users/me
@app.get("/users/{user_id}")   # checked second — matches /users/123
```

**Diagnosing:** examine `app.routes` list at startup or log matched route in a middleware. FastAPI doesn't warn about shadowed routes. The symptom is that `/users/me` returns a 404 or a validation error (because `"me"` fails `int` conversion for `user_id: int`).

**With `APIRouter`:** the same ordering applies within a router. When routers are included, the `app.include_router()` call order determines which router's routes are checked first.

---

### Q2: How does `{param:path}` work, and what are its limitations?

**Model answer:**

`{param:path}` uses the `PathConverter` which compiles to the regex `.+` (matches anything including `/`). This allows capturing multi-segment paths:

```python
@app.get("/files/{file_path:path}")
async def get_file(file_path: str):
    # /files/docs/api/intro.html → file_path = "docs/api/intro.html"
    return {"path": file_path}
```

**Limitations:**

1. **Must be the last path segment.** Having anything after a `path` converter is ambiguous — the regex is greedy and will consume everything.

2. **URL encoding:** the `file_path` value contains the decoded path, not URL-encoded form. If you need to handle encoded slashes (`%2F`), be aware that some ASGI servers decode them before routing; others don't.

3. **Leading slash:** the captured value does NOT include a leading slash. `/files/a/b/c` → `file_path = "a/b/c"`.

4. **Path traversal risk:** if you construct file system paths from `file_path`, you MUST validate against directory traversal (`../` sequences). FastAPI/Starlette do not sanitize this.

---

### Q3: How does Starlette mount a sub-application differently from `include_router`?

**Model answer:**

`include_router(router)` merges the router's routes into the app's main route list, with prefix prepended. The routes become part of the single route list, all handled by the same application instance and middleware stack.

`mount(path, app=sub_app)` installs a separate ASGI application at a prefix. Requests whose path starts with `path` are dispatched to `sub_app`, which receives a modified scope where `root_path` is updated and `path` has the prefix stripped. The sub-app can be any ASGI application — another FastAPI instance, a static files handler, a WebSocket server.

```python
# include_router: same app, routes merged
v1_router = APIRouter(prefix="/v1")
app.include_router(v1_router)

# mount: separate ASGI app, independent middleware stack
legacy_app = FastAPI()
app.mount("/legacy", legacy_app)
```

Key differences:
- **Middleware**: `include_router` routes go through the main app's middleware. Mounted sub-apps have their own middleware stack (but see the outer middleware for the main app's routing layer).
- **dependency_overrides**: the mounted sub-app has its own `dependency_overrides` dict.
- **lifespan**: mounted sub-apps do NOT automatically participate in the main app's lifespan. You must manage their startup/shutdown manually.
- **OpenAPI docs**: mounted sub-apps are excluded from the main app's `/docs` and OpenAPI schema.

---

### Q4: Can you register a custom path converter? How?

**Model answer:**

Yes. Starlette supports custom converters via the `CONVERTERS` dict in `starlette.routing`:

```python
from starlette.routing import compile_path, CONVERTERS
from starlette.convertors import Convertor
import re

class DateConvertor(Convertor):
    regex = r"\d{4}-\d{2}-\d{2}"
    
    def convert(self, value: str):
        from datetime import date
        return date.fromisoformat(value)
    
    def to_string(self, value) -> str:
        return value.isoformat()

# Register before app creation
CONVERTERS["date"] = DateConvertor()

app = FastAPI()

@app.get("/events/{event_date:date}")
async def get_events(event_date: date):
    # event_date is already a datetime.date object
    return {"date": event_date.isoformat()}
```

After registration, `{param:date}` is a valid path converter that validates the format at the routing level and coerces the value before the endpoint sees it. FastAPI's OpenAPI schema generation won't automatically know about the type; you'd need to add a custom JSON schema component.

---

## Code: Route Matching Behavior Demonstration

```python
from fastapi import FastAPI, Request
from fastapi.routing import APIRoute

app = FastAPI()


@app.get("/users/me")          # static — must come first
async def get_me():
    return {"user": "me"}


@app.get("/users/search")      # another static route
async def search_users(q: str):
    return {"results": [], "query": q}


@app.get("/users/{user_id}")   # parameterized — after statics
async def get_user(user_id: int):
    return {"user_id": user_id}


@app.get("/files/{file_path:path}")  # path converter — catches everything
async def get_file(file_path: str):
    return {"path": file_path}


# Inspect compiled route patterns at startup
@app.on_event("startup")
async def log_routes():
    for route in app.routes:
        if isinstance(route, APIRoute):
            # Starlette's compiled regex is on route.path_regex
            print(f"{route.path} → {route.path_regex.pattern}")
```

---

## Under the Hood

Route compilation lives in `starlette/routing.py:compile_path()`. It parses the path string with a regex (`{param}` or `{param:converter}`), builds a pattern string, and returns:
- `path_regex`: compiled `re.Pattern` for matching
- `path_format`: the original path template (for URL generation)
- `param_convertors`: a dict of `{param_name: Convertor}` for type conversion after matching

At request time, `Router.handle()` iterates `self.routes` and calls `route.matches(scope)` for each. `matches()` runs `self.path_regex.match(path)` and, if it matches, applies the converters to extract typed values into `scope["path_params"]`. Starlette's route matching is `O(n)` in the number of routes — for very large apps with hundreds of routes, this is measurable; use `APIRouter` grouping with prefixes to limit the search space.
