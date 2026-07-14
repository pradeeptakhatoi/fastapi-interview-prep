# Pydantic v1 vs v2 in FastAPI

## Concept

FastAPI 0.100+ defaults to Pydantic v2. The internal model is completely rewritten: validation runs via `pydantic-core` (Rust), the public API is partially backward-compatible, and several field/validator patterns changed meaningfully.

**Key API differences:**

| Concept | Pydantic v1 | Pydantic v2 |
|---------|-------------|-------------|
| Class config | `class Config:` | `model_config = ConfigDict(...)` |
| Root validators | `@root_validator` | `@model_validator(mode="before"/"after")` |
| Field validators | `@validator("field")` | `@field_validator("field", mode="before"/"after")` |
| Pre-validation | `@validator(..., pre=True)` | `mode="before"` |
| Schema generation | `.schema()` | `.model_json_schema()` |
| Dict export | `.dict()` | `.model_dump()` |
| JSON export | `.json()` | `.model_dump_json()` |
| Copy | `.copy()` | `.model_copy()` |
| Parse raw | `Model.parse_raw(json_str)` | `Model.model_validate_json(json_str)` |
| ORM mode | `orm_mode = True` | `from_attributes = True` in `ConfigDict` |
| Arbitrary types | `arbitrary_types_allowed = True` | Same, in `ConfigDict` |
| Computed fields | Not native | `@computed_field` |
| Serializers | `__json_encoder__` etc. | `@field_serializer`, `@model_serializer` |

**FastAPI compatibility shim:** FastAPI wraps Pydantic models in `ModelField` which handles v1/v2 differences internally. If you're on v1 syntax in a v2 world, you'll often get `PydanticUserError` at startup rather than at request time.

---

## Interview Questions

### Q1: What are the most impactful behavioral changes between Pydantic v1 and v2 for a FastAPI application in production?

**Model answer:**

**1. Validation performance:** v2's `pydantic-core` (Rust) is 5–50x faster than v1's pure-Python validation. For high-throughput APIs this is the single biggest operational change — you may be able to remove caches you added to work around slow validation.

**2. Strict vs lax mode:** v2 introduced explicit strict mode. In lax mode (default), `"1"` coerces to `int(1)`. In strict mode, it raises. v1 always ran in lax mode. This matters if you were relying on string-to-int coercion in API parameters.

**3. Validator ordering and return semantics:** v2 field validators receive the *already-validated* value in `after` mode; in `before` mode they receive raw input. v1's `@validator` behaved like `before` by default but the distinction was blurry. In v2 this is explicit, but teams migrating sometimes flip the wrong mode and get confusing type errors.

**4. `model_dump()` and `exclude_unset`:** `exclude_unset=True` is still present but the semantics of what counts as "set" changed subtly. In v2, a field set via alias is counted as set; in v1 there were edge cases.

**5. `model_config` replaces `class Config`:** Startup `UserWarning` if you still have `class Config`. It works as a fallback but some config options changed names.

---

### Q2: Walk me through converting a v1 model with root validators to v2 syntax.

**Model answer:**

```python
# Pydantic v1
from pydantic import BaseModel, validator, root_validator

class PaymentV1(BaseModel):
    amount: float
    currency: str
    
    @validator("currency")
    def uppercase_currency(cls, v):
        return v.upper()
    
    @root_validator
    def check_positive_amount(cls, values):
        if values.get("amount", 0) <= 0:
            raise ValueError("amount must be positive")
        return values
```

```python
# Pydantic v2
from pydantic import BaseModel, field_validator, model_validator, ConfigDict

class PaymentV2(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    
    amount: float
    currency: str
    
    @field_validator("currency", mode="after")
    @classmethod
    def uppercase_currency(cls, v: str) -> str:
        return v.upper()
    
    @model_validator(mode="after")
    def check_positive_amount(self) -> "PaymentV2":
        # In "after" mode, self is already a fully constructed model instance
        if self.amount <= 0:
            raise ValueError("amount must be positive")
        return self
```

Key differences:
- `@classmethod` is now required on `@field_validator`
- `@model_validator(mode="after")` receives `self` (the constructed instance), not `values` dict
- `@model_validator(mode="before")` receives raw input dict and returns a dict (like v1 `@root_validator(pre=True)`)

**Gotcha follow-up:** What does `mode="wrap"` do in field_validator?

It receives the raw value *and* a `handler` callable. You call `handler(value)` to run the default Pydantic validation, then you can modify the result. Useful for post-processing without duplicating the default validation logic. `mode="plain"` skips default validation entirely — you're responsible for type correctness.

---

### Q3: How does `model_config = ConfigDict(from_attributes=True)` work and when do you need it in FastAPI?

