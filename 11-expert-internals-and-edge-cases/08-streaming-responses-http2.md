# Request Body Streaming, Custom Responses, HTTP/2

## Concept

**Request body streaming:** by default, FastAPI reads the entire request body before your handler runs (for Pydantic body parsing). For large uploads or payloads, you can stream via `request.stream()`, which yields chunks as they arrive without buffering.

**Custom Response classes:** FastAPI returns `JSONResponse` by default. Alternatives:
- `StreamingResponse(content=async_generator, media_type="...")` — for large files, SSE, chunked data
- `FileResponse(path)` — serves a file, handles ETags, Range headers
- `HTMLResponse`, `PlainTextResponse` — simple wrappers
- Custom `Response` subclass — full control over serialization and headers

**HTTP/2 in FastAPI:** FastAPI itself is HTTP version-agnostic. HTTP/2 support depends entirely on the ASGI server:
- Uvicorn: HTTP/2 requires `uvicorn[standard]` (installs `h2` library) + TLS (HTTP/2 requires HTTPS in browsers, though HTTP/2 Cleartext `h2c` exists)
- Hypercorn: HTTP/2 and HTTP/3 (QUIC) support built-in
- Granian: HTTP/1.1 and HTTP/2

---

## Interview Questions

### Q1: How do you handle a large file upload without loading the entire body into memory?

**Model answer:**

Use `request.stream()` — an async generator that yields body chunks:

```python
from fastapi import FastAPI, Request
from pathlib import Path
import aiofiles

app = FastAPI()

@app.post("/upload")
async def upload_large_file(request: Request, filename: str) -> dict:
    # Validate content-length header before accepting
    content_length = request.headers.get("content-length")
    if content_length and int(content_length) > 100 * 1024 * 1024:  # 100MB
        raise HTTPException(status_code=413, detail="File too large")
    
    bytes_written = 0
    output_path = Path("/tmp") / filename
    
    async with aiofiles.open(output_path, "wb") as f:
        async for chunk in request.stream():
            await f.write(chunk)
            bytes_written += len(chunk)
            # Stream-based size limit (in case Content-Length was absent/spoofed)
            if bytes_written > 100 * 1024 * 1024:
                output_path.unlink(missing_ok=True)
                raise HTTPException(status_code=413, detail="File too large")
    
    return {"filename": filename, "bytes": bytes_written}
```

**Critical constraint:** once you call `await request.body()` (which buffers everything) you cannot use `request.stream()` — the stream has already been consumed. Similarly, if FastAPI already parsed the body (e.g., you have a Pydantic body parameter), `request.stream()` yields nothing. Choose one approach per endpoint.

**`UploadFile` vs manual streaming:** `UploadFile` (declared as a route parameter) buffers files up to 1MB in memory, then spills to disk (via `SpooledTemporaryFile`). For files > 1MB, `UploadFile` is fine for most cases. For very large files (> 100MB) or when you need streaming processing (e.g., hash-as-you-upload), use `request.stream()`.

---

### Q2: How does StreamingResponse work internally, and when can it deadlock?

**Model answer:**

`StreamingResponse` wraps an async generator. When the ASGI server starts consuming the response, Starlette's `StreamingResponse.__call__()` drives the generator by calling `async for chunk in self.body_iterator`. For each chunk, it calls:
```python
await send({"type": "http.response.body", "body": chunk, "more_body": True})
```
After the last chunk:
```python
await send({"type": "http.response.body", "body": b"", "more_body": False})
```

**Deadlock scenario with `BaseHTTPMiddleware`:**

As covered in section 04, `BaseHTTPMiddleware` uses `call_next(request)` which starts the inner app in a background task and waits for it to produce a response via an `asyncio.Queue`. The issue:

1. `StreamingResponse` in the inner app yields chunks → puts them in the queue
2. `BaseHTTPMiddleware.dispatch()` receives the `StreamingResponse` object (not the chunks) — it's returned when `http.response.start` is received
3. The response body is still being generated in the background task
4. If `dispatch()` tries to buffer the body (to inspect or modify it), it waits on the generator
5. The generator waits for the background task to yield more
6. If the background task is blocked waiting for `send()` to return (which would require the outer layer to consume the chunk), and the outer layer is waiting for the buffer to fill — deadlock

The practical fix: never buffer `StreamingResponse` bodies in middleware. Pass them through unchanged.

---

### Q3: What HTTP/2 features are relevant to FastAPI applications, and what requires ASGI server configuration?

**Model answer:**

**HTTP/2 features and their ASGI implications:**

**Multiplexing:** Multiple requests over a single TCP connection. Transparent at the ASGI level — each request gets its own `(scope, receive, send)` invocation. No FastAPI changes needed.

**Server Push:** HTTP/2 allows the server to push resources before the client asks. Starlette checks `scope["extensions"]["http.response.push"]` — if present, you can use `await request.send_push_promise(path)`. Only available if the ASGI server supports it.

