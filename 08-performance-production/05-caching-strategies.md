# Caching Strategies in FastAPI

## Concept

Caching is the act of storing computed results so future requests can skip the computation. In FastAPI, caching operates at multiple layers, each with different trade-offs in consistency, complexity, and invalidation cost:

```
Browser / CDN cache          ← HTTP cache headers (Cache-Control, ETag)
      ↓
Reverse proxy cache          ← nginx proxy_cache, Varnish
      ↓
Application cache            ← Redis (shared across workers), in-process (per worker)
      ↓
Database query cache         ← SQLAlchemy identity map, DB-level query cache
      ↓
Database
```

**The three hard problems in caching:**
1. **Cache invalidation** — knowing when to evict stale data. Phil Karlton's "two hard things in computer science" problem.
2. **Cache stampede (thundering herd)** — when a hot key expires, many concurrent requests simultaneously miss and all try to recompute. The recomputation load can exceed the load you were trying to cache away.
3. **Multi-process coherence** — in-process caches (per Gunicorn worker) are invisible to other workers. Invalidating one worker's cache doesn't invalidate others.

**Caching patterns:**

| Pattern | Description | Consistency | Complexity |
|---------|-------------|-------------|------------|
| Cache-aside (lazy) | Application checks cache; on miss, reads DB and populates cache | Eventual (TTL-based or explicit) | Low |
| Write-through | Write to cache and DB synchronously on every write | Strong | Medium |
| Write-behind (write-back) | Write to cache immediately, DB asynchronously | Weak (data loss risk) | High |
| Read-through | Cache layer fetches from DB on miss (transparent to app) | Eventual | Medium |
| Refresh-ahead | Background task proactively refreshes before TTL expires | Strong | High |

---

## Interview Questions

### Q1: How does HTTP cache control work in FastAPI, and when is it the right caching layer to use?

**Model answer:**

HTTP caching is the cheapest possible cache — the client or an intermediate CDN/proxy serves the cached response without the request ever reaching your application server. FastAPI sets cache headers in the response; the infrastructure upstream handles the rest.

**Key headers:**

```python
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
import hashlib, json

app = FastAPI()


@app.get("/products/{product_id}")
async def get_product(product_id: int, response: Response):
    product = await fetch_product(product_id)   # DB fetch

    # Cache-Control: max-age tells clients/proxies how long to cache (seconds)
    response.headers["Cache-Control"] = "public, max-age=300, stale-while-revalidate=60"

    # ETag: fingerprint of the response body — clients send If-None-Match on revalidation
    etag = hashlib.md5(json.dumps(product).encode()).hexdigest()
    response.headers["ETag"] = f'"{etag}"'

    return product


@app.get("/products/{product_id}/conditional")
async def get_product_conditional(product_id: int, request: Request, response: Response):
    product = await fetch_product(product_id)
    etag = f'"{hashlib.md5(json.dumps(product).encode()).hexdigest()}"'

    # If client already has this version, return 304 Not Modified (no body)
    if request.headers.get("If-None-Match") == etag:
        return Response(status_code=304, headers={"ETag": etag})

    response.headers["ETag"] = etag
    response.headers["Cache-Control"] = "public, max-age=300"
    return product
```

**`Cache-Control` directives for APIs:**

```
public, max-age=300                 → cacheable by CDN and browser for 5 min
private, max-age=300                → cacheable by browser only (user-specific data)
no-store                            → never cache (auth endpoints, payment data)
no-cache                            → always revalidate with server before using
stale-while-revalidate=60           → serve stale for 60s while fetching fresh in background
stale-if-error=3600                 → serve stale for 1h if origin is down
```

**`Vary` header — critical for correctness:** tells caches to store separate copies for different request header values:

```python
# If response differs by Accept-Language, the cache must store one copy per language
response.headers["Vary"] = "Accept-Language, Accept-Encoding"
```

Without `Vary: Authorization` on private endpoints behind a shared proxy, one user's response could be served to another.

