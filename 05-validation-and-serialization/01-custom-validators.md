# Custom Validators, Field Validators, Model Validators, Computed Fields

## Concept

Pydantic v2 offers four validation hooks with distinct responsibilities:

| Hook | Scope | Timing | Access |
|------|-------|--------|--------|
| `@field_validator("field", mode="before")` | Single field | Before type coercion | Raw input value |
| `@field_validator("field", mode="after")` | Single field | After type coercion | Typed value |
| `@model_validator(mode="before")` | Whole model | Before field validation | Raw dict/input |
| `@model_validator(mode="after")` | Whole model | After all fields valid | `self` (model instance) |
| `@computed_field` | Derived field | Read-time | `self` |

**`mode="before"` vs `mode="after"`:** "before" runs on raw input (what the client sent). "after" runs on the already-validated, type-coerced value. Choose "before" for input normalization (stripping whitespace, lowercasing), "after" for business-logic constraints.

**`@computed_field`:** defines a property that appears in serialization (`.model_dump()`) and the OpenAPI schema, computed from other fields. The decorated method must have a return type annotation.

---

## Interview Questions

### Q1: When would you use `@model_validator(mode="before")` vs `mode="after"`?

**Model answer:**

**`mode="before"` (raw input dict, must return dict):**
Use when you need to reshape or rename input before field-level validation runs:
- Accepting multiple input formats (camelCase and snake_case)
- Mapping aliased fields
- Pre-processing that would fail Pydantic's type system if done after

```python
@model_validator(mode="before")
@classmethod
def accept_legacy_format(cls, data: Any) -> Any:
    # Accept both "userId" and "user_id"
    if isinstance(data, dict) and "userId" in data:
        data = {**data, "user_id": data.pop("userId")}
    return data
```

**`mode="after"` (model instance, self, must return self):**
Use for cross-field business logic that requires typed values:
- Ensuring `end_date > start_date`
- Validating that exactly one of `email` or `phone` is provided
- Computing derived fields that need multiple inputs

```python
@model_validator(mode="after")
def check_date_range(self) -> "DateRange":
    if self.end_date <= self.start_date:
        raise ValueError("end_date must be after start_date")
    return self
```

**Gotcha:** In `mode="before"`, you receive the raw input — it could be a dict, another model instance, or anything. Always check `isinstance(data, dict)` before accessing keys. In `mode="after"`, `self` is fully constructed — you can access any field attribute safely.

---

### Q2: How do `@computed_field` properties behave during serialization and in OpenAPI schemas?

**Model answer:**

A `@computed_field` property:
1. Is **included** in `.model_dump()` output
2. Is **included** in `.model_dump_json()` output
3. Appears in the model's JSON schema (OpenAPI)
4. Is **excluded** from `model_fields` (it's not an input field — it can't be set via input data)
5. Cannot be used as a validator target in `@field_validator`

```python
from pydantic import BaseModel, computed_field

class Product(BaseModel):
    price: float
    tax_rate: float = 0.2

    @computed_field
    @property
    def price_with_tax(self) -> float:
        return self.price * (1 + self.tax_rate)

p = Product(price=100.0)
p.model_dump()  # {"price": 100.0, "tax_rate": 0.2, "price_with_tax": 120.0}
```

