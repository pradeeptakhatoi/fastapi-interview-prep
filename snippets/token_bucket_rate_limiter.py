"""
Token bucket rate limiter using Redis Lua script for atomicity.
Per-client limiting keyed by IP (swap for user ID in authenticated APIs).
"""

import time
from contextlib import asynccontextmanager

import redis.asyncio as aioredis
from fastapi import Depends, FastAPI, HTTPException, Request, status

# Atomic token bucket: check and decrement in one Redis round-trip
_LUA_SCRIPT = """
local key = KEYS[1]
local capacity = tonumber(ARGV[1])
local refill_rate = tonumber(ARGV[2])
local now = tonumber(ARGV[3])
local cost = tonumber(ARGV[4])

local bucket = redis.call('HMGET', key, 'tokens', 'ts')
local tokens = tonumber(bucket[1]) or capacity
local ts = tonumber(bucket[2]) or now

local elapsed = math.max(0, now - ts)
tokens = math.min(capacity, tokens + elapsed * refill_rate)

if tokens >= cost then
    tokens = tokens - cost
    redis.call('HMSET', key, 'tokens', tokens, 'ts', now)
    redis.call('PEXPIRE', key, math.ceil(capacity / refill_rate * 1000))
    return {1, math.floor(tokens)}
else
    redis.call('HMSET', key, 'tokens', tokens, 'ts', now)
    redis.call('PEXPIRE', key, math.ceil(capacity / refill_rate * 1000))
    return {0, math.floor(tokens)}
end
"""


class TokenBucketLimiter:
    def __init__(
        self,
        redis: aioredis.Redis,
        capacity: int,
        refill_rate: float,
        prefix: str = "rl",
    ) -> None:
        self.redis = redis
        self.capacity = capacity
        self.refill_rate = refill_rate
        self.prefix = prefix
        self._script = redis.register_script(_LUA_SCRIPT)

    async def consume(self, key: str, cost: int = 1) -> tuple[bool, int]:
        allowed, remaining = await self._script(
            keys=[f"{self.prefix}:{key}"],
            args=[self.capacity, self.refill_rate, time.time(), cost],
        )
        return bool(allowed), int(remaining)


@asynccontextmanager
async def lifespan(app: FastAPI):
    redis = await aioredis.from_url("redis://localhost:6379")
    app.state.limiter = TokenBucketLimiter(
        redis,
        capacity=60,       # burst of 60 requests
        refill_rate=10.0,  # 10 requests/second sustained
    )
    yield
    await redis.aclose()


app = FastAPI(lifespan=lifespan)


def rate_limit(cost: int = 1):
    async def _check(request: Request) -> None:
        limiter: TokenBucketLimiter = request.app.state.limiter
        client_key = request.client.host if request.client else "global"
        allowed, remaining = await limiter.consume(client_key, cost)

        if not allowed:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Rate limit exceeded",
                headers={
                    "Retry-After": "1",
                    "X-RateLimit-Remaining": "0",
                    "X-RateLimit-Limit": str(limiter.capacity),
                },
            )
    return _check


# Standard endpoint: costs 1 token
@app.get("/items/", dependencies=[Depends(rate_limit(cost=1))])
async def list_items() -> list:
    return []


# Expensive endpoint: costs 5 tokens (bulk operation)
@app.post("/items/bulk", dependencies=[Depends(rate_limit(cost=5))])
async def bulk_create() -> dict:
    return {"created": 0}
