"""
Server-Sent Events (SSE) endpoint using StreamingResponse + async generator.
Handles client disconnect detection and proper event formatting.
"""

import asyncio
import json
import time
from collections.abc import AsyncGenerator

from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse

app = FastAPI()


def format_sse(data: dict | str, event: str | None = None, id: int | None = None) -> str:
    lines = []
    if id is not None:
        lines.append(f"id: {id}")
    if event:
        lines.append(f"event: {event}")
    payload = json.dumps(data) if isinstance(data, dict) else data
    lines.append(f"data: {payload}")
    lines.append("")   # blank line terminates the event
    lines.append("")   # extra blank line for good measure
    return "\n".join(lines)


async def event_stream(request: Request) -> AsyncGenerator[str, None]:
    """Yields SSE-formatted strings until client disconnects."""
    event_id = 0
    try:
        while True:
            if await request.is_disconnected():
                break

            event_id += 1
            yield format_sse(
                data={"ts": time.time(), "id": event_id},
                event="tick",
                id=event_id,
            )
            await asyncio.sleep(1)

    except asyncio.CancelledError:
        pass  # client disconnected; generator is being garbage-collected


@app.get("/events")
async def sse(request: Request) -> StreamingResponse:
    return StreamingResponse(
        event_stream(request),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",    # tells nginx not to buffer this response
        },
    )


# Fan-out SSE: broadcast from a shared asyncio.Queue to multiple clients
_broadcast_queue: asyncio.Queue[dict] = asyncio.Queue()


async def broadcaster(request: Request) -> AsyncGenerator[str, None]:
    # Each client gets its own copy from the shared queue via get()
    # In production, use a pub/sub (Redis SUBSCRIBE) instead of a shared queue
    # to support multiple workers.
    event_id = 0
    try:
        while not await request.is_disconnected():
            try:
                event = await asyncio.wait_for(_broadcast_queue.get(), timeout=30)
                event_id += 1
                yield format_sse(data=event, id=event_id)
            except asyncio.TimeoutError:
                # Heartbeat to keep connection alive through proxies
                yield ": heartbeat\n\n"
    except asyncio.CancelledError:
        pass


@app.get("/broadcast")
async def broadcast_stream(request: Request) -> StreamingResponse:
    return StreamingResponse(broadcaster(request), media_type="text/event-stream")


@app.post("/broadcast/send")
async def send_event(data: dict) -> dict:
    await _broadcast_queue.put(data)
    return {"queued": True}
