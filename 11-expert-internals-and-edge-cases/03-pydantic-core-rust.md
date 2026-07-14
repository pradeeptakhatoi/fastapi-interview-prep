# pydantic-core (Rust): Where the Actual Performance Gains Come From

## Concept

Pydantic v2's validation runs through `pydantic-core`, a Rust extension module. The key insight: validation no longer happens in interpreted Python; it runs in compiled Rust via the Python C extension API. The Python-visible `BaseModel` API is a thin wrapper over Rust `SchemaValidator` objects.

**The compilation pipeline:**

1. At class definition time (`class MyModel(BaseModel):`), Python metaclass machinery runs
2. `ModelMetaclass.__new__()` inspects fields, builds a `CoreSchema` — a pure Python dict describing the validation logic
3. The `CoreSchema` is passed to `pydantic_core.SchemaValidator(core_schema)` which compiles it into a Rust validator object
4. This compiled validator is stored as `MyModel.__pydantic_validator__`
5. At validation time (`MyModel.model_validate(data)`), Python calls into the Rust validator with no Python overhead on the validation hot path

**What "compiled" means here:** the `CoreSchema` dict is interpreted by Rust code to build a tree of Rust `CombinedValidator` enum variants. Each variant knows how to validate its type without dynamic dispatch on each field — the validation tree is a static Rust data structure.

---

## Interview Questions

### Q1: What is the performance difference between Pydantic v1 and v2 validation, and why?

**Model answer:**

Benchmarks show v2 is typically **5–17x faster** for common validation patterns, with some cases showing 50x improvement (discriminated unions, large lists). The reasons:

**v1 (pure Python):**
- Each field validation is a Python function call
- Type coercion involves Python isinstance checks, dict lookups
- Error collection builds Python dicts
- GIL-held Python object allocation for every intermediate value

**v2 (Rust via pydantic-core):**
- The `SchemaValidator` compiled at class-definition time has no dynamic dispatch per field
- Memory allocation is handled by Rust's allocator, outside the Python heap
- Conversion between Python objects and Rust types happens once at entry/exit, not per field
- Rust can inline validation for primitive types (str, int, float) to single CPU instructions

For a FastAPI app receiving 1000 requests/sec with a 10-field request body:
- v1: ~500μs per validation → 50% of request time on validation alone
- v2: ~30μs per validation → validation is now negligible

**Practical implication:** the bottleneck shifts from Python validation to I/O (DB queries, network calls), which is the correct design.

---

### Q2: What is a `CoreSchema` and how does it relate to JSON Schema?

**Model answer:**

`CoreSchema` is a Python dict that describes validation rules in pydantic-core's internal format. It's **not** JSON Schema — it's Pydantic's own internal representation that gets compiled to Rust validators.

```python
from pydantic import BaseModel
from pydantic_core import core_schema

# CoreSchema for a simple string field with min_length
str_schema = core_schema.str_schema(min_length=3, max_length=100)

# CoreSchema for an int field
int_schema = core_schema.int_schema(ge=0, le=1000)

# CoreSchema for a model (simplified)
model_schema = core_schema.model_schema(
    MyModel,
    core_schema.model_fields_schema({
        "name": core_schema.model_field(str_schema),
        "count": core_schema.model_field(int_schema),
    }),
)
```

JSON Schema is generated separately from the `CoreSchema` via `pydantic.json_schema.GenerateJsonSchema`. This is why FastAPI's OpenAPI spec generation (`/openapi.json`) is a separate step from validation — it traverses the `CoreSchema` to produce JSON Schema, but the validation itself never touches JSON Schema.

**Gotcha:** custom types that use `__get_pydantic_core_schema__` classmethod to define their validation also define how they appear in `__get_pydantic_json_schema__` for the OpenAPI spec — these are two separate hooks.

---

### Q3: How do you define a custom type that integrates cleanly with pydantic-core validation?

**Model answer:**

Use `__get_pydantic_core_schema__` to return a `CoreSchema` that describes how your type validates:

```python
from __future__ import annotations
from typing import Any
from pydantic import GetCoreSchemaHandler
from pydantic_core import core_schema


class PositiveDecimal:
    def __init__(self, value: str):
        from decimal import Decimal, InvalidOperation
        try:
            d = Decimal(value)
        except InvalidOperation:
            raise ValueError(f"Invalid decimal: {value!r}")
        if d <= 0:
            raise ValueError(f"Must be positive, got {d}")
        self.value = d
    
    def __repr__(self) -> str:
        return f"PositiveDecimal({self.value})"
    
    @classmethod
    def __get_pydantic_core_schema__(
        cls,
        source_type: Any,
        handler: GetCoreSchemaHandler,
    ) -> core_schema.CoreSchema:
        # Accept str input, validate in Python, store as PositiveDecimal
        return core_schema.no_info_plain_validator_function(
            cls._validate,
            serialization=core_schema.plain_serializer_function_ser_schema(
                lambda v: str(v.value),
                info_arg=False,
            ),
        )
    
    @classmethod
    def _validate(cls, v: Any) -> PositiveDecimal:
        if isinstance(v, cls):
            return v
        if isinstance(v, str):
            return cls(v)
        raise ValueError(f"Expected str, got {type(v).__name__}")
    
    @classmethod
    def __get_pydantic_json_schema__(cls, schema, handler):
        return {"type": "string", "pattern": r"^\d+(\.\d+)?$", "description": "Positive decimal"}
```

