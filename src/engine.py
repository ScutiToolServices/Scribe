"""Speech to text. ffmpeg decodes here (async subprocess); the transcription is done by a STTWhisperModel service."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

import httpx

from src.admin_api import ProbeError, check
from src.errors import AudioRejected, EngineFailed
from src.runtime import as_getter

PCM_BYTES_PER_SECOND = 16000 * 2  # 16 kHz, mono, 16-bit


@dataclass(frozen=True)
class Segment:
    start_s: float
    end_s: float
    text: str


@dataclass(frozen=True)
class Transcript:
    text: str
    segments: list[Segment]
    language: str
    words: list[Segment] = field(default_factory=list)


class SttEngine(Protocol):
    model_name: str

    async def decode(self, path: Path) -> bytes:
        """16 kHz mono s16le PCM. Raises AudioRejected when ffmpeg cannot read the file."""

    async def transcribe(self, pcm: bytes) -> Transcript:
        """Raises EngineFailed."""


class Decoder:
    """Audio file to 16 kHz mono PCM."""

    def __init__(
        self, ffmpeg: str | Callable[[], str],
        max_seconds: float | Callable[[], float] = 150, decode_timeout_s: float | Callable[[], float] = 110,
    ) -> None:
        self._ffmpeg = as_getter(ffmpeg)
        self._max_seconds = as_getter(max_seconds)
        self._decode_timeout_s = as_getter(decode_timeout_s)

    def _limit(self) -> float:
        return min(150, self._max_seconds())

    async def decode(self, path: Path) -> bytes:
        try:
            process = await asyncio.create_subprocess_exec(
                self._ffmpeg(), "-v", "error", "-nostdin", "-i", str(path),
                "-t", f"{self._limit() + 1:g}",
                "-f", "s16le", "-ar", "16000", "-ac", "1", "pipe:1",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            )
        except OSError as error:
            raise EngineFailed("Audio decoding is unavailable.") from error
        # Decode at most one extra second so oversize input is rejected rather
        # than silently truncated. Do not retain arbitrarily verbose stderr.
        output = asyncio.create_task(process.communicate())
        try:
            pcm, _ = await asyncio.wait_for(asyncio.shield(output), self._decode_timeout_s())
        except (TimeoutError, asyncio.CancelledError) as error:
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
            await output  # drain pipes and reap before staging-file cleanup
            if isinstance(error, asyncio.CancelledError):
                raise
            raise EngineFailed("Audio decoding took too long. Try a shorter file.") from error
        if process.returncode != 0 or not pcm:
            raise AudioRejected("That file is not audio Scribe can read.")
        if len(pcm) > self._limit() * PCM_BYTES_PER_SECOND:
            raise AudioRejected("That audio is too long.")
        return pcm


async def remote_transcribe(url: str, token: str, pcm: bytes, timeout_s: float) -> Transcript:
    """POST the PCM to a STTWhisperModel service. Every failure is one safe message (httpx, JSON and shape errors vary and
    may hold URLs)."""
    params = {"language": "en", "beam_size": 1, "word_timestamps": "true"}
    try:
        async with httpx.AsyncClient(timeout=timeout_s) as http:
            reply = await http.post(url + "/v1/transcribe", params=params, content=pcm,
                                    headers={"X-Internal-Token": token} if token else {})
        reply.raise_for_status()
        body = reply.json()
        return Transcript(
            body["text"],
            [Segment(s["start_s"], s["end_s"], s["text"]) for s in body["segments"]],
            body["language"],
            [Segment(w["start_s"], w["end_s"], w["text"]) for w in body["words"]],
        )
    except Exception as error:
        raise EngineFailed("Transcription failed. Try again.") from error


async def probe_whisper(url: str, token: str) -> str:
    """The admin page's Test button for a STTWhisperModel address: reachable, then the token on a tenth of a second of silence."""
    await check(url, "", "/health")
    reply = await check(url, token, "/v1/transcribe", method="POST", content=b"\0\0" * 1600,
                        params={"language": "en", "word_timestamps": "false"})
    try:
        return f"Connected, token accepted, model {reply.json()['model']}."
    except (ValueError, KeyError, TypeError):
        raise ProbeError("That address is not a STTWhisperModel service.") from None


class RoutedEngine:
    """Decoding here, transcription at the STTWhisperModel service in the "whisper" slot (saved on the admin page). The slot
    is read on every call, so a change on the admin page applies to the next transcription. With no address saved,
    transcribing fails with a hint to connect one.

    `model_name` is Scribe's configured name, so it must match the model the service runs; cached transcripts are keyed
    by it."""

    def __init__(self, decoder: Decoder, model_name: str, slot: Callable[[], tuple[str, str] | None],
                 timeout_s: float | Callable[[], float] = 115) -> None:
        self._decoder, self.model_name, self._slot, self._timeout_s = decoder, model_name, slot, as_getter(timeout_s)

    @property
    def is_remote(self) -> bool:
        return self._slot() is not None

    async def decode(self, path: Path) -> bytes:
        return await self._decoder.decode(path)

    async def transcribe(self, pcm: bytes) -> Transcript:
        remote = self._slot()
        if remote is None:
            raise EngineFailed("No speech model service is connected. Save a connection on the Scribe admin page.")
        return await remote_transcribe(remote[0], remote[1], pcm, self._timeout_s())
