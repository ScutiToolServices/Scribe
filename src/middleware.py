"""Require a token created on the admin page on /mcp and /api. `tokens=None` (no admin password, loopback only) = open."""

from __future__ import annotations

import asyncio
import json

from src.tokens import TokenStore

TOKEN_HEADER = b"x-internal-token"


def _protected(path: str) -> bool:
    return path == "/mcp" or path.startswith("/mcp/") or path.startswith("/api/")


class TokenMiddleware:
    def __init__(self, app, tokens: TokenStore | None) -> None:
        self.app = app
        self._tokens = tokens

    async def __call__(self, scope, receive, send):
        if self._tokens and scope["type"] in {"http", "websocket"} and _protected(scope.get("path", "")):
            provided = dict(scope.get("headers") or []).get(TOKEN_HEADER, b"").decode("latin-1")
            row = await asyncio.to_thread(self._tokens.lookup, provided)
            wait = self._tokens.throttle(row) if row else None
            if row is None or wait:
                if scope["type"] == "websocket":
                    await send({"type": "websocket.close", "code": 1008})
                    return
                status, error, extra = (401, "Invalid or missing token", []) if row is None else (
                    429, "Too many requests for this token", [(b"retry-after", str(wait).encode())])
                body = json.dumps({"error": error}).encode("utf-8")
                await send({"type": "http.response.start", "status": status,
                            "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()), *extra]})
                await send({"type": "http.response.body", "body": body})
                return
        await self.app(scope, receive, send)
