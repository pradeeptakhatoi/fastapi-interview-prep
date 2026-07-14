# GraphQL Integration with FastAPI (Strawberry / Ariadne)

## Concept

GraphQL and FastAPI coexist cleanly because both are ASGI-compatible. GraphQL is mounted as a sub-application or route on the FastAPI app; REST endpoints continue to work alongside it.

**Two main libraries:**

| | Strawberry | Ariadne |
|--|-----------|---------|
| Schema definition | Code-first (Python classes + decorators) | Schema-first (SDL string + resolver functions) |
| Type safety | Strong (Python type annotations → GraphQL types) | Looser (resolvers are plain functions) |
| FastAPI integration | `strawberry.fastapi.GraphQLRouter` | `ariadne.asgi.GraphQL` mounted via `app.mount()` |
| Subscriptions | WebSocket-native | WebSocket-native |
| Dataloaders | `strawberry.dataloader.DataLoader` | `ariadne-django` / manual |
| Pydantic interop | `strawberry.experimental.pydantic` | Manual |

**Strawberry** is the modern choice for greenfield FastAPI projects — code-first schema is natural for Python developers, and the FastAPI integration via `GraphQLRouter` gives access to FastAPI's dependency injection from within resolvers.

**The N+1 problem** is GraphQL's most common production issue: querying a list of 100 users where each user has a `posts` field fires 100 separate SQL queries (one per user). **DataLoaders** batch and deduplicate these into a single query per request.

---

## Interview Questions

### Q1: How does Strawberry mount into a FastAPI application, and how do GraphQL resolvers access FastAPI dependencies?

**Model answer:**

Strawberry provides `GraphQLRouter` which is an `APIRouter` subclass. It registers the GraphQL endpoint (typically `POST /graphql` for queries/mutations and `GET /graphql` for the playground) as standard FastAPI routes. This means FastAPI's full middleware stack, dependency injection, and exception handling apply.

Resolvers access FastAPI dependencies via `strawberry.fastapi.Info` — a typed context object injected into every resolver:

```python
import strawberry
from strawberry.fastapi import GraphQLRouter
from fastapi import FastAPI, Depends
from sqlalchemy.ext.asyncio import AsyncSession

# Define context object that carries per-request deps
@strawberry.type
class Query:
    @strawberry.field
    async def users(self, info: strawberry.types.Info) -> list["UserType"]:
        db: AsyncSession = info.context["db"]
        result = await db.execute(select(User))
        return result.scalars().all()

schema = strawberry.Schema(query=Query)

# Context getter — receives FastAPI Request, returns dict available in info.context
async def get_context(
    db: AsyncSession = Depends(get_db),
    current_user = Depends(get_current_user),
) -> dict:
    return {"db": db, "user": current_user}

graphql_router = GraphQLRouter(schema, context_getter=get_context)

app = FastAPI()
app.include_router(graphql_router, prefix="/graphql")
```

The `context_getter` is a FastAPI dependency itself — it can `Depends()` on anything, including `yield` dependencies (DB sessions). Strawberry calls `context_getter` per request and merges the returned dict into `info.context`.

**Gotcha follow-up:** What happens to yield dependency teardown in the GraphQL context?

Because `context_getter` is a FastAPI dependency, any `yield` deps declared inside it (like `get_db`) have their teardown (`session.close()`, `session.rollback()`) triggered normally after the GraphQL request completes. The GraphQL router participates fully in FastAPI's `AsyncExitStack`-based teardown — it's not a special case.

---

### Q2: Explain the N+1 problem in GraphQL and how DataLoaders solve it. Trace the execution for a `users → posts` query.

**Model answer:**

**N+1 problem:**

```graphql
query {
  users {          # 1 SQL query: SELECT * FROM users
    id
    name
    posts {        # N SQL queries: SELECT * FROM posts WHERE user_id = ?
      title        # one per user → 1 + N total
    }
  }
}
```

With 100 users, this fires 101 SQL queries. In production at 100 req/s, that's 10,100 queries/sec for a single GraphQL query type.

**DataLoader solution:** batch and cache per-request.

A `DataLoader` accumulates keys (user IDs) as resolvers run, then fires a single batched query at the end of the "tick" (one event loop iteration). Per-request caching means the same user ID requested twice in one query returns the same cached value.

