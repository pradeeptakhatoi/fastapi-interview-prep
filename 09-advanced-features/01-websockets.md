# WebSocket Support, Connection Lifecycle, Scaling WebSockets Across Workers

## Concept

FastAPI's WebSocket support is a thin wrapper over Starlette's `WebSocket` class. A WebSocket route receives a `WebSocket` object (not a `Request`) and must explicitly accept the connection:

```python
@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    # connection open
    try:
        while True:
            data = await ws.receive_text()
            await ws.send_text(f"echo: {data}")
    except WebSocketDisconnect:
        pass  # client closed the connection
```

**WebSocket ASGI event flow:**
- `receive()` yields: `{"type": "websocket.connect"}`, then `{"type": "websocket.receive", "text": ..., "bytes": ...}` or `{"type": "websocket.disconnect"}`
- `send()` accepts: `{"type": "websocket.accept"}`, `{"type": "websocket.send", "text": ..., "bytes": ...}`, `{"type": "websocket.close"}`

**The scaling problem:** WebSocket connections are long-lived and stateful. With Gunicorn + 4 workers, a client connected to worker 1 cannot directly receive messages from worker 2. For broadcasting to all connected clients, you need a shared pub/sub layer.

---

## Interview Questions

### Q1: How do you broadcast a message to all connected WebSocket clients in a multi-worker FastAPI deployment?

**Model answer:**

The core problem: each worker process has its own set of in-memory WebSocket connections. A message needs to reach connections on all workers.

**Solution: Redis Pub/Sub as the inter-worker message bus.**

```python
import asyncio
from contextlib import asynccontextmanager
import redis.asyncio as aioredis
from fastapi import FastAPI, WebSocket, WebSocketDisconnect

# Per-process connection registry
connected_clients: set[WebSocket] = set()


@asynccontextmanager
async def lifespan(app: FastAPI):
    redis = await aioredis.from_url("redis://localhost:6379")
    app.state.redis = redis

    # Start background task: subscribe to Redis and forward to local WebSockets
    pubsub = redis.pubsub()
    await pubsub.subscribe("broadcast")
    app.state.pubsub = pubsub

    async def redis_listener():
        async for message in pubsub.listen():
            if message["type"] == "message":
                text = message["data"]
                # Broadcast to all clients connected to THIS worker
                dead = set()
                for ws in connected_clients:
                    try:
                        await ws.send_text(text)
                    except Exception:
                        dead.add(ws)
                connected_clients -= dead

    task = asyncio.create_task(redis_listener())
    yield
    task.cancel()
    await pubsub.unsubscribe("broadcast")
    await redis.aclose()


app = FastAPI(lifespan=lifespan)


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    connected_clients.add(ws)
    try:
        while True:
            await ws.receive_text()  # keep connection open; ignore client messages
    except WebSocketDisconnect:
        connected_clients.discard(ws)


@app.post("/broadcast")
async def broadcast(message: str, request: Request):
    # Publish to Redis → all workers receive via their subscriber tasks
    await request.app.state.redis.publish("broadcast", message)
    return {"published": True}
```

**The architecture:** a background asyncio task per worker subscribes to the Redis channel. When a message is published, all workers receive it and forward it to their locally connected WebSockets.

**Gotcha follow-up:** What happens if the Redis listener task crashes?

The broadcast stops for that worker — connected clients on that worker don't receive messages, but also don't disconnect. You need to monitor the task and restart it:

```python
async def resilient_listener():
    while True:
        try:
            await redis_listener()
        except Exception:
            logger.exception("Redis listener crashed, restarting in 1s")
            await asyncio.sleep(1)
```

---

### Q2: How do you authenticate WebSocket connections in FastAPI?

**Model answer:**

WebSocket connections don't use HTTP headers the same way REST endpoints do — browsers don't send the `Authorization` header in WebSocket upgrade requests (it's not part of the WebSocket protocol spec). Options:

**1. Token in query parameter (simplest, but token in URL logs):**
```python
@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket, token: str = Query(...)):
    user = validate_jwt(token)  # your JWT validation
    await ws.accept()
```

**2. Token sent as first message after connection:**
```python
@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    auth_data = await ws.receive_json()
    user = validate_jwt(auth_data["token"])
    if not user:
        await ws.close(code=4001)  # 4000-4999 are application-defined codes
        return
    # Connection authenticated
```

**3. Subprotocol header (supported by browsers):**
The `Sec-WebSocket-Protocol` header can carry auth tokens. The server accepts the connection with a matching subprotocol:
```python
@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    # ws.headers contains "sec-websocket-protocol"
    token = ws.headers.get("sec-websocket-protocol", "").split(",")[0].strip()
    user = validate_jwt(token)
    await ws.accept(subprotocol=token)  # echo back the subprotocol to complete handshake
```

**4. Cookie-based auth (works if same-origin):**
Cookies are sent with WebSocket upgrade requests if the WebSocket URL is same-origin. Read from `ws.cookies`:
```python
token = ws.cookies.get("access_token")
```

In production: option 2 (first-message auth) is most explicit and flexible. Option 1 is simplest but tokens appear in server access logs.

---

### Q3: How do you handle WebSocket connection cleanup when a client disconnects abruptly (network drop)?

**Model answer:**

TCP doesn't immediately detect connection drops at the application layer. Without WebSocket pings, a dropped connection can appear open for minutes. Solutions:

**1. Server-side ping with timeout:**
```python
@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    
    async def ping_loop():
        while True:
            await asyncio.sleep(30)
            await ws.send_bytes(b"ping")  # or ws.send_text("ping")
    
    ping_task = asyncio.create_task(ping_loop())
    try:
        while True:
            data = await asyncio.wait_for(ws.receive_text(), timeout=90)
            await ws.send_text(f"pong: {data}")
    except (WebSocketDisconnect, asyncio.TimeoutError):
        pass
    finally:
        ping_task.cancel()
        connected_clients.discard(ws)
```

**2. Use WebSocket-level ping/pong frames (automatic in Starlette):**
Starlette's WebSocket implementation does NOT automatically send WebSocket protocol ping frames. You must implement them manually (as above) or configure the ASGI server.

**Uvicorn's `--ws-ping-interval` and `--ws-ping-timeout`:**
```bash
uvicorn myapp.main:app --ws-ping-interval 20 --ws-ping-timeout 20
```
This sends WebSocket protocol-level ping frames every 20 seconds and closes the connection if no pong is received within 20 seconds.

**3. Detect disconnect via `WebSocketDisconnect`:**
```python
try:
    data = await ws.receive_text()
except WebSocketDisconnect as e:
    # e.code: 1000 (normal), 1001 (going away), 1006 (abnormal closure/network drop)
    connected_clients.discard(ws)
```

---

## Under the Hood

Starlette's `WebSocket` class is a wrapper around the ASGI `scope`, `receive`, and `send` callables for a `websocket` scope. `await ws.receive_text()` calls `await receive()` in a loop until it gets a `websocket.receive` event (not `websocket.connect`). `await ws.send_text(msg)` calls `await send({"type": "websocket.send", "text": msg})`.

`WebSocketDisconnect` is raised when `receive()` returns a `{"type": "websocket.disconnect"}` event. This is the normal signal for both graceful client disconnect and abnormal network closure (the ASGI server detects the TCP close and sends the disconnect event).

WebSocket connections block one event loop coroutine for their duration. With 1000 connected WebSockets on one Uvicorn worker, there are 1000 suspended coroutines — each consuming ~1KB of memory for the coroutine frame. This is manageable; the limit is typically OS file descriptors (default 1024 per process, tunable via `ulimit -n`).