**When to use HTTP caching:**
- Public, user-agnostic data: product catalog, static reference data, public API responses
- CDN edge caching: responses that can be cached globally near users
- Reducing origin traffic for read-heavy, rarely-changing data

**When NOT to use HTTP caching:**
- Authenticated, user-specific data (unless `Cache-Control: private`)
- Data that changes faster than the minimum TTL you're willing to tolerate
- Anything requiring immediate consistency on write (cart contents, account balance)

**Gotcha follow-up:** What happens if you set `Cache-Control: public, max-age=3600` on an endpoint that also returns an `Authorization`-scoped response?

A shared CDN (CloudFront, Fastly) will cache the first user's response and serve it to all subsequent users for the next hour, regardless of who they are. The fix is `Cache-Control: private, max-age=3600` (browser caches only) or removing the header entirely. The correct `Vary: Authorization` would cause the CDN to cache one copy per Authorization header value — which usually means one copy per user token (defeating the purpose of CDN caching and growing the cache unboundedly as tokens rotate).

---

### Q2: How do you implement Redis cache-aside in FastAPI without duplicating cache logic across endpoints?

**Model answer:**

The cleanest approach wraps the cache logic in a reusable decorator or dependency that is transparent to the endpoint body.

```python
import functools
import json
import time
from typing import Any, Callable, TypeVar
from collections.abc import Awaitable

import redis.asyncio as aioredis
from fastapi import Request

F = TypeVar("F", bound=Callable[..., Awaitable[Any]])


def cached(ttl: int, key_prefix: str = "", vary_on: list[str] | None = None):
    """
    Decorator: cache the JSON-serializable return value of an async endpoint.

    vary_on: list of path/query parameter names to include in the cache key.
    If omitted, all path params are used.
    """
    def decorator(func: F) -> F:
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            # Extract request from kwargs (injected by FastAPI)
            request: Request = kwargs.get("request") or next(
                (a for a in args if isinstance(a, Request)), None
            )
            redis: aioredis.Redis = request.app.state.redis

            # Build cache key from prefix + relevant params
            params = vary_on or list(request.path_params.keys())
            key_parts = [key_prefix or func.__name__]
            for p in sorted(params):
                val = request.path_params.get(p) or request.query_params.get(p, "")
                key_parts.append(f"{p}:{val}")
            cache_key = "cache:" + ":".join(key_parts)

            # Cache hit
            cached_val = await redis.get(cache_key)
            if cached_val is not None:
                request.state.cache_hit = True
                return json.loads(cached_val)

            # Cache miss: call the real endpoint
            request.state.cache_hit = False
            result = await func(*args, **kwargs)

            # Populate cache (fire-and-forget — don't let Redis errors break the endpoint)
            try:
                await redis.setex(cache_key, ttl, json.dumps(result))
            except Exception:
                pass  # log in production; don't surface to client

            return result
        return wrapper  # type: ignore[return-value]
    return decorator


# Usage
from fastapi import FastAPI, Depends

app = FastAPI()


@app.get("/products/{product_id}")
@cached(ttl=300, key_prefix="product")
async def get_product(product_id: int, request: Request) -> dict:
    return await db_fetch_product(product_id)


@app.get("/categories")
@cached(ttl=3600, key_prefix="categories", vary_on=[])  # no params → single shared key
async def list_categories(request: Request) -> list:
    return await db_fetch_categories()
```

**Cache invalidation companion:**

```python
async def invalidate_product(redis: aioredis.Redis, product_id: int) -> None:
    await redis.delete(f"cache:product:product_id:{product_id}")


@app.put("/products/{product_id}")
async def update_product(
    product_id: int,
    payload: ProductUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict:
    product = await db_update_product(db, product_id, payload)
    await db.commit()
    # Invalidate immediately after write
    await invalidate_product(request.app.state.redis, product_id)
    return product
```

**`fastapi-cache2` library** — provides this pattern with decorators and supports Redis, memcached, and in-memory backends:

