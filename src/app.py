"""The Scribe server: one MCP tool, an audio upload route and a health check."""

from __future__ import annotations

import asyncio
import hmac
import os
from pathlib import Path
from typing import Annotated, Literal

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
from src.engine import CONTEXTS, STRUCTURES, STYLINGS, Cleaner, Cleanup, Decoder, RoutedEngine, SttEngine, probe_s1mini, probe_whisper
from src.errors import AudioRejected, EngineFailed, IdentityRequired, QuotaExceeded, ScribeError
from src.identity import owner_from_context
from src.middleware import TokenMiddleware
from src.tokens import TokenStore
from src.options import live, make_config
from src.runtime import Config
from src.service import ScribeService
from src.store import AudioStore
from src.streaming import serve_stream

WEB_DIR = Path(os.environ.get("SERVICE_WEB_DIR") or PROJECT_DIR / "web")

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


CONNECT = {
    "host": "scribe",
    "port": 29401,
    "paths": [
        {
            "label": "MCP, for Armory",
            "method": "POST",
            "path": "/mcp"
        },
        {
            "label": "Upload audio",
            "method": "POST",
            "path": "/api/files"
        },
        {
            "label": "Transcribe",
            "method": "POST",
            "path": "/api/transcribe"
        },
        {
            "label": "Partial transcript",
            "method": "POST",
            "path": "/api/transcribe-partial"
        },
        {
            "label": "Live dictation",
            "method": "WS",
            "path": "/api/stream"
        },
        {
            "label": "Health, no token",
            "method": "GET",
            "path": "/health"
        }
    ]
}


def flag(value: str | None):
    """?voice_gate=: True/False for true/false (also 1/0, on/off), None when absent, "bad" for anything else."""
    if value is None or value == "":
        return None
    return {"true": True, "1": True, "on": True, "false": False, "0": False, "off": False}.get(value.lower(), "bad")


def voice_prefilter(voice_dir, threshold):
    """The main-voice gate, or None when the optional "voice" extra (numpy, onnxruntime) is not installed."""
    try:
        from src.voicegate import build_prefilter
    except ImportError:
        return None
    return build_prefilter(voice_dir, threshold)


