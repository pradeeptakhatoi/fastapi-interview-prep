# Rate Limiting and Throttling in FastAPI

## Concept

Rate limiting controls how many requests a client can make in a given time window. Throttling is the enforcement mechanism — slowing or blocking traffic that exceeds the limit. In FastAPI, both are implemented as dependencies or middleware that run before the route handler.

**Why this is hard in production:**
1. **Distributed state** — with multiple Gunicorn workers or pods, in-process counters only see their own traffic. You need a shared store (Redis) for accurate counting.
2. **Algorithm choice** — fixed window, sliding window, token bucket, and leaky bucket have different burst characteristics and implementation complexity. Choosing wrong means either false 429s or burst storms that slip through.
3. **Key design** — limiting by IP is easy but wrong for shared NAT (thousands of users behind one IP) and bypassable from different IPs. Limiting by authenticated user ID or API key is correct but requires auth to run first.
4. **Headers** — clients need `X-RateLimit-*` and `Retry-After` to back off intelligently. Missing these turns rate limiting into a debugging nightmare.

**Algorithm comparison:**

| Algorithm | Burst behavior | Memory | Complexity | Best for |
|-----------|---------------|--------|------------|----------|
| Fixed window | Allows 2× burst at window boundary | O(1) per key | Low | Simple quota enforcement |
| Sliding window (log) | Exact, no boundary burst | O(n) per key | High | Strict per-user quotas |
| Sliding window (counter) | Approximate, no boundary burst | O(1) per key | Medium | High-traffic APIs |
| Token bucket | Controlled burst up to capacity | O(1) per key | Medium | APIs with burst tolerance |
| Leaky bucket | Smoothed output, no burst | O(1) per key | Medium | Downstream rate protection |

---

## Interview Questions

### Q1: Explain the token bucket algorithm and why it requires a Lua script in Redis for correctness.

**Model answer:**

**Token bucket:** a bucket holds up to `capacity` tokens. Tokens refill at `rate` tokens/second continuously. Each request consumes `cost` tokens (default 1). If the bucket has enough tokens, the request is allowed; otherwise it's rejected with 429.

Unlike fixed windows, token bucket allows short bursts up to `capacity` while enforcing a sustained rate of `rate` req/s. A bucket with capacity=100, rate=10 allows a burst of 100 requests instantly, then 10 requests/second thereafter.

**Why Lua?** The check-and-decrement is two operations:
1. Read current tokens and last-refill timestamp
2. Compute new token count (refill since last check), subtract cost, write back

Between operations 1 and 2, another request on a different connection can read the same (stale) token count and also be allowed. Both consume tokens, but only one decrement is recorded — the other is lost (TOCTOU race condition).

Lua scripts in Redis execute atomically — Redis is single-threaded and runs the entire script as one uninterruptible unit. No other command executes between the read and the write.

```lua
-- Atomic token bucket in Lua
local key      = KEYS[1]
local capacity = tonumber(ARGV[1])
local rate     = tonumber(ARGV[2])   -- tokens per second
local now      = tonumber(ARGV[3])   -- current time (float seconds)
local cost     = tonumber(ARGV[4])

local bucket  = redis.call('HMGET', key, 'tokens', 'ts')
local tokens  = tonumber(bucket[1]) or capacity  -- default: full bucket
local ts      = tonumber(bucket[2]) or now

-- Refill: add tokens proportional to time elapsed since last request
local elapsed = math.max(0, now - ts)
tokens = math.min(capacity, tokens + elapsed * rate)

if tokens >= cost then
    tokens = tokens - cost
    redis.call('HMSET', key, 'tokens', tokens, 'ts', now)
    redis.call('PEXPIRE', key, math.ceil(capacity / rate * 1000))
    return {1, math.floor(tokens)}   -- allowed, remaining
else
    -- Still write back the refilled (but insufficient) token count + new ts
    redis.call('HMSET', key, 'tokens', tokens, 'ts', now)
    redis.call('PEXPIRE', key, math.ceil(capacity / rate * 1000))
    return {0, math.floor(tokens)}   -- denied, remaining (0)
end
```

**Gotcha follow-up:** What is `PEXPIRE` doing here and why is the TTL set to `capacity / rate * 1000` ms?

