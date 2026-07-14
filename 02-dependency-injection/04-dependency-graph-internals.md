# How FastAPI Builds the Dependency Graph at Startup (solve_dependencies Internals)

## Concept

FastAPI builds a complete dependency graph at **startup time** (route registration), not at request time. This graph is stored as a `Dependant` object on each route's `APIRoute` instance. At request time, `solve_dependencies()` walks this pre-built graph to resolve values — the expensive reflection (inspecting function signatures, traversing dependency trees) is done once, not per-request.

The key data structures:

```
APIRoute
  └── dependant: Dependant
        ├── path_params: list[ModelField]
        ├── query_params: list[ModelField]
        ├── body_params: list[ModelField]
        ├── header_params: list[ModelField]
        ├── cookie_params: list[ModelField]
        ├── dependencies: list[Dependant]   ← sub-dependency trees
        └── call: Callable                  ← the actual route/dep function
```

Each `Dependant` in the `dependencies` list is itself a fully-resolved tree. This recursive structure mirrors the dependency graph.

---

## Interview Questions

### Q1: Walk through how FastAPI processes `Depends()` at startup vs. at request time.

**Model answer:**

**At startup (route registration):**

When `@app.get("/path")` is called, FastAPI creates an `APIRoute`. During `APIRoute.__init__()`:
1. `get_dependant(path=path_format, call=endpoint_function)` is called
2. This inspects `inspect.signature(endpoint_function)` 
3. For each parameter with a `Depends(some_callable)` default:
   - Recursively calls `get_dependant(call=some_callable)`
   - Adds the result as a `Dependant` to `parent.dependencies`
4. For each parameter without `Depends()` (regular path/query/body params):
   - Creates a `ModelField` wrapping the Pydantic annotation
   - Adds it to the appropriate list (`path_params`, `query_params`, etc.)
5. The final `Dependant` tree is stored on the route — **never rebuilt again**

**At request time:**

`solve_dependencies(dependant, path_params, query_params, body, headers, cookies, dependency_cache)` is called:
1. Iterates over `dependant.dependencies` (already resolved tree)
2. For each sub-dependency:
   - Checks `dependency_cache` — if hit, use cached value
   - Otherwise calls the dependency callable (sync in threadpool, async directly)
   - For `yield` deps: enters via `AsyncExitStack`
   - Stores result in `dependency_cache`
3. Extracts and validates path/query/body params using `ModelField.validate()`
4. Calls the route function with all resolved values

The per-request work is **only** value resolution, not graph traversal or signature inspection.

---

### Q2: What is a `ModelField` in FastAPI's internal model, and how does it relate to Pydantic?

**Model answer:**

`ModelField` (in `fastapi.utils` and older versions in `fastapi._compat`) is FastAPI's internal adapter that wraps Pydantic's field representation. It exists because FastAPI needs to handle both Pydantic v1 and v2, and because it needs additional metadata (e.g., whether a field comes from query string, path, etc.).

In Pydantic v2, a `ModelField` wraps a `pydantic.fields.FieldInfo` and a `pydantic_core.SchemaValidator` for that specific field. FastAPI uses it to:
- Validate individual parameter values (not a full model — just one field)
- Generate the OpenAPI JSON Schema for that parameter
- Apply `include`/`exclude` logic in response serialization

The distinction from `pydantic.fields.FieldInfo`: `ModelField` is FastAPI's layer; `FieldInfo` is pure Pydantic. FastAPI wraps `FieldInfo` to track the HTTP source (path/query/body/header/cookie) alongside the type information.

---

### Q3: How does FastAPI handle circular dependencies?

**Model answer:**

FastAPI does **not** detect circular dependencies at startup — it would recurse infinitely building the `Dependant` tree, resulting in a `RecursionError` at startup, not a helpful error message.

Circular dependencies (`A depends on B, B depends on A`) are a structural error in the application design. FastAPI offers no cycle detection. The `RecursionError` at import time is the only signal.

The correct fix is to break the cycle through restructuring:
- Extract shared state into a third dependency that both A and B depend on
- Use `app.state` for truly shared singleton state instead of threading it through dependencies
- Use `contextvars` if the shared state is request-scoped but doesn't need to be a formal dependency

