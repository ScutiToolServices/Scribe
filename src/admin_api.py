"""Admin API shared by the tool services Bard, Scribe and Reader (the same file in all three; change one, copy it
to the others). It adds, next to the existing /admin/settings:

  GET  /admin/status               uptime, usage per user, and service-specific items
  GET  /admin/backends             the model-service slots: local or a URL, whether a token is saved (never the token)
  PUT  /admin/backends/{key}       {"url", "token", "admin_password"?}: url "" = run it in this service; token omitted = keep the saved one;
                                   a slot with `admin` set needs the model's admin password with its address
  DELETE /admin/backends/{key}     forget the saved value, go back to the environment variable
  POST /admin/backends/{key}/test  {"url", "token"} or {"connection"} optional (the form's unsaved values); calls the model service
  PUT  /admin/backends/{key}       also takes {"connection": name}: use a saved connection (see below)
  PUT  /admin/connections/{name}   {"url", "token", "admin_password"?}: save a named model-service address, shared by every slot
  DELETE /admin/connections/{name} forget it (409 while a slot uses it)
  GET/POST /admin/tokens           the tokens callers (Armory, other services) send in X-Internal-Token; POST {"name",
                                   "rate"?, "expires_days"?} returns the plaintext token once
  DELETE /admin/tokens/{id}        revoke it

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
import re
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

from src.tokens import TokenStore

MAX_USERS = 200
MAX_URL = 300
MAX_TOKEN = 500
MAX_CONNECTIONS = 50
CONNECTIONS = "_connections"  # reserved key in backends.json: {name: {"url", "token"}}
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._-]{0,39}")


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
        row = self._saved.get(key)
        if row is not None:
            use = row.get("use")
            if use is None:
                return row, "saved"
            if isinstance(use, str) and isinstance(self._saved.get(CONNECTIONS, {}).get(use), dict):
                return self._saved[CONNECTIONS][use], "saved"  # a connection removed by hand falls through
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
        use = (self._saved.get(key) or {}).get("use") if source == "saved" else None
        return {"mode": "remote" if url else "local", "url": url, "token_set": bool(row.get("token")), "admin_set": bool(row.get("admin_password")), "source": source,
                "connection": use if isinstance(use, str) else ""}

    def connections(self) -> list[dict[str, Any]]:
        self._load()
        return [{"name": n, "url": r.get("url") or "", "token_set": bool(r.get("token")), "admin_set": bool(r.get("admin_password")),
                 "used_by": [k for k, v in self._saved.items() if k != CONNECTIONS and v.get("use") == n]}
                for n, r in sorted(self._saved.get(CONNECTIONS, {}).items()) if isinstance(r, dict)]

    def connection(self, name: str) -> tuple[str, str] | None:
        self._load()
        row = self._saved.get(CONNECTIONS, {}).get(name)
        return (row.get("url") or "", row.get("token") or "") if isinstance(row, dict) else None

    def admin_password(self, key: str) -> str:
        """The model service's admin password for the slot (from its connection when it uses one)."""
        return self._row(key)[0].get("admin_password") or ""

    def connection_admin(self, name: str) -> str:
        self._load()
        row = self._saved.get(CONNECTIONS, {}).get(name)
        return (row.get("admin_password") or "") if isinstance(row, dict) else ""

    def use(self, key: str, name: str) -> None:
        """Point a slot at a saved connection."""
        self._load()
        self._write({**self._saved, key: {"use": name}})

    def save_connection(self, name: str, url: str, token: str | None, admin: str | None = None) -> None:
        """`token` and `admin` None keep the current value; "" clears it."""
        self._load()
        known = self._saved.get(CONNECTIONS, {})
        old = known.get(name) or {}
        row = {"url": url, "token": (old.get("token") or "") if token is None else token}
        if admin or (admin is None and old.get("admin_password")):
            row["admin_password"] = (old.get("admin_password") or "") if admin is None else admin
        self._write({**self._saved, CONNECTIONS: {**known, name: row}})

    def delete_connection(self, name: str) -> None:
        self._load()
        known = {n: r for n, r in self._saved.get(CONNECTIONS, {}).items() if n != name}
        self._write({**self._saved, CONNECTIONS: known})

    def set(self, key: str, url: str, token: str | None, admin: str | None = None) -> None:
        """`token` and `admin` None keep the current value; "" clears it."""
        row, _ = self._row(key)
        saved = {"url": url, "token": (row.get("token") or "") if token is None else token}
        if admin or (admin is None and row.get("admin_password")):
            saved["admin_password"] = (row.get("admin_password") or "") if admin is None else admin
        self._write({**self._saved, key: saved})

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
    admin: str = ""  # label of the model service's admin password; set = it is required with the address