`PEXPIRE` sets a TTL on the key so idle clients' buckets are cleaned up automatically — no separate cleanup job needed. The TTL is the time it would take to fully refill an empty bucket: `capacity / rate` seconds. After this long with no requests, the bucket would be full again — so the key can be safely expired and recreated as a full bucket on the next request. This is functionally equivalent to having a full bucket.

---

### Q2: How do you implement tiered rate limits — different limits for free vs paid vs internal clients — in FastAPI?

**Model answer:**

Tiered limits are a dependency composition problem: the rate limit parameters (capacity, rate) are determined by the authenticated user's tier, then the same token bucket logic applies.

```python
from dataclasses import dataclass
from enum import Enum
from fastapi import Depends, HTTPException, Request, status
import redis.asyncio as aioredis
import time

class Tier(str, Enum):
    free = "free"
    paid = "paid"
    internal = "internal"

@dataclass
class RateLimitConfig:
    capacity: int       # burst size
    rate: float         # tokens/second sustained
    cost: int = 1       # tokens per request

TIER_LIMITS: dict[Tier, RateLimitConfig] = {
    Tier.free:     RateLimitConfig(capacity=20,   rate=1.0),    # 20 burst, 1/s
    Tier.paid:     RateLimitConfig(capacity=500,  rate=50.0),   # 500 burst, 50/s
    Tier.internal: RateLimitConfig(capacity=10000, rate=1000.0), # effectively unlimited
}


class TieredRateLimiter:
    def __init__(self, redis: aioredis.Redis) -> None:
        self.redis = redis
        self._script = redis.register_script(_LUA_SCRIPT)  # same Lua as above

    async def check(
        self,
        key: str,
        config: RateLimitConfig,
    ) -> tuple[bool, int, float]:
        """Returns (allowed, remaining_tokens, retry_after_seconds)."""
        allowed, remaining = await self._script(
            keys=[f"rl:{key}"],
            args=[config.capacity, config.rate, time.time(), config.cost],
        )
        retry_after = config.cost / config.rate if not allowed else 0.0
        return bool(allowed), int(remaining), retry_after


def rate_limit(cost: int = 1):
    """Dependency factory — wraps tiered limiting around any endpoint."""
    async def _dep(
        request: Request,
        current_user=Depends(get_current_user_optional),  # None for anonymous
    ) -> None:
        limiter: TieredRateLimiter = request.app.state.limiter

        # Determine tier and key
        if current_user is None:
            tier = Tier.free
            key = f"ip:{request.client.host}"
        elif current_user.is_internal:
            tier = Tier.internal
            key = f"user:{current_user.id}"
        elif current_user.is_paid:
            tier = Tier.paid
            key = f"user:{current_user.id}"
        else:
            tier = Tier.free
            key = f"user:{current_user.id}"  # free users: per-user, not per-IP

        config = TIER_LIMITS[tier]
        config = RateLimitConfig(config.capacity, config.rate, cost)

        allowed, remaining, retry_after = await limiter.check(key, config)

        # Always set headers so clients can track their quota
        request.state.rate_limit_remaining = remaining
        request.state.rate_limit_limit = config.capacity

        if not allowed:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail={
                    "error": "rate_limit_exceeded",
                    "tier": tier,
                    "retry_after": retry_after,
                },
                headers={
                    "X-RateLimit-Limit": str(config.capacity),
                    "X-RateLimit-Remaining": "0",
                    "X-RateLimit-Policy": f"{config.capacity};w={int(1/config.rate)}",
                    "Retry-After": str(int(retry_after) + 1),
                },
            )

    return Depends(_dep)


# Usage
@app.get("/search", dependencies=[rate_limit(cost=1)])
async def search(q: str) -> list:
    ...

@app.post("/export", dependencies=[rate_limit(cost=10)])  # expensive operation
async def export() -> dict:
    ...
```

**Key design decisions:**
- Free anonymous users are rate-limited per IP. Authenticated free users are limited per user ID — prevents creating throwaway accounts to reset limits.
- Internal service clients skip meaningful limits but still go through the same code path (no `if internal: return` shortcut) — this avoids a class of bugs where adding limit checks forgets the internal bypass.
- The `cost` parameter on `rate_limit()` expresses the relative expense of the endpoint — bulk exports cost more tokens than simple reads.