**Important:** computed fields are recalculated every time you access them (they're Python properties). They're not cached unless you use `@functools.cached_property` — but then serialization behavior changes (cached properties are not always picked up correctly). For expensive computations, store the result as a regular field.

**Excluding from output:** `@computed_field(repr=False)` excludes from `repr()` but not from serialization. To exclude from serialization: `model.model_dump(exclude={"price_with_tax"})`.

---

### Q3: How do you write a reusable validator that can apply to multiple field types?

**Model answer:**

For reusable validation logic, use `Annotated` with a custom type or a `BeforeValidator`/`AfterValidator` wrapper:

```python
from typing import Annotated
from pydantic import BeforeValidator, AfterValidator, Field

# Reusable validator function
def strip_and_lowercase(v: str) -> str:
    return v.strip().lower()

def ensure_positive(v: float) -> float:
    if v <= 0:
        raise ValueError(f"Must be positive, got {v}")
    return v

# Annotated type aliases — reusable across models
NormalizedEmail = Annotated[str, BeforeValidator(strip_and_lowercase)]
PositiveFloat = Annotated[float, AfterValidator(ensure_positive)]

class User(BaseModel):
    email: NormalizedEmail          # always lowercased
    monthly_budget: PositiveFloat   # always positive

class Invoice(BaseModel):
    recipient_email: NormalizedEmail  # same validator, different model
    amount: PositiveFloat
```

`BeforeValidator` is equivalent to `@field_validator(mode="before")` but defined at the type level rather than the model level. It composes — you can stack multiple validators in one `Annotated`:

```python
CleanEmail = Annotated[
    str,
    BeforeValidator(str.strip),
    BeforeValidator(str.lower),
    AfterValidator(validate_email_format),
]
```

Validators in `Annotated` run in left-to-right order for `before` validators, and left-to-right for `after` validators (same direction — the terminology can be confusing, but the execution order within `Annotated` is always left-to-right).

---

## Code: Comprehensive Validator Patterns

```python
from __future__ import annotations

from datetime import date
from typing import Annotated, Any

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    computed_field,
    field_serializer,
    field_validator,
    model_validator,
)


# --- Reusable type building blocks ---
def strip_whitespace(v: str) -> str:
    return v.strip()

def to_lowercase(v: str) -> str:
    return v.lower()

TrimmedStr = Annotated[str, BeforeValidator(strip_whitespace)]
EmailStr = Annotated[str, BeforeValidator(strip_whitespace), BeforeValidator(to_lowercase)]
PositiveInt = Annotated[int, Field(gt=0)]


# --- Model with multiple validator types ---
class Order(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    customer_email: EmailStr
    items: list[str] = Field(min_length=1)
    discount_code: TrimmedStr | None = None
    start_date: date
    end_date: date
    quantity: PositiveInt
    unit_price: float = Field(gt=0)

    # Field-level: after type coercion, check business rule
    @field_validator("items", mode="after")
    @classmethod
    def no_duplicate_items(cls, v: list[str]) -> list[str]:
        if len(v) != len(set(v)):
            raise ValueError("Duplicate items are not allowed")
        return v

    @field_validator("discount_code", mode="before")
    @classmethod
    def normalize_discount_code(cls, v: Any) -> Any:
        if isinstance(v, str):
            return v.strip().upper() or None  # empty string → None
        return v

    # Model-level: cross-field validation
    @model_validator(mode="after")
    def check_date_range(self) -> Order:
        if self.end_date < self.start_date:
            raise ValueError(f"end_date ({self.end_date}) must be >= start_date ({self.start_date})")
        return self

    # Computed fields
    @computed_field
    @property
    def total_price(self) -> float:
        return round(self.quantity * self.unit_price, 2)

    @computed_field
    @property
    def duration_days(self) -> int:
        return (self.end_date - self.start_date).days

    # Custom serializer for date → ISO string
    @field_serializer("start_date", "end_date")
    def serialize_date(self, dt: date) -> str:
        return dt.isoformat()


# Usage in FastAPI
from fastapi import FastAPI

app = FastAPI()

@app.post("/orders", status_code=201)
async def create_order(order: Order) -> dict:
    return order.model_dump()
```

---

## Under the Hood

`@field_validator` and `@model_validator` decorators set special attributes on the class methods that Pydantic's `ModelMetaclass` picks up during `BaseModel` class creation. They're converted to `CoreSchema` validator nodes:
- `@field_validator(mode="before")` → wraps the field's `CoreSchema` in a `with-default` + `plain-validator` before the main type validator
- `@field_validator(mode="after")` → appends a validator after the type validator in the field's validator chain
- `@model_validator(mode="after")` → wraps the entire model schema in a `function-after-schema` wrapper

The compiled Rust validator then calls back into Python for each validator function. Because these cross the Rust/Python boundary on each call, validators with heavy Python logic are the main performance bottleneck for Pydantic v2 — though still much faster than v1 due to the Rust infrastructure around them.
