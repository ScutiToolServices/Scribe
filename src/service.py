"""Upload and transcribe: validate, store, cache lookup, transcribe under a semaphore."""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from src.config import Settings
from src.contract import SegmentOut, TranscribeResult
from src.engine import PCM_BYTES_PER_SECOND, Cleaner, Cleanup, SttEngine
from src.errors import AudioRejected, EngineFailed, FileNotFound, IdentityRequired
from src.store import AudioStore, StoredAudio


class ScribeService:
    def __init__(self, engine: SttEngine, store: AudioStore, settings: Settings, cleaner: Cleaner | None = None) -> None:
        self._engine, self._store, self._settings = engine, store, settings
        self._cleaner = cleaner or Cleaner(lambda: None)
        self._slots = asyncio.Semaphore(settings.concurrency)
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._inflight: set[asyncio.Task] = set()
        self._live = 0
        self._partials = 0
        self._reservations: dict[object, tuple[str, int]] = {}
        self._quota_lock = asyncio.Lock()

    def acquire_live(self) -> bool:
        if self._live >= self._settings.stream_capacity:
            return False
        self._live += 1
        return True

    def release_live(self) -> None:
        self._live -= 1

    def acquire_partial(self) -> bool:
        if self._partials >= self._settings.stream_capacity:
            return False
        self._partials += 1
        return True

    def release_partial(self) -> None:
        self._partials -= 1

    async def reserve(self, key: object, owner: str, size: int) -> None:
        async with self._quota_lock:
            total = size + sum(n for k, (who, n) in self._reservations.items() if k is not key and who == owner)
            await asyncio.to_thread(self._store.check_quota, owner, total)
            self._reservations[key] = (owner, size)

    def unreserve(self, key: object) -> None:
        self._reservations.pop(key, None)

    def _name(self, voice_gate: bool | None) -> str:
        """The transcript cache key for this call: gated and plain transcripts are stored apart."""
        name_for = getattr(self._engine, "name_for", None)
        return name_for(voice_gate) if name_for else self._engine.model_name

    async def infer(self, pcm: bytes, voice_gate: bool | None = None):
        # The task owns the slot until the worker really finishes. Cancelling a
        # to_thread await cannot stop Whisper and must never free its slot early.
        started = False
        async def run():
            nonlocal started
            async with self._slots:
                started = True
                # Fake engines and older ones take no flag: only pass it when the caller chose.
                return await (self._engine.transcribe(pcm) if voice_gate is None else self._engine.transcribe(pcm, voice_gate=voice_gate))

        task = asyncio.create_task(run())
        self._inflight.add(task)
        def completed(done):
            self._inflight.discard(done)
            if not done.cancelled():
                done.exception()
        task.add_done_callback(completed)
        try:
            return await asyncio.wait_for(asyncio.shield(task), self._settings.timeout_s)
        except TimeoutError as error:
            # Cancel only queued work; running work keeps its slot.
            if not started:
                task.cancel()
            raise EngineFailed("Transcription took too long. Try a shorter file.") from error
        except asyncio.CancelledError:
            if not started:
                task.cancel()
            raise
        except Exception as error:
            raise EngineFailed("Transcription failed. Try again.") from error

    async def partial(self, owner: str, data: bytes, voice_gate: bool | None = None) -> tuple[str, float]:
        key = object()
        try:
            await self.reserve(key, owner, len(data))
            pcm = await self._decode(data)
            await self.reserve(key, owner, max(len(data), len(pcm)))
            duration = len(pcm) / PCM_BYTES_PER_SECOND
            if duration > min(150, self._settings.max_seconds):
                raise AudioRejected("That audio is too long.")
            result = await self.infer(pcm, voice_gate)
            return result.text, duration
        finally:
            self.unreserve(key)

    async def upload(self, owner: str, filename: str, data: bytes) -> tuple[StoredAudio, float]:
        if not owner:
            raise IdentityRequired("Scribe needs to know who is asking.")
        duration = await self._probe(data)
        if duration > self._settings.max_seconds:
            raise AudioRejected(f"That audio is longer than {self._settings.max_seconds} seconds.")
        file_id = AudioStore.file_id(owner, data)
        async with self._quota_lock:
            reserved = sum(n for who, n in self._reservations.values() if who == owner)
            await asyncio.to_thread(self._store.check_quota, owner, reserved + len(data))
            stored = await asyncio.to_thread(self._store.put, owner, file_id, data, filename, duration)
        return stored, self._store.expires_at(stored)

    async def _probe(self, data: bytes) -> float:
        return len(await self._decode(data)) / PCM_BYTES_PER_SECOND

    async def _decode(self, data: bytes) -> bytes:
        """Decode a temporary copy: ffmpeg needs a seekable file for some formats (m4a)."""
        staging = self._settings.staging_dir
        await asyncio.to_thread(staging.mkdir, parents=True, exist_ok=True)

        def write() -> Path:
            handle = tempfile.NamedTemporaryFile(dir=staging, delete=False)
            with handle:
                handle.write(data)
            return Path(handle.name)

        path = await asyncio.to_thread(write)
        try:
            return await self._engine.decode(path)
        finally:
            await asyncio.to_thread(path.unlink, missing_ok=True)

    async def transcribe(self, owner: str, file_id: str, timestamps: bool, cleanup: Cleanup | None = None,
                         voice_gate: bool | None = None) -> TranscribeResult:
        if not owner:
            raise IdentityRequired("Scribe needs to know who is asking.")
        key = (owner, file_id)
        lock = self._locks.setdefault(key, asyncio.Lock())
        try:
            async with lock:
                stored = await asyncio.to_thread(self._store.get, owner, file_id)
                if stored is None:
                    raise FileNotFound("That audio file expired or does not exist. Attach it again.")
                payload = await asyncio.to_thread(self._store.get_transcript, owner, file_id, self._name(voice_gate))
                cached = payload is not None
                if payload is None:
                    payload = await self._run(owner, stored, voice_gate)
        finally:
            if not lock.locked() and not getattr(lock, "_waiters", None):
                self._locks.pop(key, None)
        segments = [SegmentOut(**s) for s in payload["segments"]] if timestamps else None
        text, note = payload["text"], ""
        gate_on = False
        if voice_gate is True:
            gate_on = getattr(self._engine, "gate_active", lambda *_: False)(True)
            if not gate_on:
                note += " Voice gate skipped: no voice is set up on this service."
        if cleanup is not None:  # the cache keeps the raw transcript; cleanup is applied per call
            if not self._cleaner.connected:
                note = " Cleanup skipped: no cleanup service is connected."
            else:
                try:
                    text = await self._cleaner.clean(text, cleanup)
                except EngineFailed:
                    note = " Cleanup failed, so this is the raw transcript."
        return TranscribeResult(
            text=text, duration_s=stored.duration_s, language=payload["language"],
            segments=segments, cached=cached, cleaned=cleanup is not None and "Cleanup" not in note, voice_gate=gate_on,
            message=f"Transcribed {stored.duration_s:.0f} s of audio ({len(payload['text'].split())} words).{note}",
        )

    async def _run(self, owner: str, stored: StoredAudio, voice_gate: bool | None = None) -> dict:
        pcm = await self._engine.decode(stored.path)
        transcript = await self.infer(pcm, voice_gate)
        payload = {
            "text": transcript.text, "language": transcript.language,
            "segments": [{"start_s": s.start_s, "end_s": s.end_s, "text": s.text} for s in transcript.segments],
        }
        await asyncio.to_thread(self._store.put_transcript, owner, stored.file_id, self._name(voice_gate), payload)
        return payload