---

### Q3: How do you implement rate limiting as middleware rather than a dependency, and what are the trade-offs?

**Model answer:**

**Middleware approach:** apply to all routes uniformly, before routing occurs.

```python
import time
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response


class RateLimitMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, *, redis: aioredis.Redis, default_capacity: int = 60,
                 default_rate: float = 10.0) -> None:
        super().__init__(app)
        self._limiter = TokenBucketLimiter(redis, default_capacity, default_rate)
        self._excluded_paths = {"/health", "/metrics", "/docs", "/openapi.json"}

    async def dispatch(self, request: Request, call_next) -> Response:
        if request.url.path in self._excluded_paths:
            return await call_next(request)

        # Best-effort client key: real IP behind proxy
        client_key = (
            request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
            or (request.client.host if request.client else "unknown")
        )

        allowed, remaining = await self._limiter.consume(client_key)

        if not allowed:
            return JSONResponse(
                status_code=429,
                content={"detail": "rate_limit_exceeded"},
                headers={
                    "X-RateLimit-Limit": str(self._limiter.capacity),
                    "X-RateLimit-Remaining": "0",
                    "Retry-After": "1",
                },
            )

        response = await call_next(request)
        response.headers["X-RateLimit-Limit"] = str(self._limiter.capacity)
        response.headers["X-RateLimit-Remaining"] = str(remaining)
        return response
```

**Middleware vs dependency — trade-offs:**

| | Middleware | Dependency |
|--|-----------|------------|
| Applies to | All routes automatically | Only routes that declare it |
| Access to auth/user | No (auth runs inside routing) | Yes (can `Depends(get_current_user)`) |
| Per-endpoint cost | Not possible | `rate_limit(cost=5)` per route |
| Tiered limits | Requires parsing auth token manually | Natural via dependency composition |
| Excludes paths | Manual path check | Simply don't add the dependency |
| Exception handler | Must return `JSONResponse` directly | Can raise `HTTPException` |
| Performance | Slightly faster (no DI overhead) | Negligible difference in practice |

**When to use middleware:** uniform IP-based rate limiting at the perimeter — blunt instrument, runs before any routing or auth overhead. Good as a first layer of protection against unauthenticated flood attacks.

**When to use dependency:** authenticated per-user tiered limits, per-endpoint cost weighting, or any rate limit that needs to know who the user is. This is the pattern for most production APIs.

**Best practice: both layers.** Middleware handles unauthenticated IP floods cheaply. Dependencies handle per-user quota enforcement with full context.

---

### Q4: What is the sliding window counter algorithm and when is it preferable to token bucket?

**Model answer:**

**Fixed window** counts requests in discrete windows (0–60s, 60–120s, etc.). A client can make `limit` requests at 59s and another `limit` at 61s — double the limit in a 2-second span. The boundary burst is the fixed window's fundamental flaw.

**Sliding window counter** approximates a true sliding window using two fixed-window counters (current and previous) and a weighted sum:

```
rate = previous_count × (1 - elapsed_fraction) + current_count
```

Where `elapsed_fraction` is how far we are into the current window (0.0–1.0).

```python
async def sliding_window_check(
    redis: aioredis.Redis,
    key: str,
    limit: int,
    window_seconds: int,
) -> tuple[bool, int]:
    """
    Two-counter sliding window. O(1) storage, approximate but boundary-safe.
    """
    now = time.time()
    current_window = int(now // window_seconds)
    previous_window = current_window - 1
    elapsed_in_window = now % window_seconds

    pipe = redis.pipeline()
    pipe.get(f"{key}:{current_window}")
    pipe.get(f"{key}:{previous_window}")
    results = await pipe.execute()

    current_count = int(results[0] or 0)
    previous_count = int(results[1] or 0)

    # Weighted approximation: how much of the previous window still counts
    weight = 1.0 - (elapsed_in_window / window_seconds)
    estimated_rate = previous_count * weight + current_count

    if estimated_rate >= limit:
        return False, 0

    # Increment current window counter
    pipe = redis.pipeline()
    pipe.incr(f"{key}:{current_window}")
    pipe.expire(f"{key}:{current_window}", window_seconds * 2)
    await pipe.execute()

    remaining = max(0, int(limit - estimated_rate - 1))
    return True, remaining
```