**Model answer:**

`from_attributes=True` (v1: `orm_mode = True`) tells Pydantic to validate data from object attribute access rather than dict key access. When enabled, `Model.model_validate(some_orm_object)` reads `.field_name` attributes on the object rather than `object["field_name"]`.

In FastAPI this is needed when:
1. Returning ORM model instances from a route with a Pydantic `response_model`
2. Using SQLAlchemy, Tortoise ORM, or any ORM that returns objects with attributes rather than dicts

```python
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

class Base(DeclarativeBase):
    pass

class UserORM(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str]

class UserSchema(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    email: str

@app.get("/users/{user_id}", response_model=UserSchema)
async def get_user(user_id: int, db: AsyncSession = Depends(get_db)):
    user = await db.get(UserORM, user_id)
    # FastAPI calls UserSchema.model_validate(user) — needs from_attributes=True
    return user
```

Without `from_attributes=True`, FastAPI tries to treat the ORM object as a dict, gets a `TypeError`, and raises a 500.

---

### Q4: What is `model_dump(exclude_unset=True)` and why does it matter for PATCH endpoints?

**Model answer:**

`exclude_unset=True` returns only fields that were explicitly provided in the input, not fields that received their default values. This is critical for PATCH semantics:

```python
class UserUpdate(BaseModel):
    name: str | None = None
    email: str | None = None
    bio: str | None = None

# PATCH body: {"name": "Alice"}
update = UserUpdate(name="Alice")
update.model_dump()                   # {"name": "Alice", "email": None, "bio": None}
update.model_dump(exclude_unset=True) # {"name": "Alice"}
```

If you use `model_dump()` (without `exclude_unset`), you'd overwrite `email` and `bio` to `None` in your database even though the client only intended to update `name`. Using `exclude_unset=True` gives you only the fields the client explicitly sent.

```python
@app.patch("/users/{user_id}")
async def patch_user(user_id: int, update: UserUpdate, db: AsyncSession = Depends(get_db)):
    user = await db.get(User, user_id)
    update_data = update.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(user, field, value)
    await db.commit()
    return user
```

**Gotcha follow-up:** If a client sends `{"email": null}`, does `exclude_unset=True` include `email`?

Yes. `null` in JSON maps to `None` in Python, but the field *was* explicitly set. `exclude_unset` tracks whether the field appeared in the input, not whether the value is non-None. So `{"email": null}` → `email` is in the dict returned by `exclude_unset=True`, with value `None`. This is the correct behavior for explicit null-ing in a PATCH.

---

## Code: Pydantic v2 Patterns for FastAPI

```python
from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import FastAPI
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    computed_field,
    field_serializer,
    field_validator,
    model_validator,
)

app = FastAPI()


class Address(BaseModel):
    street: str
    city: str
    country: str = "US"


class UserCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=100)
    email: str
    password: str = Field(min_length=8, exclude=True)  # excluded from serialization
    address: Address | None = None

    @field_validator("email", mode="after")
    @classmethod
    def normalize_email(cls, v: str) -> str:
        return v.lower()

    @model_validator(mode="after")
    def validate_name_not_email(self) -> UserCreate:
        if self.name == self.email:
            raise ValueError("name cannot be the same as email")
        return self


class UserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    email: str
    created_at: datetime
    address: Address | None = None

    @computed_field
    @property
    def display_name(self) -> str:
        return self.name.title()

    @field_serializer("created_at")
    def serialize_dt(self, dt: datetime) -> str:
        return dt.isoformat()


class UserUpdate(BaseModel):
    name: str | None = None
    email: str | None = None

    @field_validator("email", mode="after")
    @classmethod
    def normalize_email(cls, v: str | None) -> str | None:
        return v.lower() if v is not None else None


@app.patch("/users/{user_id}", response_model=UserResponse)
async def patch_user(user_id: int, update: UserUpdate) -> Any:
    patch_data = update.model_dump(exclude_unset=True)
    # Only fields explicitly sent by the client are in patch_data
    ...
```

---

## Under the Hood

Pydantic v2 compiles each model's validator at class creation time into a `CoreSchema` (a JSON-serializable schema description), then passes it to `pydantic_core.SchemaValidator` which builds a Rust validator. This is why the first import of a model is slightly slower (schema compilation) but per-instance validation is extremely fast — it's pure Rust with no Python overhead on the hot path.

FastAPI wraps Pydantic models in `fastapi.utils.create_model_field()` which generates a `ModelField` — an internal abstraction that adapts to both Pydantic v1 and v2 APIs. By FastAPI 0.111, the compatibility layer is stable, but if you're using internal Pydantic APIs (accessing `__fields__` instead of `model_fields`) you'll hit v1/v2 divergence.
