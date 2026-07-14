# Rate Limiting from Scratch and Idempotency Keys

## Concept

**Rate limiting** protects endpoints from abuse by capping how many requests a client can make in a time window. FastAPI has no built-in rate limiter; `slowapi` (a port of Flask-Limiter) is the common library, but understanding the token bucket algorithm from scratch is the Staff-level expectation.

**Token bucket algorithm:**
- Each client gets a "bucket" with capacity `N` tokens
- Tokens refill at rate `R` per second
- Each request consumes 1 token
- If the bucket is empty, the request is rejected (429)
- Implementation: store `(tokens, last_refill_time)` in Redis per client key

**Idempotency keys** ensure that retried POST requests (due to network failures, client timeouts) don't create duplicate resources. The pattern:
- Client sends `Idempotency-Key: <uuid>` header with every mutating request
- Server checks Redis/DB for this key
- If present: return the cached response (no re-execution)
- If absent: execute the operation, cache the response with the key, return response
- Key expires after a TTL (typically 24h)

---

## Interview Questions

### Q1: Implement a token bucket rate limiter as a FastAPI dependency. What are the Redis atomicity concerns?

**Model answer:**

The atomicity concern: "check token count → decrement" is a read-modify-write operation. If not atomic, two concurrent requests can both read `tokens=1`, both decrement to 0, and both succeed — effectively doubling allowed requests.

**Solution:** use a Redis Lua script, which executes atomically on the Redis server:

```python
import time
import redis.asyncio as aioredis
from fastapi import FastAPI, Depends, HTTPException, Request, status

RATE_LIMIT_SCRIPT = """
local key = KEYS[1]
local capacity = tonumber(ARGV[1])
local refill_rate = tonumber(ARGV[2])  -- tokens per second
local now = tonumber(ARGV[3])
local requested = tonumber(ARGV[4])

local bucket = redis.call('HMGET', key, 'tokens', 'last_refill')
local tokens = tonumber(bucket[1]) or capacity
local last_refill = tonumber(bucket[2]) or now

-- Refill tokens based on elapsed time
local elapsed = math.max(0, now - last_refill)
local refill = elapsed * refill_rate
tokens = math.min(capacity, tokens + refill)

if tokens >= requested then
    tokens = tokens - requested
    redis.call('HMSET', key, 'tokens', tokens, 'last_refill', now)
    redis.call('EXPIRE', key, math.ceil(capacity / refill_rate) + 1)
    return {1, math.floor(tokens)}
else
    redis.call('HMSET', key, 'tokens', tokens, 'last_refill', now)
    redis.call('EXPIRE', key, math.ceil(capacity / refill_rate) + 1)
    return {0, math.floor(tokens)}
end
"""


class TokenBucketRateLimiter:
    def __init__(
        self,
        redis_client: aioredis.Redis,
        capacity: int = 100,
        refill_rate: float = 10.0,  # tokens/second
        key_prefix: str = "rate_limit",
    ):
        self.redis = redis_client
        self.capacity = capacity
        self.refill_rate = refill_rate
        self.key_prefix = key_prefix
        self._script = None

    async def _get_script(self):
        if self._script is None:
            self._script = self.redis.register_script(RATE_LIMIT_SCRIPT)
        return self._script

    async def check(self, key: str) -> tuple[bool, int]:
        script = await self._get_script()
        redis_key = f"{self.key_prefix}:{key}"
        result = await script(
            keys=[redis_key],
            args=[self.capacity, self.refill_rate, time.time(), 1],
        )
        allowed, remaining = result
        return bool(allowed), int(remaining)


# App-level setup
app = FastAPI()

@app.on_event("startup")
async def startup():
    redis = await aioredis.from_url("redis://localhost:6379", decode_responses=True)
    app.state.limiter = TokenBucketRateLimiter(redis, capacity=100, refill_rate=10.0)


# Dependency factory
def rate_limit(capacity: int = 100, refill_rate: float = 10.0):
    async def _check(request: Request) -> None:
        # Key by IP — production: use authenticated user ID
        client_key = request.client.host if request.client else "unknown"
        limiter: TokenBucketRateLimiter = request.app.state.limiter
        allowed, remaining = await limiter.check(client_key)
        if not allowed:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Rate limit exceeded",
                headers={
                    "Retry-After": "1",
                    "X-RateLimit-Remaining": "0",
                },
            )
        # Could add remaining to response headers here
    return _check


@app.post("/orders", dependencies=[Depends(rate_limit(capacity=10, refill_rate=1.0))])
async def create_order():
    return {"created": True}
```