```python
from strawberry.dataloader import DataLoader
from sqlalchemy import select

async def load_posts_for_users(user_ids: list[int]) -> list[list[Post]]:
    """Called once with ALL user IDs gathered in one event loop tick."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Post).where(Post.user_id.in_(user_ids))
        )
        posts = result.scalars().all()

    # Group by user_id — must return results in the SAME ORDER as user_ids
    posts_by_user: dict[int, list[Post]] = {}
    for post in posts:
        posts_by_user.setdefault(post.user_id, []).append(post)
    return [posts_by_user.get(uid, []) for uid in user_ids]


# In context_getter: create a DataLoader per request (NOT singleton)
async def get_context(db: AsyncSession = Depends(get_db)) -> dict:
    return {
        "db": db,
        "posts_loader": DataLoader(load_fn=load_posts_for_users),
    }


@strawberry.type
class UserType:
    id: int
    name: str

    @strawberry.field
    async def posts(self, info: strawberry.types.Info) -> list["PostType"]:
        loader: DataLoader = info.context["posts_loader"]
        return await loader.load(self.id)
        # All 100 users call loader.load(id) synchronously
        # DataLoader batches them into one load_posts_for_users([1,2,...,100]) call
```

**Execution trace:**
1. `users` resolver fires → returns 100 `UserType` objects
2. GraphQL engine calls `posts` resolver for each `UserType` → 100 calls to `loader.load(user_id)`
3. `DataLoader` accumulates 100 keys, then fires ONE `load_posts_for_users([1..100])`
4. Single SQL: `SELECT * FROM posts WHERE user_id IN (1, 2, ..., 100)`
5. Results mapped back to each user

Total: 2 SQL queries regardless of user count (vs 1+N naive).

**Critical implementation detail:** DataLoaders must be **per-request**, not singletons. If a DataLoader lives on `app.state`, its cache persists across requests — you'll serve stale data. Create a new `DataLoader` instance in `context_getter` every request.

---

### Q3: How do you implement GraphQL subscriptions in FastAPI with Strawberry?

**Model answer:**

Strawberry subscriptions use WebSockets. The `GraphQLRouter` automatically adds a WebSocket route alongside the HTTP route. Subscription resolvers are async generators.

```python
import asyncio
import strawberry
from typing import AsyncGenerator

@strawberry.type
class Subscription:
    @strawberry.subscription
    async def count(
        self,
        info: strawberry.types.Info,
        target: int = 10,
    ) -> AsyncGenerator[int, None]:
        for i in range(target):
            yield i
            await asyncio.sleep(1)

    @strawberry.subscription
    async def order_updates(
        self,
        info: strawberry.types.Info,
        order_id: int,
    ) -> AsyncGenerator["OrderUpdate", None]:
        # Subscribe to Redis pub/sub for this order's updates
        redis = info.context["redis"]
        pubsub = redis.pubsub()
        await pubsub.subscribe(f"order:{order_id}")
        try:
            async for message in pubsub.listen():
                if message["type"] == "message":
                    data = json.loads(message["data"])
                    yield OrderUpdate(**data)
        finally:
            await pubsub.unsubscribe(f"order:{order_id}")


schema = strawberry.Schema(query=Query, subscription=Subscription)
```

The WebSocket protocol used is `graphql-transport-ws` (the modern standard) or `graphql-ws` (legacy). Strawberry supports both. Clients connect to the same `/graphql` endpoint but via WebSocket — the upgrade header differentiates it.

**Scaling subscriptions:** same problem as WebSocket scaling in general — subscriptions hold long-lived connections per worker. Use Redis pub/sub (as shown above) so any worker can publish and all subscribing workers forward to their connected clients.

---

### Q4: How does Strawberry's Pydantic integration work, and when is it useful in a FastAPI project that already uses Pydantic models?

**Model answer:**

`strawberry.experimental.pydantic` generates Strawberry types from Pydantic models, avoiding duplicate class definitions:

```python
from pydantic import BaseModel
import strawberry
from strawberry.experimental.pydantic import type as pydantic_type

class UserPydantic(BaseModel):
    id: int
    name: str
    email: str

@pydantic_type(model=UserPydantic, fields=["id", "name", "email"])
class UserType:
    pass  # Strawberry generates GraphQL type from Pydantic model
```

**When it's useful:**
- You have existing Pydantic models (for REST endpoints) and want to expose the same data via GraphQL without duplicating schema definitions
- Pydantic validation rules are reused for GraphQL input types

**When to avoid it:**
- GraphQL and REST schemas often diverge — the same "user" may expose different fields over each API. Coupling them tightly via `pydantic_type` makes it harder to evolve them independently.
- The experimental API has changed across Strawberry versions — stability concerns for production

The practical pattern in large codebases: keep Pydantic models for REST, define separate Strawberry types for GraphQL, and share only the SQLAlchemy ORM models (or domain objects) as the common layer. This gives each API full control over its contract.

---

## Code: Complete Strawberry + FastAPI Application

