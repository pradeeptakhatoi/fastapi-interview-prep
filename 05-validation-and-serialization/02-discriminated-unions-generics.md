# Nested Models, Discriminated Unions, Generic Models

## Concept

**Nested models:** Pydantic validates nested `BaseModel` instances recursively. The parent model's `CoreSchema` references the child model's `SchemaValidator` — validation is fully typed end-to-end.

**Discriminated unions:** when a field can be one of several types (a union), Pydantic normally tries each type in order until one validates — O(n) and ambiguous. A discriminated union uses a specific literal field (the "discriminator") to jump directly to the right type in O(1). This is both faster and produces clearer error messages.

**Generic models:** `BaseModel` supports Python generics via `Generic[T]`. Pydantic v2 fully supports this for building reusable container schemas (paginated responses, API envelopes, typed event payloads).

---

## Interview Questions

### Q1: When would you use a discriminated union over a plain `Union`? What's the performance difference?

**Model answer:**

**Plain union** (`Union[Cat, Dog]`): Pydantic tries `Cat` first. If it fails, tries `Dog`. Two full validation attempts for every `Dog` input. With 10 types in a union: up to 10 validation attempts per value.

**Discriminated union**: Pydantic reads the discriminator field first, maps it to the exact type, validates only that type. Always one validation attempt regardless of union size.

When to use discriminated unions:
- Polymorphic event payloads where a `type` field determines the schema
- API responses that can return one of several shapes
- Message bus payloads
- Any union where one field's value uniquely identifies the type

```python
from typing import Annotated, Literal, Union
from pydantic import BaseModel, Field

class DogEvent(BaseModel):
    type: Literal["dog"]
    breed: str
    fetch_ability: int

class CatEvent(BaseModel):
    type: Literal["cat"]
    indoor: bool
    lives_remaining: int

class BirdEvent(BaseModel):
    type: Literal["bird"]
    can_fly: bool

# Plain union — tries all three for every input
AnimalEventSlow = Union[DogEvent, CatEvent, BirdEvent]

# Discriminated union — reads "type", jumps directly to right model
AnimalEvent = Annotated[
    Union[DogEvent, CatEvent, BirdEvent],
    Field(discriminator="type"),
]
```

**Performance:** with 10 model types and 1000 validations/sec, a plain union does up to 10,000 validation attempts/sec. A discriminated union does 1,000. For large schemas, this is significant.

---

### Q2: How do you build a generic paginated response model in FastAPI?

**Model answer:**

```python
from typing import Generic, TypeVar
from pydantic import BaseModel

T = TypeVar("T")

class Page(BaseModel, Generic[T]):
    items: list[T]
    total: int
    page: int
    page_size: int
    has_next: bool

    @classmethod
    def from_query(cls, items: list[T], total: int, page: int, page_size: int) -> "Page[T]":
        return cls(
            items=items,
            total=total,
            page=page,
            page_size=page_size,
            has_next=(page * page_size) < total,
        )
```

Using it with FastAPI:

```python
class ItemResponse(BaseModel):
    id: int
    name: str
    price: float

@app.get("/items/", response_model=Page[ItemResponse])
async def list_items(page: int = 1, page_size: int = 20) -> Page[ItemResponse]:
    items = [ItemResponse(id=i, name=f"Item {i}", price=i * 1.5) for i in range(page_size)]
    return Page.from_query(items=items, total=100, page=page, page_size=page_size)
```

FastAPI correctly resolves `Page[ItemResponse]` in the OpenAPI schema — the `items` array will be documented as `ItemResponse` objects. Pydantic v2 generates a specific schema for each concrete instantiation of the generic.

**Gotcha:** using `Generic[T]` with `TypeVar` that has constraints works, but using it with `TypeVar(bound=SomeBase)` produces a bound constraint in the schema. If you omit the `TypeVar` and just use `Any`, the OpenAPI schema will document `items` as containing any type.

---

### Q3: How do nested model validation errors surface in FastAPI's 422 response?

**Model answer:**

Nested validation errors include the full path in the `loc` tuple:

```python
class Address(BaseModel):
    street: str
    zip_code: str = Field(pattern=r"^\d{5}$")

class User(BaseModel):
    name: str
    address: Address
```

If `zip_code` fails, the error `loc` is `("body", "address", "zip_code")`. FastAPI's 422 response:

```json
{
  "detail": [
    {
      "type": "string_pattern_mismatch",
      "loc": ["body", "address", "zip_code"],
      "msg": "String should match pattern '^\\d{5}$'",
      "input": "ABCDE"
    }
  ]
}
```

For list items, the index appears in the path: `("body", "items", 2, "price")` means the 3rd item in the `items` array has an invalid `price`.

This makes client-side field highlighting straightforward — the `loc` array maps directly to the JSON path of the invalid field.

---

## Code: Discriminated Union Event System

```python
from typing import Annotated, Literal, Union
from pydantic import BaseModel, Field
from fastapi import FastAPI

# Event types with discriminated union
class OrderCreatedPayload(BaseModel):
    type: Literal["order.created"]
    order_id: str
    customer_id: str
    total: float

class OrderShippedPayload(BaseModel):
    type: Literal["order.shipped"]
    order_id: str
    tracking_number: str
    carrier: str

class OrderRefundedPayload(BaseModel):
    type: Literal["order.refunded"]
    order_id: str
    refund_amount: float
    reason: str

# Discriminated on "type" field — O(1) dispatch
OrderEvent = Annotated[
    Union[OrderCreatedPayload, OrderShippedPayload, OrderRefundedPayload],
    Field(discriminator="type"),
]

class WebhookPayload(BaseModel):
    event_id: str
    timestamp: str
    event: OrderEvent  # nested discriminated union


app = FastAPI()

@app.post("/webhooks/orders")
async def handle_order_webhook(payload: WebhookPayload) -> dict:
    event = payload.event
    match event.type:
        case "order.created":
            return {"action": "create", "order_id": event.order_id}
        case "order.shipped":
            return {"action": "ship", "tracking": event.tracking_number}
        case "order.refunded":
            return {"action": "refund", "amount": event.refund_amount}


# Generic envelope with metadata
from typing import Generic, TypeVar

DataT = TypeVar("DataT")

class APIResponse(BaseModel, Generic[DataT]):
    success: bool = True
    data: DataT
    meta: dict = Field(default_factory=dict)

class ErrorResponse(BaseModel):
    success: bool = False
    error: str
    code: str

@app.get("/orders/{order_id}", response_model=APIResponse[OrderCreatedPayload])
async def get_order(order_id: str) -> APIResponse[OrderCreatedPayload]:
    payload = OrderCreatedPayload(
        type="order.created",
        order_id=order_id,
        customer_id="cust_123",
        total=99.99,
    )
    return APIResponse(data=payload, meta={"cached": False})
```

---

## Under the Hood

Pydantic v2's discriminated union compiles to a `tagged-union` `CoreSchema` node. At validation time, pydantic-core reads the discriminator field from the input dict in O(1) (dict key lookup), then dispatches to the pre-compiled validator for that specific type. The `Literal["order.created"]` constraint is folded into the dispatch table at schema compilation time — no Python-level branching at validation time.

For plain `Union`, pydantic-core tries each schema in order with `try-first` semantics. It stops at the first successful validation. The order matters: if `str` is before `int` in a union, `"123"` validates as `str` (in strict mode) or might coerce to either. In lax mode, put more specific types first.