**Gotcha follow-up:** How does this work under multiple Gunicorn workers?

Each worker shares the same Redis instance, so the rate limit is enforced globally across all workers. This is the correct behavior — the Lua script's atomicity ensures correctness regardless of how many workers read/write simultaneously.

---

### Q2: Why is simple caching not sufficient for idempotency keys? What edge cases does it miss?

**Model answer:**

**Simple caching** stores "has this key been used?" → return cached response. This misses:

**1. In-flight deduplication (concurrent duplicate requests):**
If two requests with the same key arrive simultaneously, both find "not cached" and both execute. You need an atomic "check and reserve" — set the key to "in-progress" before executing, complete with "done + response" after.

**2. Response content needs to be stored, not just status:**
A cache of `key → True` tells you the request was processed but you need to return the *original response*. Store the full serialized response.

**3. TTL and key expiry strategy:**
The idempotency key should expire after a known window (24h for payment APIs). A cache that's LRU-evicted might evict a key before the client's retry window closes.

**4. Partial failures:**
If the operation succeeded but the response write to Redis failed (network partition), the next retry will re-execute. Need transactional write (Lua script or DB transaction) that commits the idempotency record atomically with the operation.

**5. Different request body, same key:**
Idempotency requires that the same key + same body always returns the same response. If a client reuses a key with a different body, you should return 422 (or the original response and ignore the new body). Cache-only approaches don't validate body consistency.

---

### Q3: How does slowapi implement rate limiting, and where does it differ from a hand-rolled approach?

**Model answer:**

`slowapi` is a FastAPI/Starlette port of Flask-Limiter. It:
1. Uses `limits` library under the hood for the rate limiting algorithms (fixed window, sliding window, token bucket)
2. Supports multiple storage backends (Redis, in-memory, Memcached)
3. Integrates via a Starlette `middleware` + a `Limiter` class with `@limiter.limit("10/minute")` decorator syntax

**Key differences from hand-rolled:**

| Aspect | slowapi | Hand-rolled |
|--------|---------|-------------|
| Algorithm | Fixed window (default), sliding window | Token bucket (smoother) |
| Storage | Pluggable via `limits` library | Direct Redis |
| Per-route vs global | Per-route via decorator | Dependency (per-route or global) |
| Error format | Starlette's 429 default | Your choice |
| Key function | IP by default, customizable | Your choice |
| Multi-worker | Redis-backed (shared) | Redis-backed (shared) |

The main architectural issue with `slowapi`: it uses a middleware that intercepts all requests, but rate limit decorators are applied per-route in a non-standard way (monkey-patching route functions). This can cause issues with FastAPI's dependency injection system and doesn't work well with `APIRouter`.

For a production system where rate limiting is a first-class concern with different limits per tier/user, a hand-rolled dependency is more maintainable.

---

## Code: Full Idempotency Key Dependency

