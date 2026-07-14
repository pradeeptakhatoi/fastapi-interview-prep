# Server-Sent Events (SSE) — Deep Implementation

## Concept

Server-Sent Events (SSE) is a browser standard (EventSource API) built on plain HTTP. The server holds the connection open and pushes newline-delimited text events. The client auto-reconnects on disconnect.

**Why SSE over WebSocket for many use cases:**
- Standard HTTP — works through proxies, CDNs, load balancers without special config
- Auto-reconnect with `Last-Event-ID` header (browser resumes from last seen event)
- Browser `EventSource` is simple to use; no client-side library needed
- Firewalls that block WebSocket upgrades don't block persistent HTTP
- HTTP/2 multiplexing means many SSE streams share one connection

**The SSE wire format** (text, `Content-Type: text/event-stream`):

```
id: 42\n
event: order-update\n
data: {"order_id": "ord_123", "status": "shipped"}\n
retry: 3000\n
\n
```

Rules:
- Each line is `field: value\n`
- An event is terminated by a blank line (`\n\n`)
- `data:` can span multiple lines (each line appended with `\n` by the browser)
- `id:` sets `lastEventId` — sent as `Last-Event-ID` header on reconnect
- `event:` names the event type (browser `EventSource.addEventListener('order-update', ...)`)
- `retry:` tells the browser how long to wait before reconnecting (ms)
- Lines starting with `:` are comments (used as heartbeat keepalives: `: ping\n\n`)

**FastAPI implementation:** `StreamingResponse` with `media_type="text/event-stream"` and an async generator body.

---

## Interview Questions

### Q1: How does `Last-Event-ID` enable resumable SSE streams, and how do you implement it server-side?

**Model answer:**

When a browser `EventSource` reconnects after a disconnect, it sends the `Last-Event-ID` header with the `id:` value of the last event it received. The server uses this to resume the stream from that point — avoiding message loss during transient disconnections.

```python
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse

app = FastAPI()

# Simulated event store — in production: Redis Stream, DB, Kafka
EVENTS: list[dict] = [
    {"id": 1, "type": "update", "data": {"msg": "first"}},
    {"id": 2, "type": "update", "data": {"msg": "second"}},
    {"id": 3, "type": "update", "data": {"msg": "third"}},
]


def format_sse(
    data: str,
    event: str | None = None,
    id: int | None = None,
    retry: int | None = None,
) -> str:
    lines = []
    if id is not None:
        lines.append(f"id: {id}")
    if event:
        lines.append(f"event: {event}")
    if retry is not None:
        lines.append(f"retry: {retry}")
    for line in data.splitlines():
        lines.append(f"data: {line}")
    lines.append("")   # blank line = end of event
    return "\n".join(lines) + "\n"


async def event_stream(request: Request, last_event_id: int | None):
    import asyncio, json

    # Replay missed events if client is resuming
    if last_event_id is not None:
        for event in EVENTS:
            if event["id"] > last_event_id:
                yield format_sse(
                    data=json.dumps(event["data"]),
                    event=event["type"],
                    id=event["id"],
                ).encode()

    # Stream new events as they arrive
    current_id = max((e["id"] for e in EVENTS), default=0)
    while True:
        if await request.is_disconnected():
            break
        # Send heartbeat comment to keep connection alive through proxies
        yield b": heartbeat\n\n"
        await asyncio.sleep(15)


@app.get("/events")
async def sse(request: Request) -> StreamingResponse:
    last_event_id_header = request.headers.get("Last-Event-ID")
    last_event_id = int(last_event_id_header) if last_event_id_header else None

    return StreamingResponse(
        event_stream(request, last_event_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
```

**Why `X-Accel-Buffering: no`:** nginx buffers responses by default. Without this header, nginx holds SSE chunks until its buffer fills — clients see no events until several accumulate. This header tells nginx to forward each chunk immediately.

