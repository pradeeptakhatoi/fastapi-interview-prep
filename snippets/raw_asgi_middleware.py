"""
Raw ASGI middleware that preserves streaming responses and background tasks.
Drop-in for BaseHTTPMiddleware when either of those matters.
"""

import time
import uuid
from starlette.types import ASGIApp, Scope, Receive, Send
from starlette.datastructures import MutableHeaders


class TimingAndTracingMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        request_id = str(uuid.uuid4())
        scope.setdefault("state", {})
        scope["state"]["request_id"] = request_id

        start = time.perf_counter()

        async def send_with_headers(message: dict) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers.append("x-request-id", request_id)
                headers.append(
                    "x-response-time",
                    f"{(time.perf_counter() - start) * 1000:.2f}ms",
                )
            await send(message)

        await self.app(scope, receive, send_with_headers)


# Usage:
# app.add_middleware(TimingAndTracingMiddleware)