**When to prefer sliding window over token bucket:**

- **Quota semantics:** "100 requests per hour" is naturally expressed as a sliding window. Token bucket expresses it as capacity=100, rate=100/3600 — accurate but less readable, and the burst behavior (full 100 tokens immediately) may not be what the product wants.
- **Simpler reasoning for customers:** "you have 1000 API calls per day" is what customers understand. Token bucket's burst concept requires explanation.
- **Billing integration:** quota counters are easy to expose in dashboards and bill against. Token bucket state (floating token count) is harder to present as a simple "you've used X of Y calls."

**When to prefer token bucket:**
- **Burst tolerance needed:** a legitimate client processing a batch may genuinely need 50 requests in 5 seconds, then nothing for a minute. Token bucket accommodates this without needing a quota window.
- **Sustained rate enforcement:** protecting a downstream service from sustained overload (not just quota enforcement). Token bucket's `rate` parameter directly caps throughput.

---

### Q5: How do you handle rate limiting correctly when FastAPI runs behind a reverse proxy?

**Model answer:**

`request.client.host` returns the IP of the last hop before Uvicorn — which is the reverse proxy (nginx, Envoy, AWS ALB), not the actual client. All users appear to come from the same IP; your rate limiter blocks everyone after the first burst.

**The `X-Forwarded-For` header** contains the real client IP:
```
X-Forwarded-For: 203.0.113.5, 10.0.0.1, 172.16.0.1
```
The leftmost IP is the original client; subsequent IPs are proxies that forwarded the request.

**Do not naively trust `X-Forwarded-For`** — clients can forge it:
```
# Attacker sets:
X-Forwarded-For: 1.2.3.4

# Your server appends the actual sender (your proxy):
X-Forwarded-For: 1.2.3.4, 10.0.0.1

# You read index 0 → attacker impersonates 1.2.3.4
```

**Correct approach:** trust only the IPs added by your known infrastructure. If you have one trusted proxy layer, the real client IP is at index `-2` (second from right):

```python
from starlette.middleware.trustedhost import TrustedHostMiddleware

TRUSTED_PROXY_COUNT = 1  # number of proxy hops you control

def get_client_ip(request: Request) -> str:
    forwarded_for = request.headers.get("X-Forwarded-For", "")
    ips = [ip.strip() for ip in forwarded_for.split(",") if ip.strip()]

    # The real client IP is TRUSTED_PROXY_COUNT hops from the right
    if len(ips) > TRUSTED_PROXY_COUNT:
        return ips[-(TRUSTED_PROXY_COUNT + 1)]

    # Fallback: direct connection or not enough hops
    return request.client.host if request.client else "unknown"
```

**Better: use `ProxyHeadersMiddleware`** (Uvicorn/Starlette built-in):

```python
# gunicorn.conf.py / startup
# Uvicorn's --proxy-headers flag enables ProxyHeadersMiddleware
# which sets request.client.host to the real client IP from X-Forwarded-For

uvicorn myapp.main:app \
    --proxy-headers \
    --forwarded-allow-ips="10.0.0.0/8,172.16.0.0/12"  # trusted proxy CIDR
```

With `--proxy-headers` and a trusted CIDR, `request.client.host` is automatically the real client IP — your rate limiter needs no special handling.

**For authenticated APIs:** prefer user ID over IP entirely. Authenticated requests carry a JWT or API key that identifies the user regardless of network topology. IP-based limiting as a fallback for unauthenticated traffic is still valid.

---

## Code: Production Rate Limiter with All Standard Headers

