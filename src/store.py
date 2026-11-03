"""Per-owner audio uploads with a JSON sidecar, a transcript cache, a time-to-live and a quota.

Blocking by design (plain file IO); the service calls it through asyncio.to_thread.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from src.errors import QuotaExceeded
from src.runtime import as_getter

_ID = re.compile(r"^[0-9a-f]{32}$")


@dataclass(frozen=True)
class StoredAudio:
    file_id: str
    path: Path
    filename: str
    duration_s: float
    size_bytes: int
    created_at: float


class AudioStore:
    def __init__(
        self, root: Path, ttl_seconds: int | Callable[[], int], quota_bytes: int | Callable[[], int], clock: Callable[[], float] = time.time
    ) -> None:
        self._root = Path(root)
        self._ttl = as_getter(ttl_seconds)
        self._quota = as_getter(quota_bytes)
        self._clock = clock

    @staticmethod
    def file_id(owner: str, data: bytes) -> str:
        return hashlib.sha256(owner.encode("utf-8") + b"\0" + data).hexdigest()[:32]

    def expires_at(self, stored: StoredAudio) -> float:
        return stored.created_at + self._ttl()

    def _owner_dir(self, owner: str) -> Path:
        return self._root / hashlib.sha256(owner.encode("utf-8")).hexdigest()[:16]

    def _expired(self, stored: StoredAudio) -> bool:
        return self.expires_at(stored) <= self._clock()

    @staticmethod
    def _read(owner_dir: Path, file_id: str) -> StoredAudio | None:
        binary = owner_dir / f"{file_id}.bin"
        try:
            info = json.loads((owner_dir / f"{file_id}.json").read_text(encoding="utf-8"))
            return StoredAudio(
                file_id, binary, info["filename"], float(info["duration_s"]), binary.stat().st_size,
                float(info["created_at"]),
            )
        except (OSError, ValueError, KeyError):
            return None

    @staticmethod
    def _remove(owner_dir: Path, file_id: str) -> None:
        for suffix in (".bin", ".json", ".t.json"):
            (owner_dir / f"{file_id}{suffix}").unlink(missing_ok=True)

    @staticmethod
    def _write(path: Path, data: bytes) -> None:
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, path)

    def get(self, owner: str, file_id: str) -> StoredAudio | None:
        if not _ID.match(file_id):
            return None
        owner_dir = self._owner_dir(owner)
        stored = self._read(owner_dir, file_id)
        if stored is None:
            return None
        if self._expired(stored):
            self._remove(owner_dir, file_id)
            return None
        return stored

    def _sweep_dir(self, owner_dir: Path) -> int:
        removed = 0
        for sidecar in owner_dir.glob("*.json"):
            if not _ID.match(sidecar.stem):  # skips <id>.t.json transcripts
                continue
            stored = self._read(owner_dir, sidecar.stem)
            if stored is None or self._expired(stored):
                self._remove(owner_dir, sidecar.stem)
                removed += 1
        return removed

    def sweep(self) -> int:
        if not self._root.exists():
            return 0
        return sum(self._sweep_dir(d) for d in self._root.iterdir() if d.is_dir())

    def put(self, owner: str, file_id: str, data: bytes, filename: str, duration_s: float) -> StoredAudio:
        owner_dir = self._owner_dir(owner)
        owner_dir.mkdir(parents=True, exist_ok=True)
        self._sweep_dir(owner_dir)
        used = sum(p.stat().st_size for p in owner_dir.glob("*.bin"))
        if used + len(data) > self._quota():
            raise QuotaExceeded("Your audio storage is full. Wait for older audio to expire and try again.")
        created = self._clock()
        meta = {"filename": filename, "duration_s": duration_s, "created_at": created}
        self._write(owner_dir / f"{file_id}.json", json.dumps(meta).encode("utf-8"))
        self._write(owner_dir / f"{file_id}.bin", data)
        return StoredAudio(file_id, owner_dir / f"{file_id}.bin", filename, duration_s, len(data), created)

    def check_quota(self, owner: str, additional_bytes: int) -> None:
        owner_dir = self._owner_dir(owner)
        self._sweep_dir(owner_dir)
        used = sum(p.stat().st_size for p in owner_dir.glob("*.bin"))
        if used + additional_bytes > self._quota():
            raise QuotaExceeded("Your audio storage is full. Wait for older audio to expire and try again.")

    def get_transcript(self, owner: str, file_id: str, model: str) -> dict | None:
        if not _ID.match(file_id):
            return None
        try:
            payload = json.loads((self._owner_dir(owner) / f"{file_id}.t.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return payload if payload.get("model") == model else None

    def put_transcript(self, owner: str, file_id: str, model: str, payload: dict) -> None:
        path = self._owner_dir(owner) / f"{file_id}.t.json"
        self._write(path, json.dumps({**payload, "model": model}).encode("utf-8"))
