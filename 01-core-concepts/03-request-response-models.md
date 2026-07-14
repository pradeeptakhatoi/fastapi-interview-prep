# Request/Response Models, response_model, response_model_exclude

## Concept

FastAPI separates input validation (request body models) from output filtering/serialization (`response_model`). This split is intentional: what you accept and what you expose are often different — passwords, internal IDs, computed fields that shouldn't be in the public contract.

**`response_model`** on a route decorator:
1. Defines the OpenAPI response schema (documentation)
2. Filters the returned data through the model — extra fields are stripped
3. Serializes the result to JSON via Pydantic

FastAPI calls `jsonable_encoder(response_model.model_validate(return_value))` internally. If the return value is already a Pydantic model of the right type, it's re-validated against `response_model`. If it's a dict or ORM object, it's coerced.

**`response_model_exclude`**: a set of field names to exclude from the serialized output at the route level. Faster than creating a subclass but less explicit.

**`response_model_include`**: include only these fields.

**`response_model_exclude_unset`**: equivalent to calling `.model_dump(exclude_unset=True)` on the response model. Useful for partial response patterns.

**`response_model_exclude_none`**: strips all `None` fields. Useful for clean JSON APIs where absent fields should be omitted rather than null.

---

## Interview Questions

### Q1: What exactly does `response_model` do at runtime? Trace the execution path.

**Model answer:**

When a route with `response_model=SomeModel` returns a value:

1. The return value hits `fastapi/routing.py` → `run_endpoint_function()`
2. That result is passed to `serialize_response()` in `fastapi/routing.py`
3. `serialize_response()` calls `fastapi.encoders.jsonable_encoder()` with the response model class
4. Inside `jsonable_encoder()`, if the value is a Pydantic model, `.model_dump()` is called with the `include`/`exclude`/`exclude_unset`/`exclude_none` params from the route decorator
5. If the value is an ORM object or dict, `response_model.model_validate(value)` is called first
6. The resulting dict is JSON-serialized by the `JSONResponse`

The critical implication: **returning extra fields from your endpoint function does not leak them** if they're not in `response_model`. The model acts as a whitelist filter on the output.

**Gotcha follow-up:** If you return a Pydantic model instance but it's a *different* Pydantic class than `response_model`, what happens?

FastAPI calls `response_model.model_validate(returned_instance)`. Since Pydantic v2 models support `from_attributes=True`, the validation succeeds if field names match — the returned instance is treated like any attribute-bearing object. Without `from_attributes=True` on the response model, it will fail if the returned value isn't a dict.

---

### Q2: When would you use `response_model_exclude_unset=True` on a route?

**Model answer:**

When you want the API to return only the fields that were explicitly set, not defaults. The most common use case is PATCH responses or read endpoints that support sparse fieldsets:

```python
class UserResponse(BaseModel):
    id: int
    name: str
    email: str
    bio: str | None = None
    avatar_url: str | None = None

@app.get("/users/{user_id}", response_model=UserResponse, response_model_exclude_none=True)
async def get_user(user_id: int):
    # If bio and avatar_url are NULL in the DB, they won't appear in the response
    ...
```

`exclude_none=True` is more common in APIs that want clean JSON without explicit nulls. `exclude_unset=True` is more appropriate when the response model is constructed programmatically and you want to signal "these fields weren't available."

The distinction matters: a field with value `None` but explicitly set is *included* by `exclude_unset=True` but *excluded* by `exclude_none=True`.

---

### Q3: How do you return different response schemas for different status codes?

**Model answer:**

Use the `responses` parameter on the route decorator, and return an explicit `Response` or `JSONResponse` with the appropriate status code:

```python
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel

class Item(BaseModel):
    id: int
    name: str

class ErrorDetail(BaseModel):
    code: str
    message: str

app = FastAPI()

@app.get(
    "/items/{item_id}",
    response_model=Item,
    responses={
        404: {"model": ErrorDetail, "description": "Item not found"},
        422: {"description": "Validation error"},
    },
)
async def get_item(item_id: int):
    if item_id == 0:
        return JSONResponse(
            status_code=404,
            content={"code": "NOT_FOUND", "message": "Item not found"},
        )
    return Item(id=item_id, name="Widget")
```

