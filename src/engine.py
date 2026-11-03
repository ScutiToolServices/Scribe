"""Speech-to-text engines. ffmpeg decodes (async subprocess); the model runs in a worker thread."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

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


class WhisperEngine:
    def __init__(
        self, models_dir: Path, model_name: str, ffmpeg: str,
        max_seconds: float | Callable[[], float] = 150, decode_timeout_s: float | Callable[[], float] = 110,
    ) -> None:
        self.model_name = model_name
        self._models_dir = Path(models_dir)
        self._ffmpeg = ffmpeg
        self._max_seconds = as_getter(max_seconds)
        self._decode_timeout_s = as_getter(decode_timeout_s)
        self._model = None
        self._lock = asyncio.Lock()

    def _limit(self) -> float:
        return min(150, self._max_seconds())

    async def decode(self, path: Path) -> bytes:
        try:
            process = await asyncio.create_subprocess_exec(
                self._ffmpeg, "-v", "error", "-nostdin", "-i", str(path),
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

    def _load(self):
        from faster_whisper import WhisperModel  # heavy import, kept out of module load

        return WhisperModel(
            self.model_name, device="cpu", compute_type="int8", download_root=str(self._models_dir)
        )

    def _run(self, pcm: bytes) -> Transcript:
        import numpy as np

        audio = np.frombuffer(pcm, np.int16).astype(np.float32) / 32768.0
        segments, _ = self._model.transcribe(audio, language="en", beam_size=1, word_timestamps=True)
        parts, words = [], []
        for s in segments:
            parts.append(Segment(round(s.start, 2), round(s.end, 2), s.text.strip()))
            words.extend(Segment(w.start, w.end, w.word.strip()) for w in (s.words or []))
        return Transcript(" ".join(p.text for p in parts if p.text), parts, "en", words)

    async def transcribe(self, pcm: bytes) -> Transcript:
        try:
            async with self._lock:  # one model load, even for concurrent first calls
                if self._model is None:
                    self._model = await asyncio.to_thread(self._load)
            return await asyncio.to_thread(self._run, pcm)
        except Exception as error:  # model errors vary; never leak their text (may hold paths)
            raise EngineFailed("Transcription failed. Try again.") from error