```python
from fastapi_cache import FastAPICache
from fastapi_cache.backends.redis import RedisBackend
from fastapi_cache.decorator import cache

@app.on_event("startup")
async def startup():
    redis = aioredis.from_url("redis://localhost")
    FastAPICache.init(RedisBackend(redis), prefix="myapp-cache")

@app.get("/products/{product_id}")
@cache(expire=300)
async def get_product(product_id: int) -> dict:
    return await db_fetch_product(product_id)
```

`fastapi-cache2` auto-generates cache keys from the function name and arguments, supports `namespace` partitioning, and provides `invalidate` helpers. The downside: its key generation may include internal parameters (like `request`) that you don't want in the key — review the generated keys.

---

### Q3: What is a cache stampede and how do you prevent it?

**Model answer:**

A **cache stampede** (thundering herd) occurs when a popular key expires and many concurrent requests simultaneously get a cache miss. Each request independently fetches from the DB and tries to write the same value back to the cache. The DB sees `N` simultaneous queries for the same data where `N` is the number of concurrent requests at that moment. For a hot key at high traffic, this can be hundreds of queries simultaneously — potentially overwhelming the DB.

**Prevention strategies:**

**1. Redis mutex (get-or-lock):** only one request recomputes; others wait.

```python
import asyncio
import json
import redis.asyncio as aioredis

LOCK_TTL = 10       # seconds — must exceed max recomputation time
WAIT_POLL = 0.05    # poll interval while waiting for lock


async def get_or_compute(
    redis: aioredis.Redis,
    key: str,
    ttl: int,
    compute: Callable[[], Awaitable[Any]],
) -> Any:
    # Fast path: cache hit
    val = await redis.get(key)
    if val is not None:
        return json.loads(val)

    lock_key = f"{key}:lock"

    # Try to acquire lock with SET NX EX (atomic)
    acquired = await redis.set(lock_key, "1", ex=LOCK_TTL, nx=True)

    if acquired:
        # We won the lock — recompute and populate
        try:
            result = await compute()
            await redis.setex(key, ttl, json.dumps(result))
            return result
        finally:
            await redis.delete(lock_key)
    else:
        # Another request is recomputing — wait for it
        deadline = asyncio.get_event_loop().time() + LOCK_TTL
        while asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(WAIT_POLL)
            val = await redis.get(key)
            if val is not None:
                return json.loads(val)
        # Lock holder timed out — fall through to direct DB fetch
        return await compute()
```

**2. Probabilistic early expiration (XFetch):** slightly before the key expires, some requests proactively recompute — spreading the recomputation over time rather than concentrating it at expiry.

```python
import math, random, time

async def probabilistic_get(
    redis: aioredis.Redis,
    key: str,
    ttl: int,
    beta: float,   # 1.0 = standard; higher = more eager recomputation
    compute: Callable[[], Awaitable[Any]],
) -> Any:
    data = await redis.get(key)
    if data is not None:
        payload = json.loads(data)
        expiry = await redis.ttl(key)
        # XFetch formula: recompute early with probability proportional to (remaining_ttl → 0)
        if expiry > 0 and (-beta * math.log(random.random())) < (ttl - expiry):
            return payload["value"]   # still fresh enough for this request
        # else: fall through to recompute (with some probability)

    result = await compute()
    await redis.setex(key, ttl, json.dumps({"value": result}))
    return result
```

XFetch is lock-free and naturally distributes recomputation — multiple requests may recompute simultaneously near expiry, but they do so gradually rather than in one synchronous burst. Preferable when the DB can handle occasional redundant queries and you want to avoid lock contention.

**3. Stale-while-revalidate:** serve the stale value immediately and trigger background recomputation.

