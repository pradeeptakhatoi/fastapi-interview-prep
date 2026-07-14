"""
Idempotency key dependency for POST endpoints.
Prevents duplicate execution on client retries.
Uses Redis with Lua script for atomic check-and-reserve.
"""

import hashlib
import json
from typing import Any

import redis.asyncio as aioredis
from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse

_RESERVE_SCRIPT = """
local key = KEYS[1]
local lock_ttl = tonumber(ARGV[1])

local existing = redis.call('GET', key)
if existing then
    return existing
end

redis.call('SET', key, '"__in_progress__"', 'EX', lock_ttl)
return nil
"""

_COMPLETE_SCRIPT = """
local key = KEYS[1]
local payload = ARGV[1]
local ttl = tonumber(ARGV[2])
redis.call('SET', key, payload, 'EX', ttl)
return 1
"""


class _CacheHit(Exception):
    def __init__(self, data: Any, status_code: int) -> None:
        self.data = data
        self.status_code = status_code


class IdempotencyKey:
    """
    Usage:
        @app.post("/orders")
        async def create_order(
            body: OrderIn,
            idempotency: IdempotencyContext = Depends(IdempotencyKey()),
        ):
            result = await process_order(body)
            await idempotency.commit(result, status_code=201)
            return result
    """

    def __init__(self, ttl: int = 86400, lock_ttl: int = 30) -> None:
        self.ttl = ttl
        self.lock_ttl = lock_ttl

    async def __call__(self, request: Request) -> "IdempotencyContext":
        key_header = request.headers.get("Idempotency-Key")
        if not key_header:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Idempotency-Key header required",
            )

        body = await request.body()
        fingerprint = hashlib.sha256(
            f"{key_header}:{request.url.path}:{body.hex()}".encode()
        ).hexdigest()
        redis_key = f"idempotency:{fingerprint}"

        redis: aioredis.Redis = request.app.state.redis
        reserve = redis.register_script(_RESERVE_SCRIPT)

        raw = await reserve(keys=[redis_key], args=[self.lock_ttl])

        if raw == b'"__in_progress__"':
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Duplicate request already in progress",
            )

        if raw is not None:
            cached = json.loads(raw)
            raise _CacheHit(data=cached["data"], status_code=cached["status"])

        return IdempotencyContext(
            redis=redis,
            key=redis_key,
            ttl=self.ttl,
        )


class IdempotencyContext:
    def __init__(self, redis: aioredis.Redis, key: str, ttl: int) -> None:
        self._redis = redis
        self._key = key
        self._ttl = ttl
        self._complete = redis.register_script(_COMPLETE_SCRIPT)

    async def commit(self, data: Any, status_code: int = 200) -> None:
        payload = json.dumps({"data": data, "status": status_code})
        await self._complete(keys=[self._key], args=[payload, self._ttl])


# --- App wiring ---

from contextlib import asynccontextmanager
from pydantic import BaseModel


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.redis = await aioredis.from_url("redis://localhost:6379")
    yield
    await app.state.redis.aclose()


app = FastAPI(lifespan=lifespan)


@app.exception_handler(_CacheHit)
async def cache_hit_handler(request: Request, exc: _CacheHit) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content=exc.data,
        headers={"X-Idempotency-Replayed": "true"},
    )


class OrderIn(BaseModel):
    product_id: int
    quantity: int


@app.post("/orders", status_code=201)
async def create_order(
    body: OrderIn,
    idempotency: IdempotencyContext = Depends(IdempotencyKey()),
) -> dict:
    # Expensive operation — runs at most once per Idempotency-Key
    result = {"order_id": "ord_123", "product_id": body.product_id}

    await idempotency.commit(result, status_code=201)
    return result