async def _unused(url: str, token: str) -> str:
    raise ProbeError("Not used.")


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


def register(mcp: Any, *, service: str, admin_password: str, usage: Usage, backends: Backends, tokens: TokenStore, slots: list[Slot],
             status: Callable[[], Awaitable[list[dict[str, str]]]], playground: dict[str, Any] | None = None,
             connect: dict[str, Any] | None = None) -> None:
    """Add shared admin routes, the optional public-request playground descriptor and the optional `connect` descriptor
    ({"host": docker name, "port": int, "paths": [{"label", "method", "path"}]}, shown on the page as the addresses other
    services can use); never include credentials."""
    by_key = {slot.key: slot for slot in slots}

    def denied(request: Request) -> Response | None:
        given = request.headers.get("x-admin-password", "").encode("utf-8")
        if not hmac.compare_digest(given, admin_password.encode("utf-8")):
            return JSONResponse({"detail": "Wrong admin password."}, status_code=401)
        return None

    def describe() -> list[dict[str, Any]]:
        return [{"key": s.key, "label": s.label, "help": s.help, "example": s.example, "local": s.local, "admin": s.admin,
                 **backends.describe(s.key)} for s in slots]

    def listing() -> dict[str, Any]:
        return {"service": service, "backends": describe(), "connections": backends.connections()}

    async def body_of(request: Request) -> dict[str, Any]:
        try:
            data = await request.json()
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    def cleaned(slot: Slot, data: dict[str, Any]) -> tuple[str, str | None, str | None]:
        url, token, admin = data.get("url", ""), data.get("token"), data.get("admin_password")
        if not isinstance(url, str) or not all(v is None or isinstance(v, str) for v in (token, admin)):
            raise ValueError("Send {\"url\": text, \"token\": text}.")
        url = url.strip().rstrip("/")
        if not url and not slot.local:
            raise ValueError(f"{slot.label} needs the address of its model service.")
        if url and not _valid_url(url):
            raise ValueError("The address must look like http://host:port, with no user name, query or fragment.")
        if any(v is not None and len(v) > MAX_TOKEN for v in (token, admin)):
            raise ValueError("That token or password is too long.")
        return url, token, admin

    @mcp.custom_route("/admin/status", methods=["GET"])
    async def admin_status(request: Request) -> Response:
        if (refused := denied(request)) is not None:
            return refused
        return JSONResponse({"service": service, "items": await status(), "usage": usage.snapshot(), "playground": playground,
                             "connect": connect})

    @mcp.custom_route("/admin/tokens", methods=["GET", "POST"])
    async def admin_tokens(request: Request) -> Response:
        if (refused := denied(request)) is not None:
            return refused
        if request.method == "GET":
            return JSONResponse({"tokens": await asyncio.to_thread(tokens.list)})
        data = await body_of(request)
        name, rate, days = data.get("name"), data.get("rate"), data.get("expires_days")
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 64:
            return JSONResponse({"detail": "The name takes 1 to 64 characters."}, status_code=422)
        for value, high in ((rate, 100_000), (days, 3650)):
            if value is not None and (type(value) is not int or not 1 <= value <= high):
                return JSONResponse({"detail": f"Rate and expiry are whole numbers from 1 to {high}."}, status_code=422)
        return JSONResponse(await asyncio.to_thread(tokens.create, name.strip(), rate, days))

    @mcp.custom_route("/admin/tokens/{token_id}", methods=["DELETE"])
    async def admin_token(request: Request) -> Response:
        if (refused := denied(request)) is not None:
            return refused
        if not await asyncio.to_thread(tokens.revoke, request.path_params["token_id"]):
            return JSONResponse({"detail": "No such token."}, status_code=404)
        return JSONResponse({"ok": True})

    @mcp.custom_route("/admin/backends", methods=["GET"])
    async def admin_backends(request: Request) -> Response:
        if (refused := denied(request)) is not None:
            return refused
        return JSONResponse(listing())

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
                if "connection" in data:
                    if not isinstance(data["connection"], str) or backends.connection(data["connection"]) is None:
                        raise ValueError("No such saved connection.")
                    if slot.admin and not backends.connection_admin(data["connection"]):
                        raise ValueError(f"That connection has no {slot.admin}. Edit it and add one.")
                elif "url" not in data:
                    raise ValueError("Send {\"url\": text, \"token\": text} or {\"connection\": name}.")
                else:
                    url, token, admin = cleaned(slot, data)
                    if slot.admin and url and not (admin or backends.admin_password(slot.key)):
                        raise ValueError(f"{slot.label} needs its {slot.admin} with the address.")
            except ValueError as error:
                return JSONResponse({"detail": str(error)}, status_code=422)
            if "connection" in data:
                await asyncio.to_thread(backends.use, slot.key, data["connection"])
            else:
                await asyncio.to_thread(backends.set, slot.key, url, token, admin)
        return JSONResponse(listing())

    @mcp.custom_route("/admin/connections/{name}", methods=["PUT", "DELETE"])
    async def admin_connection(request: Request) -> Response:
        if (refused := denied(request)) is not None:
            return refused
        name = request.path_params["name"]
        if request.method == "DELETE":
            used = next((c["used_by"] for c in backends.connections() if c["name"] == name), [])
            if used:
                return JSONResponse({"detail": "A slot still uses that connection: " + ", ".join(used) + "."}, status_code=409)
            await asyncio.to_thread(backends.delete_connection, name)
            return JSONResponse(listing())
        try:
            if not NAME.fullmatch(name):
                raise ValueError("The name takes 1 to 40 letters, digits, spaces, dots, dashes or underscores.")
            data = await body_of(request)
            url, token, admin = cleaned(Slot("", "A connection", "", _unused, local=False), data)
            if backends.connection(name) is None and len(backends.connections()) >= MAX_CONNECTIONS:
                raise ValueError("Too many saved connections.")
        except ValueError as error:
            return JSONResponse({"detail": str(error)}, status_code=422)
        await asyncio.to_thread(backends.save_connection, name, url, token, admin)
        return JSONResponse(listing())

    @mcp.custom_route("/admin/backends/{key}/test", methods=["POST"])
    async def admin_backend_test(request: Request) -> Response:
        if (refused := denied(request)) is not None:
            return refused
        slot = by_key.get(request.path_params["key"])
        if slot is None:
            return JSONResponse({"detail": "No such model service slot."}, status_code=404)
        data = await body_of(request)
        picked = data.get("connection")
        try:
            if "connection" in data:
                if not isinstance(picked, str) or backends.connection(picked) is None:
                    raise ValueError("No such saved connection.")
                url, token = backends.connection(picked)  # type: ignore[misc]
            else:
                url, token, _ = cleaned(slot, data) if "url" in data else ((backends.get(slot.key) or ("", ""))[0], None, None)
        except ValueError as error:
            return JSONResponse({"detail": str(error)}, status_code=422)
        if not url:
            return JSONResponse({"ok": True, "message": "Runs inside this service; nothing to connect to."})
        token = backends.token(slot.key) if token is None else token  # blank field = the saved token
        try:
            return JSONResponse({"ok": True, "message": await slot.probe(url, token or "")})
        except ProbeError as error:
            return JSONResponse({"ok": False, "message": str(error)})