When you return a `Response` subclass directly, `response_model` filtering is **bypassed** — you control the payload entirely. This is intentional: explicit `Response` return means "I'll handle serialization myself."

---

### Q4: What's the difference between `response_model=None` and not setting `response_model`?

**Model answer:**

- **Not setting `response_model`**: FastAPI infers the schema from the return type annotation (if present). If no annotation, the response is undocumented but still works.
- **`response_model=None`**: explicitly disables response model validation and schema generation. FastAPI will not apply any filtering or serialization beyond what `JSONResponse` does normally. Useful when the response is dynamic, or when you're returning raw `Response` objects throughout and don't want FastAPI to interfere.

Setting `response_model=None` is the correct way to say "I'm managing the response myself; don't generate a response schema." Using a return type annotation of `Response` achieves a similar effect for OpenAPI documentation.

---

### Q5: You notice a field in your response that should not be there — a password hash. How do you prevent it?

**Model answer:**

Four options in increasing permanence:

**1. Separate response schema** (best practice — explicit contract):
```python
class UserDB(BaseModel):
    id: int
    email: str
    hashed_password: str  # internal

class UserResponse(BaseModel):
    id: int
    email: str  # no hashed_password

@app.get("/users/{id}", response_model=UserResponse)
async def get_user(id: int) -> UserDB: ...
```

**2. `Field(exclude=True)` on the model** (good if the field should *never* be serialized):
```python
class User(BaseModel):
    id: int
    email: str
    hashed_password: str = Field(exclude=True)
```

**3. `response_model_exclude` on the route** (quick fix, but fragile — name-based, not type-checked):
```python
@app.get("/users/{id}", response_model=User, response_model_exclude={"hashed_password"})
```

**4. `model_dump(exclude={"hashed_password"})` and return a dict** (bypasses response_model).

Option 1 is the correct architectural choice because it creates an explicit public contract. Option 2 is appropriate when the field is *never* appropriate to expose. Options 3 and 4 are expedient but maintenance-unfriendly.

---

## Code: Layered Response Models

```python
from fastapi import FastAPI, Depends, HTTPException, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ConfigDict

app = FastAPI()


# Internal (DB) representation
class UserInDB(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    email: str
    hashed_password: str
    is_active: bool
    is_superuser: bool


# Public API response — explicitly curated
class UserPublic(BaseModel):
    id: int
    email: str
    is_active: bool


# Admin API response — adds one more field
class UserAdmin(UserPublic):
    is_superuser: bool


class ErrorResponse(BaseModel):
    detail: str
    code: str


@app.get(
    "/users/{user_id}",
    response_model=UserPublic,
    responses={
        404: {"model": ErrorResponse},
        403: {"model": ErrorResponse},
    },
)
async def get_user(user_id: int, is_admin: bool = False) -> UserPublic | JSONResponse:
    # Simulated DB fetch — in practice, use Depends(get_db)
    fake_user = UserInDB(
        id=user_id,
        email="user@example.com",
        hashed_password="bcrypt_hash",
        is_active=True,
        is_superuser=False,
    )
    if not fake_user:
        return JSONResponse(
            status_code=404,
            content={"detail": "User not found", "code": "USER_NOT_FOUND"},
        )
    # FastAPI filters through UserPublic — hashed_password and is_superuser are stripped
    return fake_user


@app.get(
    "/admin/users/{user_id}",
    response_model=UserAdmin,    # more fields for admin endpoint
)
async def admin_get_user(user_id: int) -> UserAdmin:
    ...
```

---

## Under the Hood

`response_model` filtering is implemented in `fastapi/routing.py:serialize_response()`. It calls `fastapi.encoders.jsonable_encoder()` with the Pydantic model class, which handles the ORM-mode coercion, `include`/`exclude` application, and ultimately calls `.model_dump()`. The result is a plain Python dict that the `JSONResponse` class then JSON-serializes via `json.dumps()` (or `orjson.dumps()` if `ORJSONResponse` is used).

One important implication: if you return a large object graph (e.g., an ORM model with many lazy relationships), FastAPI will traverse the graph during `jsonable_encoder()`. SQLAlchemy lazy-loaded relationships will trigger additional SQL queries *during response serialization*, not during your route function. This is a common source of N+1 query bugs that are invisible in unit tests but show up in production.