**Gotcha follow-up:** The browser sends `Last-Event-ID` only for reconnects, not the initial connection. If the initial connection never set an event ID (`id:` field), the browser won't send `Last-Event-ID` on reconnect either. Always set event IDs on every event if you want resumable streams.

---

### Q2: How do you fan out SSE events to multiple connected clients across multiple Gunicorn workers?

**Model answer:**

Same architectural problem as WebSocket fan-out: each worker process has its own set of connected clients. The solution is a shared pub/sub layer — Redis is the standard choice.

**Pattern: Redis Pub/Sub per channel + per-worker subscriber task**

```python
import asyncio
import json
from contextlib import asynccontextmanager
from typing import AsyncGenerator

import redis.asyncio as aioredis
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse

# Per-worker in-memory registry of active SSE connections
# key: channel name, value: set of asyncio.Queue for each connected client
_subscriptions: dict[str, set[asyncio.Queue]] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    redis = await aioredis.from_url("redis://localhost:6379")
    app.state.redis = redis

    # Background task: subscribe to all channels, fan out to local clients
    async def redis_to_local():
        pubsub = redis.pubsub()
        await pubsub.psubscribe("sse:*")  # subscribe to all sse:* channels
        async for message in pubsub.listen():
            if message["type"] != "pmessage":
                continue
            channel: str = message["channel"].decode()    # "sse:orders"
            data: str = message["data"].decode()
            queues = _subscriptions.get(channel, set())
            dead = set()
            for q in queues:
                try:
                    q.put_nowait(data)
                except asyncio.QueueFull:
                    dead.add(q)  # client is too slow — drop it
            _subscriptions[channel] -= dead

    task = asyncio.create_task(redis_to_local())
    yield
    task.cancel()
    await redis.aclose()


app = FastAPI(lifespan=lifespan)


async def sse_generator(
    request: Request, channel: str
) -> AsyncGenerator[bytes, None]:
    queue: asyncio.Queue[str] = asyncio.Queue(maxsize=100)

    # Register this client
    _subscriptions.setdefault(channel, set()).add(queue)
    yield b": connected\n\n"

    try:
        while True:
            if await request.is_disconnected():
                break
            try:
                # Wait for a message, with timeout for heartbeats
                data = await asyncio.wait_for(queue.get(), timeout=20.0)
                payload = json.loads(data)
                event_id = payload.get("id", "")
                event_type = payload.get("type", "message")
                event_data = json.dumps(payload.get("data", {}))
                yield (
                    f"id: {event_id}\nevent: {event_type}\ndata: {event_data}\n\n"
                ).encode()
            except asyncio.TimeoutError:
                # Send heartbeat — keeps connection alive through proxies
                yield b": heartbeat\n\n"
    finally:
        # Always clean up — disconnect, exception, or server shutdown
        _subscriptions.get(channel, set()).discard(queue)


@app.get("/stream/{channel}")
async def stream(channel: str, request: Request) -> StreamingResponse:
    return StreamingResponse(
        sse_generator(request, f"sse:{channel}"),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/publish/{channel}")
async def publish(channel: str, data: dict, request: Request) -> dict:
    redis: aioredis.Redis = request.app.state.redis
    await redis.publish(f"sse:{channel}", json.dumps(data))
    return {"published": True}
```

**Why `asyncio.Queue` per client (not a shared list):** each client has its own buffer. A slow client that can't keep up has its own queue filling up — `QueueFull` kicks it out without affecting other clients. A shared list would block all clients while waiting for the slowest one.

---

### Q3: SSE vs WebSocket — when is each the correct choice?

**Model answer:**

