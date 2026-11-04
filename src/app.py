"""The Scribe server: one MCP tool, an audio upload route and a health check."""

from __future__ import annotations

import asyncio
import hmac
import os
from pathlib import Path
from typing import Annotated

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.staticfiles import StaticFiles
from starlette.websockets import WebSocket
from starlette.routing import Mount, WebSocketRoute

from src.config import PROJECT_DIR, Settings
from src.contract import TranscribeResult
from src.admin_api import Backends, Slot, Usage, register
from src.engine import RoutedEngine, SttEngine, WhisperEngine, probe_whisper
from src.errors import AudioRejected, EngineFailed, IdentityRequired, QuotaExceeded, ScribeError
from src.identity import owner_from_context
from src.middleware import InternalTokenMiddleware
from src.options import live, make_config
from src.runtime import Config
from src.service import ScribeService
from src.store import AudioStore
from src.streaming import serve_stream

WEB_DIR = Path(os.environ.get("SCRIBE_WEB_DIR") or PROJECT_DIR / "web")

PLAYGROUND = {"operations": [{
    "label": "Transcribe", "route": "/api/transcribe", "method": "POST", "encoding": "raw", "response": "json",
    "fields": [
        {"key": "X-Requester-Username", "label": "Requester username", "type": "text", "location": "header",
         "required": True, "default": "playground"},
        {"key": "file", "label": "Audio file", "type": "file", "required": True, "accept": "audio/*"},
    ],
}]}

INSTRUCTIONS = (
    "Speech to text (English). The user attaches an audio file in chat; the question then contains a line "
    "'[Audio file_id: ...]'. Call tool_scribe_transcribe with that file_id to get the transcript."
)