```python
async def stale_while_revalidate(
    redis: aioredis.Redis,
    key: str,
    ttl: int,
    stale_ttl: int,    # how long stale data is acceptable (stale_ttl > ttl)
    compute: Callable[[], Awaitable[Any]],
) -> Any:
    stale_key = f"{key}:stale"
    recomputing_key = f"{key}:recomputing"

    val = await redis.get(key)
    if val is not None:
        return json.loads(val)  # fresh hit

    stale = await redis.get(stale_key)
    if stale is not None:
        # Serve stale immediately; trigger background refresh only once
        is_recomputing = await redis.set(recomputing_key, "1", ex=ttl, nx=True)
        if is_recomputing:
            asyncio.create_task(_refresh(redis, key, stale_key, ttl, stale_ttl, compute))
        return json.loads(stale)

    # No stale data either — must wait for fresh data
    result = await compute()
    await asyncio.gather(
        redis.setex(key, ttl, json.dumps(result)),
        redis.setex(stale_key, stale_ttl, json.dumps(result)),
    )
    return result


async def _refresh(redis, key, stale_key, ttl, stale_ttl, compute):
    result = await compute()
    await asyncio.gather(
        redis.setex(key, ttl, json.dumps(result)),
        redis.setex(stale_key, stale_ttl, json.dumps(result)),
    )
    await redis.delete(f"{key}:recomputing")
```

**Gotcha follow-up:** The mutex approach uses `asyncio.sleep(WAIT_POLL)` to poll. What's wrong with this if `WAIT_POLL` is very small, and what's a better mechanism?

Tight polling (e.g., 1ms) creates a busy-wait loop inside the event loop, blocking the cooperative scheduler from handling other requests. At 1ms poll with 100 concurrent waiters, you're scheduling 100,000 wake-ups per second doing nothing. Use `asyncio.wait_for` with Redis `BLPOP` or pub/sub notification instead: the lock holder publishes to a channel on release; waiters block on `subscribe` rather than polling.

---

### Q4: How do you handle cache invalidation across multiple routes that share the same underlying data?

**Model answer:**

This is the hardest problem in caching. A `PUT /products/{id}` must invalidate:
- `GET /products/{id}` (single product)
- `GET /products` (list that includes the product)
- `GET /categories/{category_id}` (if the product is in this category)
- `GET /search?q=...` (any search that might have returned this product)

**Tag-based invalidation** assigns cache keys to logical groups (tags). When data changes, delete all keys with that tag.

```python
import json
from typing import Any
import redis.asyncio as aioredis


class TaggedCache:
    """Cache where entries can be grouped by tag for bulk invalidation."""

    def __init__(self, redis: aioredis.Redis, prefix: str = "cache") -> None:
        self.redis = redis
        self.prefix = prefix

    def _key(self, key: str) -> str:
        return f"{self.prefix}:{key}"

    def _tag_set_key(self, tag: str) -> str:
        return f"{self.prefix}:tag:{tag}"

    async def set(self, key: str, value: Any, ttl: int, tags: list[str] = []) -> None:
        full_key = self._key(key)
        pipe = self.redis.pipeline()
        pipe.setex(full_key, ttl, json.dumps(value))
        for tag in tags:
            # Add this key to each tag's set; set a generous TTL on the tag set itself
            pipe.sadd(self._tag_set_key(tag), full_key)
            pipe.expire(self._tag_set_key(tag), ttl * 2)
        await pipe.execute()

    async def get(self, key: str) -> Any | None:
        val = await self.redis.get(self._key(key))
        return json.loads(val) if val is not None else None

    async def invalidate_tag(self, tag: str) -> int:
        tag_key = self._tag_set_key(tag)
        keys = await self.redis.smembers(tag_key)
        if not keys:
            return 0
        pipe = self.redis.pipeline()
        pipe.delete(*keys)
        pipe.delete(tag_key)
        await pipe.execute()
        return len(keys)


# Usage: endpoints tag their cache entries
@app.get("/products/{product_id}")
async def get_product(product_id: int, request: Request) -> dict:
    cache: TaggedCache = request.app.state.cache
    cache_key = f"product:{product_id}"

    hit = await cache.get(cache_key)
    if hit is not None:
        return hit

    product = await db_fetch_product(product_id)
    await cache.set(
        cache_key, product, ttl=300,
        tags=[
            f"product:{product_id}",           # invalidate this specific product
            f"category:{product['category_id']}",  # invalidate all in this category
            "products:list",                    # invalidate any list endpoint
        ],
    )
    return product


@app.put("/products/{product_id}")
async def update_product(product_id: int, payload: dict, request: Request) -> dict:
    product = await db_update_product(product_id, payload)
    cache: TaggedCache = request.app.state.cache
    # One call invalidates product detail, all lists it appears in, and category views
    await cache.invalidate_tag(f"product:{product_id}")
    return product
```

