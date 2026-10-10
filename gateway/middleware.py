"""Request authentication, body limits, admission, and deadlines."""

import asyncio
from collections.abc import Callable

from fastapi import HTTPException
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send


class RequestGuard:
    def __init__(self, app: ASGIApp, *, authenticate: Callable[[str | None], None],
                 max_bytes: int, max_requests: int, deadline: float):
        self.app = app
        self.authenticate = authenticate
        self.max_bytes = max_bytes
        self.max_requests = max_requests
        self.deadline = deadline
        self.active = 0

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"].rstrip("/") not in {"/search", "/fetch"}:
            await self.app(scope, receive, send)
            return
        headers = scope.get("headers", [])
        auth = [value for key, value in headers if key.lower() == b"authorization"]
        try:
            self.authenticate(auth[0].decode("latin-1") if len(auth) == 1 else None)
        except HTTPException as error:
            await JSONResponse({"detail": error.detail}, error.status_code, headers=error.headers)(scope, receive, send)
            return
        if self.active >= self.max_requests:
            await JSONResponse({"detail": "Gateway busy; retry later"}, 503, headers={"Retry-After": "5"})(scope, receive, send)
            return
        # No await between check and increment: admission is atomic on one event loop.
        self.active += 1
        started = False

        async def tracked_send(message):
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            async with asyncio.timeout(self.deadline):
                lengths = [value for key, value in headers if key.lower() == b"content-length"]
                encodings = [value.lower() for key, value in headers if key.lower() == b"content-encoding"]
                if any(value != b"identity" for value in encodings):
                    await JSONResponse({"detail": "Compressed request bodies are not supported"}, 415)(scope, receive, send)
                    return
                if lengths:
                    if len(lengths) != 1 or not lengths[0].isdigit():
                        await JSONResponse({"detail": "Invalid Content-Length"}, 400)(scope, receive, send)
                        return
                    # Avoid parsing arbitrarily long decimal strings.
                    if len(lengths[0]) > 10 or int(lengths[0]) > self.max_bytes:
                        await JSONResponse({"detail": "Request body too large"}, 413)(scope, receive, send)
                        return
                body = bytearray()
                while True:
                    message = await receive()
                    if message["type"] == "http.disconnect":
                        return
                    chunk = message.get("body", b"")
                    if len(body) + len(chunk) > self.max_bytes:
                        await JSONResponse({"detail": "Request body too large"}, 413)(scope, receive, send)
                        return
                    body.extend(chunk)
                    if not message.get("more_body", False):
                        break
                delivered = False

                async def buffered_receive():
                    nonlocal delivered
                    if not delivered:
                        delivered = True
                        return {"type": "http.request", "body": bytes(body), "more_body": False}
                    return await receive()

                await self.app(scope, buffered_receive, tracked_send)
        except TimeoutError:
            if not started:
                await JSONResponse({"detail": "Gateway request deadline exceeded"}, 504)(scope, receive, send)
        finally:
            self.active -= 1