```python
import hashlib
import json
import time
from typing import Any
import redis.asyncio as aioredis
from fastapi import FastAPI, Depends, Request, Response, HTTPException, status
from fastapi.responses import JSONResponse


IDEMPOTENCY_SCRIPT = """
local key = KEYS[1]
local ttl = tonumber(ARGV[1])

local existing = redis.call('GET', key)
if existing then
    return existing  -- cached response
end

-- Mark as in-progress (prevents concurrent duplicates)
redis.call('SET', key, '"IN_PROGRESS"', 'EX', 30)  -- 30s lock
return nil
"""

IDEMPOTENCY_COMPLETE_SCRIPT = """
local key = KEYS[1]
local response_json = ARGV[1]
local ttl = tonumber(ARGV[2])
redis.call('SET', key, response_json, 'EX', ttl)
return 1
"""


class IdempotencyStore:
    def __init__(self, redis: aioredis.Redis, ttl: int = 86400):
        self.redis = redis
        self.ttl = ttl

    async def get_or_reserve(self, key: str) -> str | None:
        result = await self.redis.eval(IDEMPOTENCY_SCRIPT, 1, key, self.ttl)
        return result  # None if new, JSON string if cached/in-progress

    async def complete(self, key: str, response_data: Any, status_code: int) -> None:
        payload = json.dumps({"data": response_data, "status": status_code})
        await self.redis.eval(IDEMPOTENCY_COMPLETE_SCRIPT, 1, key, payload, self.ttl)


def idempotency_key(
    required: bool = True,
    ttl: int = 86400,
):
    async def _dependency(
        request: Request,
        response: Response,
    ) -> tuple[str | None, IdempotencyStore | None]:
        key_header = request.headers.get("Idempotency-Key")

        if not key_header:
            if required:
                raise HTTPException(
                    status_code=422,
                    detail="Idempotency-Key header is required",
                )
            return None, None

        # Include request body hash to detect body mismatch
        body = await request.body()
        body_hash = hashlib.sha256(body).hexdigest()[:16]
        redis_key = f"idempotency:{key_header}:{body_hash}"

        store: IdempotencyStore = request.app.state.idempotency_store
        cached = await store.get_or_reserve(redis_key)

        if cached == '"IN_PROGRESS"':
            raise HTTPException(
                status_code=409,
                detail="Duplicate request in progress",
            )

        if cached:
            payload = json.loads(cached)
            # Return cached response immediately — endpoint won't execute
            # Note: raising here short-circuits the route handler
            raise IdempotencyCacheHit(payload["data"], payload["status"])

        return redis_key, store

    return _dependency


class IdempotencyCacheHit(Exception):
    def __init__(self, data: Any, status_code: int):
        self.data = data
        self.status_code = status_code


# Register the cache-hit handler
app = FastAPI()

@app.exception_handler(IdempotencyCacheHit)
async def idempotency_hit_handler(request: Request, exc: IdempotencyCacheHit):
    return JSONResponse(
        status_code=exc.status_code,
        content=exc.data,
        headers={"X-Idempotency-Replayed": "true"},
    )


IdempotencyDep = Depends(idempotency_key(required=True))


@app.post("/payments")
async def create_payment(
    body: dict,
    idempotency: tuple = IdempotencyDep,
) -> dict:
    redis_key, store = idempotency

    # Process payment (expensive, must not run twice)
    result = {"payment_id": "pay_123", "status": "completed"}

    # Cache the response for future retries
    if redis_key and store:
        await store.complete(redis_key, result, 200)

    return result
```

---

## Under the Hood

**Token bucket in Redis:** The Lua script runs atomically because Redis is single-threaded for command execution. The script reads and writes happen in a single CPU tick on the Redis server — no other command can interleave. This is the standard Redis pattern for any compare-and-swap operation.

**Idempotency key pattern:** the "in-progress" lock (30-second TTL) handles the race condition where two requests arrive simultaneously. Only one gets `nil` from `GET` and proceeds to `SET "IN_PROGRESS"`. The second gets `"IN_PROGRESS"` and returns 409. After the first request completes, it replaces `"IN_PROGRESS"` with the actual response. Subsequent retries (even after the 30s lock expires, in the failure case) will see the final response.

The 30-second lock TTL should exceed the maximum expected processing time. For payments or other long operations, use a longer lock TTL or implement a "heartbeat" that extends the lock while processing.