**Header compression (HPACK):** Transparent to the application layer.

**Binary framing:** Transparent.

**What requires ASGI server configuration:**
- TLS (required for HTTP/2 in browsers): SSL certificate/key in Uvicorn/Hypercorn config
- `h2` package: `pip install uvicorn[standard]` includes it
- Load balancer: must support HTTP/2 between client and LB, and separately between LB and backend (often called "HTTP/2 backend" or "h2c" in nginx/envoy config)

**FastAPI gotchas with HTTP/2:**
- `request.scope["http_version"]` will be `"2"` for HTTP/2 connections
- Connection-level headers (`Connection`, `Transfer-Encoding`) are not valid in HTTP/2 and some clients/servers will reject them — avoid sending them manually
- `Keep-Alive` header is meaningless in HTTP/2 (connections are inherently persistent)

---

### Q4: How do you implement Server-Sent Events (SSE) in FastAPI without a library?

**Model answer:**

SSE is HTTP with `Content-Type: text/event-stream`. The server sends newline-delimited "event" blocks, and the client keeps the connection open receiving them. It's unidirectional (server → client) and uses long-lived HTTP connections.

```python
import asyncio
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse

app = FastAPI()


async def event_generator(request: Request):
    event_id = 0
    while True:
        # Check if client disconnected
        if await request.is_disconnected():
            break
        
        event_id += 1
        data = f"data: {{\"id\": {event_id}, \"time\": {asyncio.get_event_loop().time():.2f}}}\n\n"
        yield data.encode()
        
        await asyncio.sleep(1)


@app.get("/events")
async def sse_endpoint(request: Request):
    return StreamingResponse(
        event_generator(request),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # disable nginx buffering
        },
    )
```

**SSE event format:**
```
data: {"message": "hello"}\n
\n
event: custom-event\n
data: {"message": "typed event"}\n
id: 42\n
retry: 3000\n
\n
```

Each field is on its own line; event blocks are separated by blank lines.

**vs WebSocket:**
- SSE: HTTP, unidirectional, auto-reconnect built into browser `EventSource`, works through standard HTTP proxies/CDNs
- WebSocket: separate protocol, bidirectional, requires WebSocket-aware proxies, more complex client code

For dashboards, live feeds, notifications: SSE is often the right choice. For chat, collaborative editing, gaming: WebSocket.

---

## Code: FileResponse with Range Support and Custom Media Type

```python
import os
from pathlib import Path
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse, Response

app = FastAPI()

MEDIA_DIR = Path("/var/media")


@app.get("/media/{filename}")
async def serve_media(filename: str, request: Request):
    filepath = MEDIA_DIR / filename
    
    if not filepath.exists() or not filepath.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    
    # Security: prevent path traversal
    try:
        filepath.resolve().relative_to(MEDIA_DIR.resolve())
    except ValueError:
        raise HTTPException(status_code=403, detail="Access denied")
    
    # FileResponse handles ETags, Last-Modified, and Range requests automatically
    return FileResponse(
        path=filepath,
        filename=filename,
        media_type="application/octet-stream",
    )


# Custom chunked streaming for large data generation
async def generate_csv(rows: int):
    yield b"id,name,value\n"
    for i in range(rows):
        yield f"{i},item_{i},{i * 1.5:.2f}\n".encode()


@app.get("/export/csv")
async def export_csv(rows: int = 1000):
    return StreamingResponse(
        generate_csv(rows),
        media_type="text/csv",
        headers={
            "Content-Disposition": 'attachment; filename="export.csv"',
            "Transfer-Encoding": "chunked",
        },
    )


# Custom Response subclass for NDJSON (newline-delimited JSON)
class NDJSONResponse(Response):
    media_type = "application/x-ndjson"

    def __init__(self, content, **kwargs):
        import json
        body = "\n".join(json.dumps(item) for item in content) + "\n"
        super().__init__(content=body.encode(), **kwargs)


@app.get("/items/ndjson", response_class=NDJSONResponse)
async def items_ndjson():
    return [{"id": 1, "name": "a"}, {"id": 2, "name": "b"}]
```

---

## Under the Hood

`StreamingResponse.__call__()` in `starlette/responses.py`:
1. Calls `await send({"type": "http.response.start", "status": self.status_code, "headers": ...})`
2. Iterates `self.body_iterator`:
   - If async: `async for chunk in self.body_iterator: await send({...body chunk...})`
   - If sync: wraps in `iterate_in_threadpool()` to avoid blocking the event loop
3. Calls `await send({"type": "http.response.body", "body": b"", "more_body": False})`

`FileResponse` uses `aiofiles` under the hood for async file reading (on non-Linux platforms) or `anyio`'s thread pool. On Linux, Starlette can use `sendfile(2)` syscall optimization if the ASGI server supports it — this sends file bytes directly from kernel buffer to socket without a copy through user space.