**Pattern limitations:** tag sets can grow unboundedly for tags like `"products:list"` if you never clean them up. Expired cache keys linger in the tag set as phantom members. Two mitigations: (1) use `SSCAN` + `EXISTS` on members during invalidation to prune dead entries, (2) set a shorter TTL on tag sets than on the values they track.

**Simpler alternative for structured data:** namespace versioning. Store a version counter in Redis; include the version in every cache key. Invalidating a namespace bumps the counter — all existing keys become unreachable (and will be evicted by TTL).

```python
async def get_namespace_version(redis: aioredis.Redis, ns: str) -> int:
    return int(await redis.get(f"ns:{ns}") or 1)

async def invalidate_namespace(redis: aioredis.Redis, ns: str) -> None:
    await redis.incr(f"ns:{ns}")  # atomic; old version keys never found again

# Cache key: f"cache:products:{version}:{product_id}"
```

---

### Q5: When is in-process caching appropriate in a FastAPI application, and what are its failure modes?

**Model answer:**

In-process caching (e.g., `functools.lru_cache`, `cachetools.TTLCache`) stores data in the worker process's memory — no network round-trip, no serialization. Reads are nanosecond-speed. It is appropriate for:

- **Configuration and feature flags:** values that change rarely (hours/days), where a brief staleness period is acceptable
- **Static reference data:** country codes, currency list, enum values fetched from DB once at startup
- **Expensive pure-function results:** computation results (parsing, regex compilation, schema validation)
- **Single-worker deployments:** development, lambda functions, scripts

**The multi-process failure mode:**

```python
# This looks correct but breaks with multiple Gunicorn workers
from functools import lru_cache

@lru_cache(maxsize=1)
def get_feature_flags() -> dict:
    return db_fetch_feature_flags()  # called once per process

# Worker 1: cache = {"new_ui": True}   (updated at 10:00)
# Worker 2: cache = {"new_ui": False}  (stale, not yet updated)
# Worker 3: cache = {"new_ui": True}
# → Feature is enabled for ~33% of requests, disabled for ~33%
```

Invalidating `lru_cache` in one worker (`get_feature_flags.cache_clear()`) does not affect other workers. With 4 Gunicorn workers, you have 4 independent caches. Invalidation via an API endpoint hits one worker at random.

**Correct in-process caching for multi-worker FastAPI:**

```python
import asyncio
import time
from typing import Any

class TTLCache:
    """Per-worker in-process cache with TTL. Acceptable for low-consistency data."""

    def __init__(self, ttl: int) -> None:
        self._store: dict[str, tuple[Any, float]] = {}  # key → (value, expiry_ts)
        self._ttl = ttl

    def get(self, key: str) -> Any | None:
        entry = self._store.get(key)
        if entry is None:
            return None
        value, expiry = entry
        if time.monotonic() > expiry:
            del self._store[key]
            return None
        return value

    def set(self, key: str, value: Any) -> None:
        self._store[key] = (value, time.monotonic() + self._ttl)

    def invalidate(self, key: str) -> None:
        self._store.pop(key, None)


# Mount on app.state at startup — shared within one worker process
@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.local_cache = TTLCache(ttl=60)   # 60s TTL — each worker refreshes independently
    app.state.redis = await aioredis.from_url("redis://localhost")
    yield
    await app.state.redis.aclose()
```

**Two-level (L1/L2) caching pattern:**

