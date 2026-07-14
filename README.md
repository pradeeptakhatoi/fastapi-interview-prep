# FastAPI Interview Prep — Senior / Staff Engineer

A targeted preparation guide for engineers with production FastAPI experience interviewing at Senior or Staff level. This is **not** an intro tutorial. The assumption throughout is that you already know how to build with FastAPI; the goal is to make you fluent in *why* it works the way it does and where it will surprise you under load.

---

## Scope

- FastAPI, Starlette, ASGI internals
- Pydantic v2 (all code uses v2 syntax)
- Production deployment (Gunicorn/Uvicorn, async correctness, observability)

Out of scope: generic system design, behavioral questions, non-FastAPI Python frameworks.

---

## Study Roadmap

### Must-Know (foundation — interview will assume this)

| # | Section | Est. Days | Why It Matters |
|---|---------|-----------|----------------|
| 01 | [Core Concepts](./01-core-concepts/) | 1 | Parameter binding, Pydantic v2, response models — asked in every screen |
| 02 | [Dependency Injection](./02-dependency-injection/) | 2 | The single deepest area interviewers probe; `yield` deps + caching + overrides distinguish mid from senior |
| 03 | [Async Internals](./03-async-internals/) | 2 | Sync vs async endpoint threadpool behavior — production bugs live here |
| 04 | [Routing & Middleware](./04-routing-and-middleware/) | 1 | Middleware stack ordering, lifespan, background tasks |
| 07 | [Testing](./07-testing/) | 1 | `TestClient` vs `AsyncClient`, dependency overrides — every team asks this |

### Should-Know (expected at Senior level)

| # | Section | Est. Days | Why It Matters |
|---|---------|-----------|----------------|
| 05 | [Validation & Serialization](./05-validation-and-serialization/) | 1.5 | Custom validators, discriminated unions, OpenAPI customization |
| 06 | [Security](./06-security/) | 1 | OAuth2, JWT, CORS — table stakes for any API role |
| 08 | [Performance & Production](./08-performance-production/) | 2 | Gunicorn tuning, connection pooling, profiling |
| 10 | [Common Pitfalls](./10-common-pitfalls/) | 0.5 | Mutable defaults, circular imports, global state misuse |

### Differentiator (Staff-level signal)

| # | Section | Est. Days | Why It Matters |
|---|---------|-----------|----------------|
| 09 | [Advanced Features](./09-advanced-features/) | 1.5 | WebSockets at scale, SSE, sub-applications |
| 11 | [Expert Internals & Edge Cases](./11-expert-internals-and-edge-cases/) | 3 | ASGI spec, raw middleware, pydantic-core, exception handler ordering — separates principal from senior |

**Suggested order:** 01 → 02 → 11 → 03 → 04 → 05 → 07 → 06 → 08 → 09 → 10

Do 11 early. Interviewers save those questions for the end of the loop, but your answer quality there decides the level.

---

## Snippets

Production-ready, copy-paste code in [`/snippets`](./snippets/):

| File | Contents |
|------|----------|
| `raw_asgi_middleware.py` | Raw ASGI middleware (no `BaseHTTPMiddleware`) that preserves streaming |
| `jwt_auth_dependency.py` | JWT Bearer dependency with refresh token pattern |
| `db_session_dependency.py` | `yield`-based SQLAlchemy async session dependency |
| `token_bucket_rate_limiter.py` | Token bucket rate limiter via Redis + async FastAPI dependency |
| `sse_endpoint.py` | Server-Sent Events endpoint with async generator |
| `idempotency_key_dependency.py` | Idempotency key dependency for POST endpoints |
| `custom_openapi_schema.py` | Programmatic OpenAPI schema override |

---

## Conventions

- All code targets **Python 3.11+**, **FastAPI 0.111+**, **Pydantic v2**
- Runnable snippets use inline comments only where behavior is non-obvious
- "Under the hood" callouts reference actual Starlette/FastAPI source modules, not vague descriptions
- Each file: concept → interview questions with model answers → gotcha follow-ups → code