def build_app(settings: Settings, engine: SttEngine | None = None, config: Config | None = None) -> Starlette:
    """`config` (default: from SCRIBE_ADMIN_PASSWORD, see options.make_config) makes the options in options.py
    changeable on /admin/ while running; without it they stay as the environment set them."""
    config = config if config is not None else make_config(settings)
    base, settings = settings, live(settings, config)
    backends = Backends(base.data_dir / "backends.json", {"whisper": {"url": base.whisper_url, "token": base.whisper_token}})
    usage = Usage()
    engine = engine or RoutedEngine(
        WhisperEngine(
            base.models_dir, settings.model, lambda: settings.ffmpeg,
            max_seconds=lambda: settings.max_seconds, decode_timeout_s=lambda: settings.timeout_s,
        ),
        lambda: backends.get("whisper"), timeout_s=lambda: settings.timeout_s + 5,
    )
    store = AudioStore(base.audio_dir, lambda: settings.ttl_seconds, lambda: settings.quota_bytes)
    store.sweep()
    service = ScribeService(engine, store, settings)
    mcp = FastMCP(name="scribe", instructions=INSTRUCTIONS, host=settings.host, port=settings.port)

    @mcp.tool(
        meta={"keywords": ["transcribe", "speech to text", "stt", "audio", "dictation"], "display_label": "Transcribing audio"},
        annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
    )
    async def tool_scribe_transcribe(
        file_id: Annotated[str, Field(description="The file_id from the '[Audio file_id: ...]' line of the user's attachment.")],
        timestamps: Annotated[bool, Field(description="Also return per-segment start and end times.")] = False,
        ctx: Context = None,
    ) -> TranscribeResult:
        """Transcribe an attached English audio file to text. The file belongs to the asking user and expires
        after 24 hours. Returns the transcript, the duration and a summary."""
        try:
            owner = owner_from_context(ctx)
            async with usage.track(owner, "transcribe"):
                return await service.transcribe(owner, file_id, timestamps)
        except ScribeError as error:
            raise ToolError(str(error)) from error

    @mcp.custom_route("/health", methods=["GET"])
    async def health(request: Request) -> Response:
        return JSONResponse({"ok": True})

    @mcp.custom_route("/api/transcribe", methods=["POST"])
    @mcp.custom_route("/api/files", methods=["POST"])
    async def upload(request: Request) -> Response:
        owner = request.headers.get("x-requester-username", "").strip()
        if not owner:
            return JSONResponse({"error": "identity_required"}, status_code=401)
        # shortcut: the whole body is read before the size check; fine behind ember_api's own cap, add streaming if exposed.
        data = await request.body()
        if len(data) > settings.max_bytes:
            return JSONResponse({"error": "too_large"}, status_code=413)
        # Only a display name: last path part, bounded; it never becomes a path.
        filename = request.headers.get("x-filename", "audio").replace("\\", "/").rsplit("/", 1)[-1][:255] or "audio"
        try:
            stored, expires_at = await service.upload(owner, filename, data)
            if request.url.path == "/api/transcribe":
                async with usage.track(owner, "transcribe"):
                    result = await service.transcribe(owner, stored.file_id, False)
                return JSONResponse({"text": result.text, "duration_s": result.duration_s})
        except QuotaExceeded as error:
            return JSONResponse({"error": error.code, "message": str(error)}, status_code=413)
        except AudioRejected as error:
            return JSONResponse({"error": error.code, "message": str(error)}, status_code=400)
        except EngineFailed as error:
            return JSONResponse({"error": error.code, "message": str(error)}, status_code=503)
        except IdentityRequired:
            return JSONResponse({"error": "identity_required"}, status_code=401)
        return JSONResponse({
            "file_id": stored.file_id, "duration_s": round(stored.duration_s, 1),
            "size_bytes": stored.size_bytes, "expires_at": expires_at,
        })

    @mcp.custom_route("/api/transcribe-partial", methods=["POST"])
    async def partial(request: Request) -> Response:
        owner = request.headers.get("x-requester-username", "").strip()
        if not owner:
            return JSONResponse({"error": "identity_required"}, status_code=401)
        if not service.acquire_partial():
            return JSONResponse({"error": "stream_capacity", "message": "Dictation is busy. Try again."}, status_code=503)
        try:
            data = bytearray()
            async for chunk in request.stream():
                data.extend(chunk)
                if len(data) > settings.max_bytes:
                    return JSONResponse({"error": "too_large"}, status_code=413)
            async with usage.track(owner, "partial"):
                text, duration = await service.partial(owner, bytes(data))
            return JSONResponse({"text": text, "duration_s": duration})
        except QuotaExceeded:
            return JSONResponse({"error": "quota_exceeded", "message": "Your audio storage is full."}, status_code=413)
        except AudioRejected:
            return JSONResponse({"error": "audio_rejected", "message": "That audio cannot be transcribed."}, status_code=400)
        except Exception:
            return JSONResponse({"error": "engine_failed", "message": "Transcription failed. Try again."}, status_code=503)
        finally:
            service.release_partial()

    async def stream(websocket: WebSocket) -> None:
        owner = websocket.headers.get("x-requester-username", "").strip()
        if not owner:  # serve_stream refuses it; nothing to count
            await serve_stream(websocket, service, settings)
            return
        async with usage.track(owner, "dictation"):
            await serve_stream(websocket, service, settings)

    async def status() -> list[dict[str, str]]:
        remote = backends.get("whisper")
        return [{"label": "Model", "value": settings.model},
                {"label": "Speech model runs", "value": "in a model service" if remote else "in this process"}]

    if config is not None:
        register(mcp, service="Scribe", admin_password=base.admin_password, usage=usage, backends=backends, status=status,
                 playground=PLAYGROUND, slots=[
            Slot("whisper", "Speech to text", "Local runs faster-whisper inside Scribe. A model service is a STTWhisperModel address; "
                 "its model must match Scribe's model option, which keys the transcript cache.", probe_whisper, "http://127.0.0.1:9230"),
        ])

        @mcp.custom_route("/admin/settings", methods=["GET", "PUT"])
        async def admin_settings(request: Request) -> Response:
            given = request.headers.get("x-admin-password", "").encode("utf-8")
            if not hmac.compare_digest(given, base.admin_password.encode("utf-8")):
                return JSONResponse({"detail": "Wrong admin password."}, status_code=401)
            if request.method == "PUT":
                try:
                    values = (await request.json())["values"]
                    if not isinstance(values, dict):
                        raise TypeError
                except (ValueError, KeyError, TypeError):
                    return JSONResponse({"detail": "Send {\"values\": {...}}."}, status_code=422)
                try:
                    await asyncio.to_thread(config.set, values)
                except ValueError as error:
                    return JSONResponse({"detail": str(error)}, status_code=422)
            return JSONResponse({"service": "Scribe", "settings": config.describe()})

    app = mcp.streamable_http_app()
    if config is not None and WEB_DIR.is_dir():
        app.router.routes.append(Mount("/admin", StaticFiles(directory=WEB_DIR, html=True)))
    app.router.routes.append(WebSocketRoute("/api/stream", stream))
    app.add_middleware(InternalTokenMiddleware, token=settings.internal_token)
    return app