```python
async def get_product_cached(
    product_id: int,
    local: TTLCache,       # L1: in-process, 10s TTL
    redis: aioredis.Redis, # L2: shared Redis, 5min TTL
) -> dict:
    # L1 hit (nanoseconds)
    val = local.get(f"product:{product_id}")
    if val is not None:
        return val

    # L2 hit (sub-millisecond, one network hop)
    raw = await redis.get(f"cache:product:{product_id}")
    if raw is not None:
        result = json.loads(raw)
        local.set(f"product:{product_id}", result)  # warm L1
        return result

    # Cache miss: hit DB
    result = await db_fetch_product(product_id)
    await asyncio.gather(
        redis.setex(f"cache:product:{product_id}", 300, json.dumps(result)),
    )
    local.set(f"product:{product_id}", result)
    return result
```

At 10,000 req/s with a 10s L1 TTL, even with 4 workers this means at most 4 Redis reads per 10-second window per product — vs 100,000 Redis reads per product without L1. Each worker self-heals within 10 seconds of a Redis update.

---

## Code: Production Caching Layer with Stampede Protection

```python
"""
Multi-layer cache with stampede protection, tag-based invalidation,
and full instrumentation.
"""
import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Callable, Awaitable

import redis.asyncio as aioredis
from fastapi import Depends, FastAPI, Request, Response

log = logging.getLogger(__name__)


# ── L1: in-process per-worker cache ──────────────────────────────────────

@dataclass
class CacheEntry:
    value: Any
    expires_at: float


class LocalCache:
    def __init__(self, default_ttl: int = 30) -> None:
        self._data: dict[str, CacheEntry] = {}
        self._default_ttl = default_ttl

    def get(self, key: str) -> tuple[bool, Any]:
        entry = self._data.get(key)
        if entry is None or time.monotonic() > entry.expires_at:
            self._data.pop(key, None)
            return False, None
        return True, entry.value

    def set(self, key: str, value: Any, ttl: int | None = None) -> None:
        self._data[key] = CacheEntry(
            value=value,
            expires_at=time.monotonic() + (ttl or self._default_ttl),
        )

    def delete(self, key: str) -> None:
        self._data.pop(key, None)

    def clear_prefix(self, prefix: str) -> int:
        keys = [k for k in self._data if k.startswith(prefix)]
        for k in keys:
            del self._data[k]
        return len(keys)


# ── L2: shared Redis cache ────────────────────────────────────────────────

class CacheManager:
    def __init__(self, redis: aioredis.Redis, local: LocalCache, prefix: str = "c") -> None:
        self.redis = redis
        self.local = local
        self.prefix = prefix
        self._lock_script = redis.register_script("""
            return redis.call('SET', KEYS[1], ARGV[1], 'NX', 'EX', ARGV[2])
        """)

    def _rkey(self, key: str) -> str:
        return f"{self.prefix}:{key}"

    async def get(self, key: str) -> tuple[bool, Any]:
        # L1 check
        hit, val = self.local.get(key)
        if hit:
            return True, val

        # L2 check
        raw = await self.redis.get(self._rkey(key))
        if raw is not None:
            val = json.loads(raw)
            self.local.set(key, val)
            return True, val

        return False, None

    async def set(
        self,
        key: str,
        value: Any,
        ttl: int,
        tags: list[str] = [],
        local_ttl: int | None = None,
    ) -> None:
        rkey = self._rkey(key)
        pipe = self.redis.pipeline()
        pipe.setex(rkey, ttl, json.dumps(value))
        for tag in tags:
            pipe.sadd(f"{self.prefix}:tag:{tag}", rkey)
            pipe.expire(f"{self.prefix}:tag:{tag}", ttl + 60)
        await pipe.execute()
        self.local.set(key, value, ttl=local_ttl or min(ttl, 30))

    async def invalidate(self, key: str) -> None:
        await self.redis.delete(self._rkey(key))
        self.local.delete(key)

    async def invalidate_tag(self, tag: str) -> int:
        tag_key = f"{self.prefix}:tag:{tag}"
        keys = await self.redis.smembers(tag_key)
        if not keys:
            return 0
        pipe = self.redis.pipeline()
        pipe.delete(*keys)
        pipe.delete(tag_key)
        await pipe.execute()
        prefix_len = len(self.prefix) + 1
        for k in keys:
            self.local.delete(k.decode()[prefix_len:])
        return len(keys)

    async def get_or_compute(
        self,
        key: str,
        compute: Callable[[], Awaitable[Any]],
        ttl: int,
        tags: list[str] = [],
        lock_ttl: int = 10,
    ) -> Any:
        """Cache-aside with mutex stampede protection."""
        hit, val = await self.get(key)
        if hit:
            return val

        lock_key = f"{self._rkey(key)}:lock"
        acquired = await self._lock_script(
            keys=[lock_key], args=["1", str(lock_ttl)]
        )

        if acquired:
            try:
                result = await compute()
                await self.set(key, result, ttl=ttl, tags=tags)
                return result
            except Exception:
                raise
            finally:
                await self.redis.delete(lock_key)
        else:
            # Wait for lock holder to populate cache
            for _ in range(int(lock_ttl / 0.05)):
                await asyncio.sleep(0.05)
                hit, val = await self.get(key)
                if hit:
                    return val
            # Fallback: compute directly if lock holder failed
            log.warning("Cache lock wait timeout for key=%s; computing directly", key)
            return await compute()


# ── FastAPI wiring ────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    redis = await aioredis.from_url("redis://localhost:6379")
    local = LocalCache(default_ttl=30)
    app.state.cache = CacheManager(redis, local)
    yield
    await redis.aclose()


app = FastAPI(lifespan=lifespan)


def get_cache(request: Request) -> CacheManager:
    return request.app.state.cache


# ── Example endpoints ─────────────────────────────────────────────────────

@app.get("/products/{product_id}")
async def get_product(
    product_id: int,
    response: Response,
    cache: CacheManager = Depends(get_cache),
) -> dict:
    async def _fetch():
        return await db_fetch_product(product_id)

    product = await cache.get_or_compute(
        key=f"product:{product_id}",
        compute=_fetch,
        ttl=300,
        tags=[f"product:{product_id}", f"cat:{product_id}"],  # tag for bulk invalidation
    )
    response.headers["Cache-Control"] = "private, max-age=60"
    return product


@app.put("/products/{product_id}")
async def update_product(
    product_id: int,
    payload: dict,
    cache: CacheManager = Depends(get_cache),
) -> dict:
    product = await db_update_product(product_id, payload)
    await cache.invalidate_tag(f"product:{product_id}")
    return product


@app.get("/products")
async def list_products(
    cache: CacheManager = Depends(get_cache),
) -> list:
    return await cache.get_or_compute(
        key="products:list",
        compute=db_fetch_all_products,
        ttl=60,
        tags=["products:list"],
    )
```