```python
"""
Production-grade rate limiter: token bucket per user/IP with full RFC-standard
headers and slowapi integration for decorator-based limits.
"""
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass

import redis.asyncio as aioredis
from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse

_LUA_TOKEN_BUCKET = """
local key      = KEYS[1]
local capacity = tonumber(ARGV[1])
local rate     = tonumber(ARGV[2])
local now      = tonumber(ARGV[3])
local cost     = tonumber(ARGV[4])

local data   = redis.call('HMGET', key, 'tokens', 'ts')
local tokens = tonumber(data[1]) or capacity
local ts     = tonumber(data[2]) or now

local elapsed = math.max(0, now - ts)
tokens = math.min(capacity, tokens + elapsed * rate)

local allowed = 0
if tokens >= cost then
    tokens  = tokens - cost
    allowed = 1
end

redis.call('HMSET', key, 'tokens', tokens, 'ts', now)
redis.call('PEXPIRE', key, math.ceil(capacity / rate * 1000))
return {allowed, math.floor(tokens)}
"""


@dataclass(frozen=True)
class BucketConfig:
    capacity: int    # max burst
    rate: float      # tokens/second sustained
    cost: int = 1    # tokens this request costs


class RateLimiter:
    def __init__(self, redis: aioredis.Redis) -> None:
        self._redis = redis
        self._script = redis.register_script(_LUA_TOKEN_BUCKET)

    async def consume(self, key: str, cfg: BucketConfig) -> tuple[bool, int, float]:
        allowed, remaining = await self._script(
            keys=[f"rl:{key}"],
            args=[cfg.capacity, cfg.rate, time.time(), cfg.cost],
        )
        # Time until one token refills (seconds)
        retry_after = cfg.cost / cfg.rate if not bool(allowed) else 0.0
        return bool(allowed), int(remaining), retry_after

    def dependency(self, capacity: int, rate: float, cost: int = 1):
        """Returns a FastAPI dependency for the given bucket config."""
        cfg = BucketConfig(capacity=capacity, rate=rate, cost=cost)

        async def _dep(request: Request) -> None:
            # Use authenticated user ID if available, else real client IP
            user = getattr(request.state, "user", None)
            key = f"user:{user.id}" if user else f"ip:{_real_ip(request)}"

            allowed, remaining, retry_after = await self.consume(key, cfg)

            reset_at = int(time.time() + cfg.capacity / cfg.rate)

            # Always attach headers — clients use these to implement backoff
            request.state.rl_headers = {
                "X-RateLimit-Limit": str(cfg.capacity),
                "X-RateLimit-Remaining": str(remaining),
                "X-RateLimit-Reset": str(reset_at),
                "X-RateLimit-Policy": f"{cfg.capacity};w={int(cfg.capacity / cfg.rate)}",
            }

            if not allowed:
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail={
                        "error": "rate_limit_exceeded",
                        "retry_after_seconds": round(retry_after, 2),
                    },
                    headers={
                        **request.state.rl_headers,
                        "Retry-After": str(int(retry_after) + 1),
                    },
                )

        return Depends(_dep)


def _real_ip(request: Request) -> str:
    """Extract real client IP, respecting a single trusted proxy layer."""
    xff = request.headers.get("X-Forwarded-For", "")
    ips = [ip.strip() for ip in xff.split(",") if ip.strip()]
    if len(ips) >= 2:
        return ips[-2]  # one trusted proxy: real IP is second from right
    if ips:
        return ips[0]
    return request.client.host if request.client else "unknown"


# ── Middleware: propagate RL headers to all responses ──────────────────────

class RateLimitHeaderMiddleware:
    """Copies rate limit headers set by the dependency onto the response."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope)
        rl_headers: dict = {}

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                rl_headers.update(getattr(request.state, "rl_headers", {}))
                headers = dict(message.get("headers", []))
                for k, v in rl_headers.items():
                    headers[k.lower().encode()] = v.encode()
                message = {**message, "headers": list(headers.items())}
            await send(message)

        await self.app(scope, receive, send_with_headers)


# ── App wiring ─────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    redis = await aioredis.from_url("redis://localhost:6379", decode_responses=False)
    app.state.limiter = RateLimiter(redis)
    yield
    await redis.aclose()


app = FastAPI(lifespan=lifespan)
app.add_middleware(RateLimitHeaderMiddleware)


def get_limiter(request: Request) -> RateLimiter:
    return request.app.state.limiter


# ── Endpoints ──────────────────────────────────────────────────────────────

@app.get(
    "/search",
    dependencies=[Depends(lambda req=Depends(lambda r: r): get_limiter(req).dependency(
        capacity=60, rate=10.0
    )())],
)
async def search(q: str) -> dict:
    return {"results": []}


# Cleaner: pre-built dependency instances
def make_limits(limiter: RateLimiter):
    return {
        "standard": limiter.dependency(capacity=60, rate=10.0),
        "expensive": limiter.dependency(capacity=10, rate=1.0, cost=5),
        "strict":   limiter.dependency(capacity=5,  rate=0.5),
    }

# In practice, access via app.state after lifespan:
# limits = make_limits(app.state.limiter)
# @app.get("/items", dependencies=[limits["standard"]])
```

