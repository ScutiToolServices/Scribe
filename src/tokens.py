"""Per-caller API tokens for /mcp and /api, stored as hashes in one JSON file (`tokens.json` in the state folder).

The same file in every tool service (only the `prefix` passed in differs). The plaintext token is shown once, when it
is created on the admin page. Mutations lock a companion file, then atomically replace the JSON.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import threading
import time
from collections import deque
from contextlib import contextmanager
from pathlib import Path

LAST_USED_EVERY = 60  # seconds between writes of a token's last_used


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class TokenStore:
    def __init__(self, path: str | os.PathLike[str], prefix: str) -> None:
        self._path = Path(path)
        self._prefix = prefix
        self._hits: dict[str, deque] = {}  # recent call times per token id, for the rate limit
        self._guard = threading.Lock()

    def _load(self) -> list[dict]:
        try:
            return json.loads(self._path.read_text("utf-8"))
        except FileNotFoundError:
            return []

    @contextmanager
    def _edit(self):
        """Serialize read-modify-write across threads and processes."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.with_suffix(self._path.suffix + ".lock").open("a+b") as lock:
            if os.name == "nt":
                import msvcrt

                if lock.tell() == 0:
                    lock.write(b"\0")
                    lock.flush()
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl

                fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield self._load()
            finally:
                if os.name == "nt":
                    lock.seek(0)
                    msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(lock, fcntl.LOCK_UN)

    def _save(self, rows: list[dict]) -> None:
        temp = self._path.with_suffix(".tmp")
        temp.write_text(json.dumps(rows, indent=2), "utf-8")
        try:
            temp.chmod(0o600)
        except OSError:
            pass
        os.replace(temp, self._path)

    def list(self) -> list[dict]:
        return [{k: v for k, v in row.items() if k != "hash"} for row in self._load()]

    def create(self, name: str, rate: int | None = None, expires_days: int | None = None) -> dict:
        """The plaintext token is returned once and never stored. `rate` = calls per minute (None = no limit)."""
        token = self._prefix + secrets.token_urlsafe(32)
        row = {"id": secrets.token_hex(4), "name": name, "created": int(time.time()), "last_used": None, "hash": _hash(token)}
        if rate:
            row["rate"] = rate
        if expires_days:
            row["expires"] = row["created"] + expires_days * 86400
        with self._edit() as rows:
            self._save([*rows, row])
        return {**{k: v for k, v in row.items() if k != "hash"}, "token": token}

    def revoke(self, token_id: str) -> bool:
        with self._edit() as rows:
            kept = [row for row in rows if row["id"] != token_id]
            if len(kept) != len(rows):
                self._save(kept)
            return len(kept) != len(rows)

    def lookup(self, token: str) -> dict | None:
        """The token's row, or None when it is not a live token. Blocking: call it through `asyncio.to_thread`."""
        digest = _hash(token)
        with self._edit() as rows:
            for row in rows:
                if secrets.compare_digest(digest, row["hash"]):
                    now = int(time.time())
                    if row.get("expires") and row["expires"] <= now:
                        return None
                    if row.get("last_used") is None or now - row["last_used"] >= LAST_USED_EVERY:
                        row["last_used"] = now
                        self._save(rows)
                    return row
        return None

    def throttle(self, row: dict) -> int | None:
        """Seconds to wait when the token is over its calls per minute, else None."""
        rate = row.get("rate")
        if not rate:
            return None
        now = time.monotonic()
        with self._guard:
            hits = self._hits.setdefault(row["id"], deque())
            while hits and now - hits[0] >= 60:
                hits.popleft()
            if len(hits) >= rate:
                return max(1, int(60 - (now - hits[0])) + 1)
            hits.append(now)
        return None