---

## Under the Hood

**Redis `SET NX EX` for distributed locks:** `SET key value NX EX seconds` is atomic — it sets the key only if it doesn't exist (NX) and sets a TTL in the same command (EX). Crucially, the NX + EX is a single atomic operation: there is no window between "set if not exists" and "set expiry" where the key could be left without a TTL (which would cause a permanent lock on process crash). Before `SET NX EX` was available (Redis < 2.6.12), people used `SETNX` + `EXPIRE` in two commands — if the process crashed between the two, the lock key had no TTL and lived forever.

**`lru_cache` and async functions:** `@functools.lru_cache` does not work correctly on `async def` functions — it caches the coroutine object, not the result. The coroutine is a truthy object, so cache "hits" return a coroutine that was already awaited (and is exhausted). Use `async-lru` (`pip install async-lru`) which provides `@alru_cache` with the same interface but correct async semantics.

**HTTP `stale-while-revalidate` at the CDN layer:** this directive (RFC 5861) instructs CDNs like Cloudflare and Fastly to serve stale content immediately while fetching a fresh copy in the background. From the client's perspective, the response is always fast. From the origin's perspective, revalidation requests arrive as a slow, steady trickle rather than a burst on TTL expiry. It's the HTTP equivalent of the stale-while-revalidate application pattern — and costs nothing to implement (just a response header).
