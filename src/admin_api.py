"""Admin API shared by the tool services Bard, Scribe and Reader (the same file in all three; change one, copy it
to the others). It adds, next to the existing /admin/settings:

  GET  /admin/status               uptime, usage per user, and service-specific items
  GET  /admin/backends             the model-service slots: local or a URL, whether a token is saved (never the token)
  PUT  /admin/backends/{key}       {"url", "token"}: url "" = run it in this service; token omitted = keep the saved one
  DELETE /admin/backends/{key}     forget the saved value, go back to the environment variable
  POST /admin/backends/{key}/test  {"url", "token"} optional (the form's unsaved values); calls the model service

All behind the X-Admin-Password header. Slots are saved in `backends.json` beside the other data, so a change applies to
the next call without a restart.

shortcut: usage counters live in memory (they restart with the service) and the token is stored in clear text in
`backends.json` because it must be sent as it is; the file is private to the data folder. Upgrade to a secret store if
the host is shared."""

from __future__ import annotations

import asyncio
import hmac
import json
import os
import time
from collections import OrderedDict
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable
from urllib.parse import urlsplit

import httpx
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

MAX_USERS = 200
MAX_URL = 300
MAX_TOKEN = 500


class ProbeError(Exception):
    """A failed connection test. The message is safe to show: no URL, no text from the other service."""


class Usage:
    """Calls, errors and time per user, since the service started."""

    def __init__(self) -> None:
        self.started = time.time()
        self.calls = 0
        self.errors = 0
        self._total_ms = 0.0
        self._users: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._tools: dict[str, int] = {}

    def record(self, owner: str, tool: str, ms: float, failed: bool) -> None:
        self.calls += 1
        self.errors += failed
        self._total_ms += ms
        self._tools[tool] = self._tools.get(tool, 0) + 1
        row = self._users.setdefault(owner or "(unknown)", {"calls": 0, "errors": 0, "total_ms": 0.0, "last_at": 0.0})
        row["calls"] += 1
        row["errors"] += failed
        row["total_ms"] += ms
        row["last_at"] = time.time()
        self._users.move_to_end(owner or "(unknown)")
        while len(self._users) > MAX_USERS:
            self._users.popitem(last=False)

    @asynccontextmanager
    async def track(self, owner: str, tool: str) -> AsyncIterator[None]:
        """Wrap one call: it counts as an error when the body raises."""
        started = time.perf_counter()
        failed = False
        try:
            yield
        except BaseException:
            failed = True
            raise
        finally:
            self.record(owner, tool, (time.perf_counter() - started) * 1000, failed)

    def snapshot(self) -> dict[str, Any]:
        users = [{"user": name, "calls": row["calls"], "errors": row["errors"],
                  "avg_ms": round(row["total_ms"] / row["calls"]), "last_at": row["last_at"]}
                 for name, row in self._users.items()]
        return {"started_at": self.started, "uptime_s": round(time.time() - self.started), "calls": self.calls,
                "errors": self.errors, "avg_ms": round(self._total_ms / self.calls) if self.calls else 0,
                "tools": dict(self._tools), "users": sorted(users, key=lambda row: -row["last_at"])}


class Backends:
    """Where each model service lives. A saved value beats `defaults` (the environment variables read at start)."""

    def __init__(self, path: str | os.PathLike[str], defaults: dict[str, dict[str, str]] | None = None) -> None:
        self._path = Path(path)
        self._defaults = defaults or {}
        self._stamp: tuple[int, int] | None = None
        self._saved: dict[str, dict[str, str]] = {}

    def _load(self) -> None:
        """Read the file again when it changed (another replica, or a hand edit)."""
        try:
            info = self._path.stat()
            stamp = (info.st_mtime_ns, info.st_size)  # the size too: two writes can share a timestamp
        except OSError:
            stamp, self._saved = None, {}
            self._stamp = None
            return
        if stamp == self._stamp:
            return
        try:
            data = json.loads(self._path.read_text("utf-8"))
            self._saved = {k: v for k, v in data.items() if isinstance(v, dict)} if isinstance(data, dict) else {}
        except (OSError, ValueError):
            self._saved = {}
        self._stamp = stamp

    def _row(self, key: str) -> tuple[dict[str, str], str]:
        self._load()
        if key in self._saved:
            return self._saved[key], "saved"
        return self._defaults.get(key, {}), "environment"

    def get(self, key: str) -> tuple[str, str] | None:
        """(url, token) of the model service, or None to run it inside this service."""
        row, _ = self._row(key)
        url = (row.get("url") or "").strip().rstrip("/")
        return (url, row.get("token") or "") if url else None

    def token(self, key: str) -> str:
        return self._row(key)[0].get("token") or ""

    def describe(self, key: str) -> dict[str, Any]:
        row, source = self._row(key)
        url = (row.get("url") or "").strip().rstrip("/")
        return {"mode": "remote" if url else "local", "url": url, "token_set": bool(row.get("token")), "source": source}

    def set(self, key: str, url: str, token: str | None) -> None:
        """`token` None keeps the current one; "" clears it."""
        row, _ = self._row(key)
        keep = row.get("token") or ""
        self._write({**self._saved, key: {"url": url, "token": keep if token is None else token}})

    def reset(self, key: str) -> None:
        self._load()
        self._write({k: v for k, v in self._saved.items() if k != key})

    def _write(self, data: dict[str, dict[str, str]]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temp = self._path.with_name(self._path.name + ".tmp")
        temp.write_text(json.dumps(data, indent=2), "utf-8")
        try:
            temp.chmod(0o600)
        except OSError:
            pass
        os.replace(temp, self._path)
        self._stamp = None  # read it back on the next call


@dataclass(frozen=True)
class Slot:
    """One place a service can send work to. `probe(url, token)` returns a short success message or raises ProbeError.
    `local` is False when the service has no in-process engine for it (it must have a URL)."""

    key: str
    label: str
    help: str
    probe: Callable[[str, str], Awaitable[str]]
    example: str = ""
    local: bool = True


async def check(url: str, token: str, path: str, *, method: str = "GET", **kwargs: Any) -> httpx.Response:
    """One call to a model service with the token; every failure becomes a safe ProbeError."""
    headers = {"X-Internal-Token": token} if token else {}
    try:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=False) as http:
            reply = await http.request(method, url + path, headers=headers, **kwargs)
    except (httpx.HTTPError, ValueError):
        raise ProbeError("Could not reach that address.") from None
    if reply.status_code == 401:
        raise ProbeError("The service refused the token (401).")
    if reply.status_code == 403:
        raise ProbeError("The token is not allowed to do that (403).")
    if reply.status_code == 429:
        raise ProbeError("The service is busy or the token is over its rate limit (429).")
    if reply.status_code >= 400:
        raise ProbeError(f"The service answered {reply.status_code}.")
    return reply