In practice, circular deps in FastAPI usually arise from importing module A's route file in module B's dependency file, while module A imports from module B. The fix is circular import resolution (moving shared code to a third module), not a FastAPI-level solution.

---

### Q4: How does FastAPI's dependency system interact with `Annotated` types (PEP 593)?

**Model answer:**

`Annotated[Type, Depends(some_dep)]` is the modern syntax for declaring dependencies without using default parameter values. This is now the preferred form over `param: Type = Depends(some_dep)`:

```python
from typing import Annotated
from fastapi import Depends

# Old form (still works):
async def route(db: AsyncSession = Depends(get_db)): ...

# New form (preferred):
DbSession = Annotated[AsyncSession, Depends(get_db)]
async def route(db: DbSession): ...
```

FastAPI extracts `Depends()` instances from the `__metadata__` of `Annotated` types during `get_dependant()`. The `Annotated` approach enables:
- Type aliases that carry their own dependency declarations (composable, reusable)
- Clean separation: the type hint is `AsyncSession`, the DI wire-up is separate metadata
- Better IDE support — the parameter appears as `AsyncSession` to type checkers

Multiple `Annotated` metadata items: if you have `Annotated[str, Query(min_length=3), Doc("the search term")]`, FastAPI processes each metadata item — `Query()` for validation, `Doc()` for documentation. The order matters for FastAPI's processing logic.

```python
from typing import Annotated
from fastapi import Depends, Query
from fastapi.params import Doc

# Reusable dependency alias
CurrentUser = Annotated[User, Depends(get_current_user)]
PaginationParams = Annotated[dict, Depends(get_pagination)]
SearchQuery = Annotated[str | None, Query(min_length=3, description="Search term")]

@app.get("/items/")
async def list_items(
    user: CurrentUser,
    pagination: PaginationParams,
    q: SearchQuery = None,
) -> list[Item]:
    ...
```

---

## Code: Introspecting the Dependency Graph

```python
import inspect
from fastapi import FastAPI, Depends
from fastapi.dependencies.utils import get_dependant
from fastapi.routing import APIRoute

app = FastAPI()


def dep_a() -> str:
    return "a"

def dep_b(a: str = Depends(dep_a)) -> str:
    return f"b({a})"

def dep_c(a: str = Depends(dep_a)) -> str:
    return f"c({a})"

@app.get("/")
async def root(b: str = Depends(dep_b), c: str = Depends(dep_c)) -> dict:
    return {"b": b, "c": c}


def print_dependant_tree(dep, indent=0):
    prefix = "  " * indent
    print(f"{prefix}→ {dep.call.__name__ if dep.call else 'route'}")
    for sub in dep.dependencies:
        print_dependant_tree(sub, indent + 1)


# After app setup, inspect the route's dependency tree:
@app.on_event("startup")  # for illustration; use lifespan in production
async def inspect_deps():
    for route in app.routes:
        if isinstance(route, APIRoute):
            print(f"Route: {route.path}")
            print_dependant_tree(route.dependant)
            # Output:
            # Route: /
            # → root
            #   → dep_b
            #     → dep_a
            #   → dep_c
            #     → dep_a   ← same dep, but dep_a runs ONCE (cached)
```

---

## Under the Hood

The full call chain at startup:

```
app.include_router() / @app.get()
  → APIRoute.__init__()
    → get_dependant(path, endpoint)
      → for each param: classify as path/query/body/header/cookie or Depends
      → for Depends: get_dependant(call=dep_callable)  [recursive]
      → return Dependant(call, path_params, query_params, ..., dependencies)
  → route.dependant = dependant  [cached]
```

At request time:
```
Starlette router matches request → APIRoute.handle()
  → run_endpoint_function(dependant, request)
    → solve_dependencies(dependant, request, dependency_cache={})
      → for each sub-dependant in dependant.dependencies:
          actual = app.dependency_overrides.get(sub.call, sub.call)
          if actual in dependency_cache: use cached
          else: call actual() [async or sync threadpool]
                store in dependency_cache
      → extract path/query/body params via ModelField.validate()
      → call dependant.call(**resolved_kwargs)
```

The `dependency_cache` dict lives only for the duration of one `solve_dependencies()` call — it is **not** shared across requests. Each request gets a fresh `{}`.