```python
# schema/types.py
from __future__ import annotations
import strawberry
from datetime import datetime


@strawberry.type
class PostType:
    id: int
    title: str
    content: str
    created_at: datetime

    @classmethod
    def from_orm(cls, post) -> PostType:
        return cls(
            id=post.id,
            title=post.title,
            content=post.content,
            created_at=post.created_at,
        )


@strawberry.type
class UserType:
    id: int
    name: str
    email: str

    @strawberry.field
    async def posts(self, info: strawberry.types.Info) -> list[PostType]:
        posts = await info.context["posts_loader"].load(self.id)
        return [PostType.from_orm(p) for p in posts]

    @classmethod
    def from_orm(cls, user) -> UserType:
        return cls(id=user.id, name=user.name, email=user.email)


@strawberry.input
class CreateUserInput:
    name: str
    email: str


# schema/resolvers.py
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from myapp.models import User, Post


@strawberry.type
class Query:
    @strawberry.field
    async def users(self, info: strawberry.types.Info) -> list[UserType]:
        db: AsyncSession = info.context["db"]
        result = await db.execute(select(User))
        return [UserType.from_orm(u) for u in result.scalars().all()]

    @strawberry.field
    async def user(
        self, info: strawberry.types.Info, id: int
    ) -> UserType | None:
        db: AsyncSession = info.context["db"]
        user = await db.get(User, id)
        return UserType.from_orm(user) if user else None


@strawberry.type
class Mutation:
    @strawberry.mutation
    async def create_user(
        self, info: strawberry.types.Info, input: CreateUserInput
    ) -> UserType:
        db: AsyncSession = info.context["db"]
        # Auth check via context
        current_user = info.context.get("user")
        if not current_user:
            raise strawberry.exceptions.PermissionError("Authentication required")

        user = User(name=input.name, email=input.email)
        db.add(user)
        await db.flush()
        return UserType.from_orm(user)


# main.py
from contextlib import asynccontextmanager
from fastapi import FastAPI, Depends, Request
from strawberry.fastapi import GraphQLRouter
from strawberry.dataloader import DataLoader
from sqlalchemy.ext.asyncio import AsyncSession
import strawberry


async def load_posts(user_ids: list[int]) -> list[list]:
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Post).where(Post.user_id.in_(user_ids))
        )
        posts = result.scalars().all()
    by_user: dict[int, list] = {}
    for post in posts:
        by_user.setdefault(post.user_id, []).append(post)
    return [by_user.get(uid, []) for uid in user_ids]


async def get_graphql_context(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user_optional),
) -> dict:
    return {
        "request": request,
        "db": db,
        "user": current_user,
        # Per-request DataLoader — NEVER put this on app.state
        "posts_loader": DataLoader(load_fn=load_posts),
    }


schema = strawberry.Schema(query=Query, mutation=Mutation)

graphql_router = GraphQLRouter(
    schema,
    context_getter=get_graphql_context,
    graphiql=True,      # interactive playground at /graphql
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield  # setup/teardown handled by yield deps in context_getter


app = FastAPI(lifespan=lifespan)
app.include_router(graphql_router, prefix="/graphql", tags=["GraphQL"])

# REST endpoints coexist
@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}
```

---

## Under the Hood

**Strawberry's execution model:** `GraphQLRouter` is an `APIRouter` with two routes:
- `POST /graphql` — receives the JSON query body, runs Strawberry's execution engine (`strawberry.execute()`), returns JSON response
- `GET /graphql` — serves GraphiQL playground (if enabled)
- `WS /graphql` — WebSocket endpoint for subscriptions (added automatically if `Subscription` type is in schema)

The execution engine parses the GraphQL query, builds an execution plan, and calls resolvers. Async resolvers are gathered with `asyncio.gather()` for parallel field resolution — siblings in the GraphQL selection set run concurrently.

**DataLoader batching mechanism:** Strawberry's `DataLoader` uses `asyncio` task scheduling. When `loader.load(key)` is called, it schedules a microtask to dispatch the batch after the current coroutine yields. All `load()` calls within the same event loop tick accumulate their keys; after the tick, the batch function fires once. This is why the order of operations matters — if you `await` between `load()` calls, each may dispatch a separate batch.

**Ariadne's approach:** `ariadne.asgi.GraphQL` is a raw ASGI application (not FastAPI-integrated). It's mounted via `app.mount("/graphql", GraphQL(schema))`, which creates a Starlette sub-application boundary — FastAPI middleware still wraps it (it's inside the main app's ASGI stack), but FastAPI's dependency injection is NOT available inside Ariadne resolvers without manual wiring. You'd pass context via `context_value` (a callable receiving the raw ASGI scope/request).