```python
from pydantic import BaseModel

class Payment(BaseModel):
    amount: PositiveDecimal
    currency: str

p = Payment(amount="19.99", currency="USD")
p.model_dump()  # {"amount": "19.99", "currency": "USD"}
```

---

### Q4: How does pydantic-core handle validation errors, and how does FastAPI surface them?

**Model answer:**

When validation fails, `pydantic-core` collects all errors (not short-circuiting on the first failure) and raises `pydantic_core.ValidationError`. This is a Rust exception class that carries a list of `ErrorDetails`:

```python
[
    {
        "type": "missing",
        "loc": ("name",),
        "msg": "Field required",
        "input": {"count": 5},  # the raw input
        "url": "https://errors.pydantic.dev/..."
    },
    {
        "type": "int_parsing",
        "loc": ("count",),
        "msg": "Input should be a valid integer",
        "input": "not-a-number",
    }
]
```

FastAPI catches this at the request body parsing stage and re-raises as `fastapi.exceptions.RequestValidationError` (which wraps the Pydantic error). The default 422 handler in FastAPI calls `exc.errors()` on this wrapper and returns the error list as JSON.

The `loc` tuple mirrors the field path: for nested models, `("user", "address", "zip")` means the error is in `body.user.address.zip`. For query/path params, the first element is `"query"` or `"path"`.

**Performance note:** error collection itself is cheap in pydantic-core (it's Rust). The expensive part is converting the Rust error objects back to Python dicts when `exc.errors()` is called. For validation errors on hot paths (e.g., invalid webhook payloads at high volume), consider fast-failing after the first error with `strict=True` and a custom validator that raises immediately.

---

## Code: Inspecting the Compiled Schema

```python
from pydantic import BaseModel, Field
from pydantic_core import SchemaValidator
import json


class Item(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    price: float = Field(gt=0)
    tags: list[str] = []


# The compiled Rust validator
validator: SchemaValidator = Item.__pydantic_validator__
print(type(validator))  # <class 'pydantic_core.SchemaValidator'>

# The CoreSchema (Python dict) before compilation
core_schema_dict = Item.__pydantic_core_schema__
print(core_schema_dict["type"])  # "model"

# The JSON Schema (for OpenAPI)
json_schema = Item.model_json_schema()
print(json.dumps(json_schema, indent=2))
# {
#   "properties": {
#     "name": {"maxLength": 100, "minLength": 1, "type": "string"},
#     "price": {"exclusiveMinimum": 0, "type": "number"},
#     "tags": {"default": [], "items": {"type": "string"}, "type": "array"}
#   },
#   "required": ["name", "price"],
#   "title": "Item",
#   "type": "object"
# }

# Direct validation bypassing the model (fastest path)
raw = {"name": "Widget", "price": 9.99}
item_instance = validator.validate_python(raw)
print(item_instance)  # Item(name='Widget', price=9.99, tags=[])

# Strict mode validation (no coercion)
from pydantic import TypeAdapter
ta = TypeAdapter(Item)
try:
    ta.validate_python({"name": "Widget", "price": "9.99"}, strict=True)
except Exception as e:
    print(e)  # price: Input should be a valid number [type=float_type]
```

---

## Under the Hood

The C extension layer: `pydantic_core` is built with PyO3, a Rust library for Python extensions. `SchemaValidator` is a `#[pyclass]` (a Rust struct exposed to Python). When `validator.validate_python(data)` is called, PyO3 converts the Python dict to a Rust `Value` type, runs the validation tree (a Rust `CombinedValidator` enum), and converts the result back to a Python object. The back-and-forth conversion is where Python overhead is unavoidable, but the validation logic itself is zero-Python-overhead compiled Rust.

For JSON specifically, `validate_json(json_bytes)` is even faster — it parses the JSON *in Rust* (using `jiter`, a fast JSON parser) and validates simultaneously, without creating intermediate Python dict objects. This is why FastAPI's JSON body parsing goes through `pydantic_core.SchemaValidator.validate_json()` rather than `json.loads()` + `model.model_validate()`.