```python
# Testing rate limits
import pytest
from unittest.mock import AsyncMock, patch

@pytest.mark.asyncio
async def test_rate_limit_allows_within_quota(client):
    """First N requests within capacity should succeed."""
    responses = [await client.get("/search?q=test") for _ in range(5)]
    assert all(r.status_code == 200 for r in responses)
    assert all("X-RateLimit-Remaining" in r.headers for r in responses)
    # Remaining should decrease
    remainders = [int(r.headers["X-RateLimit-Remaining"]) for r in responses]
    assert remainders == sorted(remainders, reverse=True)


@pytest.mark.asyncio
async def test_rate_limit_blocks_over_quota(client):
    """Exhaust the bucket, next request should 429."""
    limiter: RateLimiter = client.app.state.limiter
    # Directly exhaust the bucket via Redis
    await limiter._redis.hset("rl:ip:testclient", mapping={"tokens": "0", "ts": str(time.time())})

    response = await client.get("/search?q=test")
    assert response.status_code == 429
    assert "Retry-After" in response.headers
    assert int(response.headers["Retry-After"]) >= 1
    body = response.json()
    assert body["detail"]["error"] == "rate_limit_exceeded"


@pytest.mark.asyncio
async def test_rate_limit_headers_always_present(client):
    response = await client.get("/search?q=test")
    for header in ("X-RateLimit-Limit", "X-RateLimit-Remaining", "X-RateLimit-Reset"):
        assert header in response.headers, f"Missing {header}"
```

---

## Under the Hood

**Redis single-threaded execution model:** Redis processes commands sequentially in a single thread. A Lua script loaded via `EVALSHA` (what `register_script()` uses after the first `EVAL`) is treated as a single atomic command — no other client command executes between the `HMGET` and `HMSET` inside the script. This is the foundational guarantee that makes the Lua approach work without `WATCH`/`MULTI`/`EXEC` transactions.

**`register_script()` vs `eval()`:** `redis.register_script(lua_code)` computes a SHA1 of the script and calls `SCRIPT LOAD` on first invocation, then uses `EVALSHA` for subsequent calls. This saves sending the full script body on every request — just the 40-character SHA1. If the script isn't in Redis's script cache (after a server restart), redis-py automatically falls back to `EVAL` with the full body. The fallback is transparent to callers.

**`X-RateLimit-Policy` header (IETF draft):** the `Policy` header format `{quota};w={window}` is from the [IETF Rate Limit Headers draft](https://datatracker.ietf.org/doc/html/draft-ietf-httpapi-ratelimit-headers). `Retry-After` is RFC 7231. `X-RateLimit-Limit/Remaining/Reset` are de facto standards (GitHub, Stripe, Twitter use them). The `Reset` value is a Unix timestamp (seconds), not a countdown — clients compute `reset - now` to get seconds until reset.

**`slowapi`:** a third-party library that ports Flask-Limiter to FastAPI/Starlette. It uses decorator syntax (`@limiter.limit("5/minute")`) and supports Redis, Memcached, and in-memory backends via the `limits` library. Under the hood, `slowapi` implements moving window and fixed window counters using Redis sorted sets (`ZADD`/`ZREMRANGEBYSCORE`/`ZCARD` pipeline) rather than Lua — which uses multiple round-trips and relies on pipeline atomicity rather than Lua atomicity. For simple use cases, `slowapi` is a fast start; for tiered limits and custom key strategies, the manual approach above gives full control.
