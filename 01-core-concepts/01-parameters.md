# Path, Query, Body, Header, Cookie Parameters

## Concept

FastAPI extracts parameters from the request using Python function signatures plus type annotations. The *location* of the parameter (path, query string, body, headers, cookies) is inferred by FastAPI based on where the name appears in the path template and what type the annotation is.

**Inference rules:**
- Name present in path template (`{item_id}`) → path parameter
- Primitive scalar (str, int, float, bool, UUID, datetime…) not in path → query parameter
- Pydantic `BaseModel` subclass → request body (JSON)
- Annotated with `Query(...)`, `Path(...)`, `Body(...)`, `Header(...)`, `Cookie(...)` → explicit declaration

`Header` automatically converts `_` to `-` in the header name (HTTP headers use hyphens; Python identifiers use underscores). This is controlled by `convert_underscores=True` (default).

**Validation** is Pydantic-backed. `Path(ge=1)`, `Query(min_length=3)`, `Body(embed=True)` etc. map directly to Pydantic field constraints which become JSON Schema in the generated OpenAPI spec.

`Body(embed=True)` forces the body to be wrapped in a JSON object keyed by the parameter name rather than being the root object. Useful when mixing multiple body params.

---

## Interview Questions

### Q1: How does FastAPI decide whether a parameter is a query param vs. a path param vs. a body?

**Model answer:**  
FastAPI inspects the function signature at startup during route registration. For each parameter:
1. If the name appears in the path string (e.g., `/items/{item_id}`), it's a path parameter.
2. If the type annotation is a Pydantic model (or inherits from `BaseModel`), FastAPI treats it as the request body.
3. Everything else defaults to a query parameter.
4. This inference can always be overridden by wrapping the default in `Path()`, `Query()`, `Body()`, `Header()`, or `Cookie()`.

The actual dispatch happens in `fastapi/dependencies/utils.py` → `get_dependant()` which walks function parameters and classifies them into `path_params`, `query_params`, `body_params`, etc., building a `Dependant` dataclass that's cached per route at startup.

**Gotcha follow-up:** What happens when you have two Pydantic models as body parameters?

FastAPI automatically `embed`s both: the expected JSON is `{"item": {...}, "user": {...}}`, not either model at the root. This is surprising to developers who expect the first model to be the root body.

---

### Q2: What's the difference between `Path(...)` with `...` (Ellipsis) versus a default value?

**Model answer:**  
`...` (Ellipsis) is Pydantic's sentinel for "required, no default." `Path(...)` means the path parameter is required and there is no fallback — FastAPI will return a 422 if it's missing, though path parameters are structurally always present in a well-formed URL.

Using `Path(default=None)` or `Query(default=None)` makes the parameter optional. The `...` vs `None` distinction matters more for query and body params:

```python
# Required query param — 422 if absent
@app.get("/items/")
async def get(q: str = Query(...)):
    ...

# Optional query param
@app.get("/items/")
async def get(q: str | None = Query(default=None)):
    ...
```

For path parameters, `...` is conventional but redundant — the URL routing already enforces presence.

**Gotcha follow-up:** Can you set a default for a path parameter?

Technically yes via `Path(default="default_value")`, but it's semantically broken: the path template has `{item_id}` which means the router matched *something* there. In practice, giving a path parameter a default makes the parameter appear optional in the OpenAPI schema but the route will never actually be hit with that default value supplied by the router — the value will always come from the URL match.

---

### Q3: How do Header parameters handle multi-value headers?

**Model answer:**  
Annotate the parameter as `list[str]` (or `List[str]`). HTTP allows the same header to appear multiple times, and Starlette exposes them all via `request.headers.getlist(name)`. FastAPI will collect all values into a list.

```python
@app.get("/")
async def get(x_token: list[str] = Header(default=[])):
    return {"tokens": x_token}
```

The underscore-to-hyphen conversion applies here too: `x_token` maps to the `X-Token` HTTP header (or `x-token` — headers are case-insensitive per RFC 7230).

---

### Q4: What validation does `Cookie()` perform, and how does it differ from reading `request.cookies` directly?

**Model answer:**  
`Cookie()` provides declarative validation (type coercion, length constraints, regex) and automatic OpenAPI documentation. It also participates in FastAPI's dependency system, meaning validation errors surface as structured 422 responses with field-level detail.

`request.cookies` is the raw Starlette dict — always strings, no validation, no schema generation. It's appropriate when you need raw access or when the cookie format is complex enough that field-level validation would be misleading.

The OpenAPI spec doesn't officially support cookie parameters well (they're poorly supported in Swagger UI's "Try it out"), so in practice `Cookie()` is used for documentation and validation but teams often document cookie auth separately.

---

### Q5: Explain `Body(embed=True)` — when and why would you use it?

**Model answer:**  
By default, when a Pydantic model is used as a body parameter, FastAPI expects the entire JSON payload to be that model:

```python
# Expects: {"name": "foo", "price": 1.0}
async def create(item: Item): ...
```

`Body(embed=True)` wraps it:

```python
# Expects: {"item": {"name": "foo", "price": 1.0}}
async def create(item: Item = Body(embed=True)): ...
```

This is primarily useful when:
1. You have **multiple body parameters** (FastAPI auto-embeds all of them)
2. You want the JSON key to be explicit for clarity or versioning
3. You're mixing `Body(embed=True)` fields with regular body fields and want consistent envelope structure

```python
async def update(item: Item, importance: int = Body(embed=True)):
    # Expects: {"item": {...}, "importance": 5}
    ...
```

---

## Code: Parameter Validation in Practice

```python
from fastapi import FastAPI, Path, Query, Body, Header, Cookie
from pydantic import BaseModel, Field

app = FastAPI()


class Item(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    price: float = Field(gt=0)
    tags: list[str] = []


class User(BaseModel):
    username: str


@app.get("/items/{item_id}")
async def get_item(
    item_id: int = Path(ge=1, description="Positive item ID"),
    q: str | None = Query(default=None, min_length=3, max_length=50),
    x_request_id: str | None = Header(default=None),  # X-Request-Id header
    session: str | None = Cookie(default=None),
) -> dict:
    return {"item_id": item_id, "q": q, "request_id": x_request_id}


@app.post("/items/{item_id}")
async def update_item(
    item_id: int = Path(ge=1),
    item: Item = Body(...),           # explicit body declaration
    user: User = Body(...),           # two body params → auto-embedded
) -> dict:
    # FastAPI expects: {"item": {...}, "user": {...}}
    return {"item_id": item_id, "item": item, "user": user}


@app.post("/embed-demo")
async def embed_demo(
    item: Item = Body(embed=True),    # {"item": {...}}
    count: int = Body(embed=True),    # {"item": {...}, "count": 5}
) -> dict:
    return {"item": item, "count": count}
```

---

## Under the Hood

Parameter classification happens at route registration in `fastapi/dependencies/utils.py:get_dependant()`. The function builds a `Dependant` object that caches:
- `path_params: list[ModelField]`
- `query_params: list[ModelField]`
- `body_params: list[ModelField]`
- `header_params: list[ModelField]`
- `cookie_params: list[ModelField]`

At request time, `solve_dependencies()` uses this cached `Dependant` to extract and validate values without re-inspecting the function signature. This is why startup is where the "expensive" reflection happens, not per-request.

Validation itself delegates to `pydantic-core` (Rust). Each `ModelField` wraps a Pydantic v2 `FieldInfo`, and validation goes through `pydantic_core.SchemaValidator` which is compiled Rust — validation of a single field is typically in the sub-microsecond range.