def build_app(settings: Settings, engine: SttEngine | None = None, config: Config | None = None) -> Starlette:
    """`config` (default: from SERVICE_ADMIN_PASSWORD, see options.make_config) makes the options in options.py
    changeable on /admin/ while running; without it they stay as the environment set them."""
    config = config if config is not None else make_config(settings)
    base, settings = settings, live(settings, config)
    backends = Backends(base.state_dir / "backends.json")
    tokens = TokenStore(base.state_dir / "tokens.json", "scribe_")
    usage = Usage()
    engine = engine or RoutedEngine(
        Decoder(lambda: settings.ffmpeg, max_seconds=lambda: settings.max_seconds, decode_timeout_s=lambda: settings.timeout_s),
        settings.model,
        lambda: backends.get("whisper"), timeout_s=lambda: settings.timeout_s + 5,
        prefilter=lambda force=None: voice_prefilter(base.voice_dir, settings.voice_gate_threshold)
        if (settings.voice_gate if force is None else force) else None,
    )
    store = AudioStore(base.audio_dir, lambda: settings.ttl_seconds, lambda: settings.quota_bytes)
    store.sweep()
    service = ScribeService(engine, store, settings, Cleaner(lambda: backends.get("s1mini"), timeout_s=lambda: settings.timeout_s))
    mcp = FastMCP(name="scribe", instructions=INSTRUCTIONS, host=settings.host, port=settings.port)

    @mcp.tool(
        meta={"keywords": ["transcribe", "speech to text", "stt", "audio", "dictation"], "display_label": "Transcribing audio"},
        annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
    )
    async def tool_scribe_transcribe(
        file_id: Annotated[str, Field(description="The file_id from the '[Audio file_id: ...]' line of the user's attachment.")],
        timestamps: Annotated[bool, Field(description="Also return per-segment start and end times.")] = False,
        cleanup: Annotated[bool, Field(description="Clean the text: drop filler words, fix punctuation and casing. Needs a cleanup service; timestamps stay as transcribed.")] = False,
        styling: Annotated[Literal[STYLINGS], Field(description="Tone of the cleaned text.")] = "semi-formal",
        structure: Annotated[Literal[STRUCTURES], Field(description="Cleaned text as prose or as lists.")] = "prose",
        context: Annotated[Literal[CONTEXTS], Field(description="Where the text goes: 'email' for an email, else 'general'.")] = "general",
        voice_gate: Annotated[bool | None, Field(description="Keep only the main (enrolled) voice: true silences other voices, false leaves the audio as it is, omitted follows the service setting. Needs a voice set up on the service; without it the result says it was skipped.")] = None,
        ctx: Context = None,
    ) -> TranscribeResult:
        """Transcribe an attached English audio file to text. The file belongs to the asking user and expires
        after 24 hours. Returns the transcript, the duration and a summary."""
        try:
            owner = owner_from_context(ctx)
            async with usage.track(owner, "transcribe"):
                return await service.transcribe(owner, file_id, timestamps, Cleanup(styling, structure, context) if cleanup else None, voice_gate)
        except ScribeError as error:
            raise ToolError(str(error)) from error

    @mcp.custom_route("/health", methods=["GET"])
    async def health(request: Request) -> Response:
        return JSONResponse({"ok": True, "ready": backends.get("whisper") is not None})

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
        query = request.query_params
        cleanup = None
        if query.get("cleanup") == "true":  # ?cleanup=true&styling=&structure=&context=
            cleanup = Cleanup(query.get("styling", "semi-formal"), query.get("structure", "prose"), query.get("context", "general"))
            if cleanup.styling not in STYLINGS or cleanup.structure not in STRUCTURES or cleanup.context not in CONTEXTS:
                return JSONResponse({"error": "bad_cleanup", "message": "Unknown styling, structure or context."}, status_code=422)
        voice_gate = flag(query.get("voice_gate"))
        if voice_gate == "bad":
            return JSONResponse({"error": "bad_voice_gate", "message": "voice_gate must be true or false."}, status_code=422)
        try:
            stored, expires_at = await service.upload(owner, filename, data)
            if request.url.path == "/api/transcribe":
                async with usage.track(owner, "transcribe"):
                    result = await service.transcribe(owner, stored.file_id, False, cleanup, voice_gate)
                body = {"text": result.text, "duration_s": result.duration_s}
                if cleanup:
                    body["cleaned"] = result.cleaned
                if voice_gate is not None:
                    body["voice_gate"] = result.voice_gate
                return JSONResponse(body)
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
            voice_gate = flag(request.query_params.get("voice_gate"))
            if voice_gate == "bad":
                return JSONResponse({"error": "bad_voice_gate", "message": "voice_gate must be true or false."}, status_code=422)
            async with usage.track(owner, "partial"):
                text, duration = await service.partial(owner, bytes(data), voice_gate)
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
        gate = flag(websocket.query_params.get("voice_gate"))
        gate = None if gate == "bad" else gate  # a typo falls back to the setting rather than refusing a live stream
        if not owner:  # serve_stream refuses it; nothing to count
            await serve_stream(websocket, service, settings, gate)
            return
        async with usage.track(owner, "dictation"):
            await serve_stream(websocket, service, settings, gate)

    async def status() -> list[dict[str, str]]:
        remote = backends.get("whisper")
        return [{"label": "Model", "value": settings.model},
                {"label": "Speech model runs", "value": "in a model service" if remote else "not connected"}]

    if config is not None:
        register(mcp, connect=CONNECT, service="Scribe", admin_password=base.admin_password, usage=usage, backends=backends, tokens=tokens, status=status,
                 playground=PLAYGROUND, slots=[
            Slot("whisper", "Speech to text", "The STT_Whisper_Model service that transcribes. Its model must match Scribe's model option, which keys the transcript cache.",
                 probe_whisper, "http://127.0.0.1:29300", local=False),
            Slot("s1mini", "Transcript cleanup", "The STC_S1Mini_Model service that cleans a transcript when a call asks for cleanup. Optional: with no address, cleanup is skipped.",
                 probe_s1mini, "http://127.0.0.1:29280", local=False),
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
    app.add_middleware(TokenMiddleware, tokens=tokens if base.admin_password else None)
    return app