| Dimension | SSE | WebSocket |
|-----------|-----|-----------|
| Direction | Server → Client only | Bidirectional |
| Protocol | HTTP/1.1 or HTTP/2 | Separate WS protocol (ws://) |
| Browser API | `EventSource` (built-in, simple) | `WebSocket` (built-in, more complex) |
| Proxy/CDN support | Works with standard HTTP proxies | Requires WebSocket-aware proxies |
| Auto-reconnect | Built into browser | Must implement manually |
| Message ordering | Guaranteed (TCP) | Guaranteed (TCP) |
| Per-connection overhead | 1 HTTP connection | 1 WS connection |
| HTTP/2 multiplexing | Multiple SSE streams on 1 TCP conn | One WS conn per stream |
| Binary data | No (text only) | Yes |
| Max connections per origin | Browser limit (6 per origin for HTTP/1.1, unlimited for HTTP/2) | No spec limit |

**Use SSE when:**
- Updates flow server → client (notifications, live feeds, dashboards, progress tracking)
- The client needs to display data but not send it back
- You want browser auto-reconnect with `Last-Event-ID` resumability
- Your infrastructure (CDN, load balancer) doesn't support WebSocket upgrades
- HTTP/2 is available — multiplexing means zero overhead for many SSE channels

**Use WebSocket when:**
- Bidirectional real-time communication (chat, collaborative editing, multiplayer gaming)
- Binary protocol needed (audio/video streaming, binary sensor data)
- High-frequency client→server messages (mouse tracking, live typing indicators)
- Sub-millisecond latency is required (financial trading, gaming)

**Common mistake:** using WebSocket for server-push-only scenarios (notification systems, progress bars, live dashboards) when SSE is simpler, more reliable, and works through more infrastructure configurations.

---

### Q4: How do you handle the 6-connection-per-origin browser limit for HTTP/1.1 SSE?

**Model answer:**

HTTP/1.1 browsers limit connections to 6 per origin (scheme + host + port). Each SSE `EventSource` holds one connection open. With HTTP/1.1, a tab using 3 SSE streams consumes half the connection budget — opening a 4th stream can stall other requests.

**Solutions:**

**1. HTTP/2 (best solution):** multiplexing allows unlimited concurrent streams over one TCP connection. All SSE streams share the same connection with no browser limit. Deploy TLS + HTTP/2 (`uvicorn --ssl-*` or via nginx/Envoy with H2).

**2. Single multiplexed SSE channel:** rather than one SSE stream per data type, use one stream that sends typed events:

```python
# Single endpoint, multiple event types
@app.get("/stream")
async def unified_stream(request: Request) -> StreamingResponse:
    async def generator():
        async for event in get_all_events():
            yield f"event: {event.type}\ndata: {event.json()}\n\n".encode()
    return StreamingResponse(generator(), media_type="text/event-stream")
```

Client filters by event type:
```javascript
const es = new EventSource('/stream');
es.addEventListener('order-update', e => handleOrder(JSON.parse(e.data)));
es.addEventListener('notification', e => handleNotification(JSON.parse(e.data)));
```

**3. EventSource polyfill with HTTP/2:** libraries like `@microsoft/fetch-event-source` use `fetch()` instead of `EventSource`, which goes through the HTTP/2 multiplexed connection rather than consuming a separate connection slot.

---

## Code: Production SSE with Typed Events, Reconnect, and Backpressure

```python
import asyncio
import json
import time
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from typing import Any

import redis.asyncio as aioredis
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse


@dataclass
class SSEEvent:
    data: Any
    event: str = "message"
    id: int | None = None
    retry_ms: int | None = None

    def encode(self) -> bytes:
        parts = []
        if self.id is not None:
            parts.append(f"id: {self.id}")
        parts.append(f"event: {self.event}")
        data_str = json.dumps(self.data) if not isinstance(self.data, str) else self.data
        for line in data_str.splitlines():
            parts.append(f"data: {line}")
        if self.retry_ms is not None:
            parts.append(f"retry: {self.retry_ms}")
        parts.append("")
        return ("\n".join(parts) + "\n").encode()


HEARTBEAT = b": heartbeat\n\n"


class SSEChannel:
    """Per-client SSE queue with backpressure and disconnect detection."""

    def __init__(self, maxsize: int = 50) -> None:
        self._queue: asyncio.Queue[SSEEvent | None] = asyncio.Queue(maxsize=maxsize)

    def push(self, event: SSEEvent) -> bool:
        """Returns False if queue is full (client too slow)."""
        try:
            self._queue.put_nowait(event)
            return True
        except asyncio.QueueFull:
            return False

    def close(self) -> None:
        self._queue.put_nowait(None)  # sentinel

    async def __aiter__(self) -> AsyncGenerator[SSEEvent, None]:
        while True:
            event = await self._queue.get()
            if event is None:
                return
            yield event


# Global registry: topic → set of channels (per worker)
_registry: dict[str, set[SSEChannel]] = {}


async def sse_response(
    request: Request,
    topic: str,
    last_event_id: int | None = None,
) -> AsyncGenerator[bytes, None]:
    channel = SSEChannel(maxsize=50)
    _registry.setdefault(topic, set()).add(channel)

    # Tell browser how long to wait before reconnecting
    yield SSEEvent(data="", event="connected", retry_ms=3000).encode()

    try:
        while True:
            if await request.is_disconnected():
                break

            try:
                event = await asyncio.wait_for(
                    channel._queue.get(), timeout=25.0
                )
            except asyncio.TimeoutError:
                yield HEARTBEAT
                continue

            if event is None:
                break
            yield event.encode()
    finally:
        _registry.get(topic, set()).discard(channel)


def broadcast(topic: str, event: SSEEvent) -> int:
    """Broadcast to all local clients on this topic. Returns # delivered."""
    channels = _registry.get(topic, set())
    delivered = sum(ch.push(event) for ch in channels)
    return delivered


# --- App ---

app = FastAPI()
_event_counter = 0


@app.get("/stream/{topic}")
async def stream(topic: str, request: Request) -> StreamingResponse:
    last_id_header = request.headers.get("Last-Event-ID")
    last_event_id = int(last_id_header) if last_id_header else None

    return StreamingResponse(
        sse_response(request, topic, last_event_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",       # nginx: don't buffer
            "Access-Control-Allow-Origin": "*",
        },
    )


@app.post("/publish/{topic}")
async def publish(topic: str, payload: dict) -> dict:
    global _event_counter
    _event_counter += 1
    event = SSEEvent(
        data=payload,
        event=payload.get("type", "message"),
        id=_event_counter,
    )
    count = broadcast(topic, event)
    return {"delivered_local": count, "event_id": _event_counter}
```

---

## Under the Hood

**`StreamingResponse` and SSE:** `StreamingResponse.__call__()` sends `http.response.start` with the `text/event-stream` content type and then iterates the async generator, calling `send({"type": "http.response.body", "body": chunk, "more_body": True})` for each chunk. The ASGI server (Uvicorn) writes each chunk to the TCP socket immediately — no buffering at the Uvicorn layer.

**`await request.is_disconnected()`** calls `await receive()` with a no-wait check. It returns `True` if the ASGI server has detected a TCP close (the client sent a FIN packet or the socket errored). Under Uvicorn with HTTP/1.1, this is detected via the underlying `asyncio` transport's `connection_lost()` callback. Under HTTP/2, it's detected via stream reset frames. Note: TCP half-close detection latency varies — on unreliable networks, a "disconnected" client may not be detected for 30–90 seconds (until TCP keepalive fires).

**`asyncio.wait_for` with timeout** in the generator is the correct pattern for SSE heartbeats. Without it, if no events arrive, `queue.get()` suspends indefinitely and the generator never yields a heartbeat — proxies with idle connection timeouts (nginx default: 60s) will close the connection. The 25-second heartbeat (`b": heartbeat\n\n"` — a comment line) keeps the connection alive through all standard proxy timeouts.