def _valid_url(url: str) -> bool:
    parts = urlsplit(url)
    return (len(url) <= MAX_URL and parts.scheme in ("http", "https") and bool(parts.hostname)
            and not parts.username and not parts.password and not parts.fragment and not parts.query)


def register(mcp: Any, *, service: str, admin_password: str, usage: Usage, backends: Backends, slots: list[Slot],
             status: Callable[[], Awaitable[list[dict[str, str]]]], playground: dict[str, Any] | None = None) -> None:
    """Add shared admin routes and the optional public-request playground descriptor; never include credentials."""
    by_key = {slot.key: slot for slot in slots}

    def denied(request: Request) -> Response | None:
        given = request.headers.get("x-admin-password", "").encode("utf-8")
        if not hmac.compare_digest(given, admin_password.encode("utf-8")):
            return JSONResponse({"detail": "Wrong admin password."}, status_code=401)
        return None

    def describe() -> list[dict[str, Any]]:
        return [{"key": s.key, "label": s.label, "help": s.help, "example": s.example, "local": s.local,
                 **backends.describe(s.key)} for s in slots]

    async def body_of(request: Request) -> dict[str, Any]:
        try:
            data = await request.json()
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    def cleaned(slot: Slot, data: dict[str, Any]) -> tuple[str, str | None]:
        url, token = data.get("url", ""), data.get("token")
        if not isinstance(url, str) or not (token is None or isinstance(token, str)):
            raise ValueError("Send {\"url\": text, \"token\": text}.")
        url = url.strip().rstrip("/")
        if not url and not slot.local:
            raise ValueError(f"{slot.label} needs the address of its model service.")
        if url and not _valid_url(url):
            raise ValueError("The address must look like http://host:port, with no user name, query or fragment.")
        if token is not None and len(token) > MAX_TOKEN:
            raise ValueError("That token is too long.")
        return url, token

    @mcp.custom_route("/admin/status", methods=["GET"])
    async def admin_status(request: Request) -> Response:
        if (refused := denied(request)) is not None:
            return refused
        return JSONResponse({"service": service, "items": await status(), "usage": usage.snapshot(), "playground": playground})

    @mcp.custom_route("/admin/backends", methods=["GET"])
    async def admin_backends(request: Request) -> Response:
        if (refused := denied(request)) is not None:
            return refused
        return JSONResponse({"service": service, "backends": describe()})

    @mcp.custom_route("/admin/backends/{key}", methods=["PUT", "DELETE"])
    async def admin_backend(request: Request) -> Response:
        if (refused := denied(request)) is not None:
            return refused
        slot = by_key.get(request.path_params["key"])
        if slot is None:
            return JSONResponse({"detail": "No such model service slot."}, status_code=404)
        if request.method == "DELETE":
            await asyncio.to_thread(backends.reset, slot.key)
        else:
            try:
                data = await body_of(request)
                if "url" not in data:
                    raise ValueError("Send {\"url\": text, \"token\": text}.")
                url, token = cleaned(slot, data)
            except ValueError as error:
                return JSONResponse({"detail": str(error)}, status_code=422)
            await asyncio.to_thread(backends.set, slot.key, url, token)
        return JSONResponse({"service": service, "backends": describe()})

    @mcp.custom_route("/admin/backends/{key}/test", methods=["POST"])
    async def admin_backend_test(request: Request) -> Response:
        if (refused := denied(request)) is not None:
            return refused
        slot = by_key.get(request.path_params["key"])
        if slot is None:
            return JSONResponse({"detail": "No such model service slot."}, status_code=404)
        data = await body_of(request)
        try:
            url, token = cleaned(slot, data) if "url" in data else ((backends.get(slot.key) or ("", ""))[0], None)
        except ValueError as error:
            return JSONResponse({"detail": str(error)}, status_code=422)
        if not url:
            return JSONResponse({"ok": True, "message": "Runs inside this service; nothing to connect to."})
        token = backends.token(slot.key) if token is None else token  # blank field = the saved token
        try:
            return JSONResponse({"ok": True, "message": await slot.probe(url, token or "")})
        except ProbeError as error:
            return JSONResponse({"ok": False, "message": str(error)})
