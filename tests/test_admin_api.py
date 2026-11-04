"""Shared by Bard, Scribe and UnlimitedOCR (the same file in all three), like src/admin_api.py."""

import asyncio
import json

import httpx
import pytest
from mcp.server.fastmcp import FastMCP
from starlette.testclient import TestClient

from src.admin_api import Backends, ProbeError, Slot, Usage, check, register

REAL_CLIENT = httpx.AsyncClient


def make(tmp_path, defaults=None, probe=None, local=True):
    seen = []

    async def default_probe(url, token):
        seen.append((url, token))
        return "Reachable"

    async def status():
        return [{"label": "Engine", "value": "test"}]

    usage, backends = Usage(), Backends(tmp_path / "backends.json", defaults)
    mcp = FastMCP("t")
    register(mcp, service="Test", admin_password="pw", usage=usage, backends=backends, status=status,
             slots=[Slot("one", "Model one", "help", probe or default_probe, "http://127.0.0.1:1", local)])
    return TestClient(mcp.streamable_http_app()), usage, backends, seen


PW = {"X-Admin-Password": "pw"}


def test_every_route_needs_the_admin_password(tmp_path):
    c, *_ = make(tmp_path)
    for method, path in [("get", "/admin/status"), ("get", "/admin/backends"), ("put", "/admin/backends/one"),
                         ("delete", "/admin/backends/one"), ("post", "/admin/backends/one/test")]:
        assert getattr(c, method)(path).status_code == 401
        assert getattr(c, method)(path, headers={"X-Admin-Password": "nope"}).status_code == 401


def test_status_reports_service_items_and_usage(tmp_path):
    c, usage, *_ = make(tmp_path)
    usage.record("ana", "speak", 100, False)
    usage.record("ana", "speak", 300, True)
    usage.record("bo", "list", 50, False)
    body = c.get("/admin/status", headers=PW).json()
    assert body["service"] == "Test" and body["items"] == [{"label": "Engine", "value": "test"}]
    assert (body["usage"]["calls"], body["usage"]["errors"], body["usage"]["avg_ms"]) == (3, 1, 150)
    assert body["usage"]["tools"] == {"speak": 2, "list": 1}
    users = {row["user"]: row for row in body["usage"]["users"]}
    assert users["ana"]["calls"] == 2 and users["ana"]["errors"] == 1 and users["ana"]["avg_ms"] == 200


async def test_track_counts_a_failure_and_lets_it_through():
    usage = Usage()
    async with usage.track("ana", "t"):
        pass
    with pytest.raises(ValueError):
        async with usage.track("ana", "t"):
            raise ValueError("x")
    assert (usage.calls, usage.errors) == (2, 1)


def test_usage_keeps_at_most_200_users():
    usage = Usage()
    for number in range(250):
        usage.record(f"u{number}", "t", 1, False)
    assert len(usage.snapshot()["users"]) == 200 and usage.calls == 250


def test_a_slot_starts_from_the_environment_and_a_saved_value_beats_it(tmp_path):
    c, _, backends, _ = make(tmp_path, {"one": {"url": "http://env:9000/", "token": "envtok"}})
    first = c.get("/admin/backends", headers=PW).json()["backends"][0]
    assert (first["mode"], first["url"], first["token_set"], first["source"]) == ("remote", "http://env:9000", True, "environment")
    saved = c.put("/admin/backends/one", headers=PW, json={"url": "http://new:9001", "token": "tok2"}).json()["backends"][0]
    assert (saved["url"], saved["source"]) == ("http://new:9001", "saved") and backends.get("one") == ("http://new:9001", "tok2")
    assert "tok2" not in json.dumps(saved) and "envtok" not in json.dumps(saved)  # the token is never sent back
    back = c.delete("/admin/backends/one", headers=PW).json()["backends"][0]
    assert back["source"] == "environment" and backends.get("one") == ("http://env:9000", "envtok")


def test_an_omitted_token_keeps_the_saved_one_and_an_empty_one_clears_it(tmp_path):
    c, _, backends, _ = make(tmp_path)
    c.put("/admin/backends/one", headers=PW, json={"url": "http://a:1", "token": "secret"})
    c.put("/admin/backends/one", headers=PW, json={"url": "http://b:2"})
    assert backends.get("one") == ("http://b:2", "secret")
    c.put("/admin/backends/one", headers=PW, json={"url": "http://b:2", "token": ""})
    assert backends.get("one") == ("http://b:2", "")


def test_an_empty_url_means_local_and_beats_an_environment_url(tmp_path):
    c, _, backends, _ = make(tmp_path, {"one": {"url": "http://env:9000"}})
    body = c.put("/admin/backends/one", headers=PW, json={"url": ""}).json()["backends"][0]
    assert body["mode"] == "local" and backends.get("one") is None


def test_a_slot_without_a_local_engine_needs_a_url(tmp_path):
    c, *_ = make(tmp_path, local=False)
    assert c.put("/admin/backends/one", headers=PW, json={"url": ""}).status_code == 422


@pytest.mark.parametrize("url", ["ftp://x:1", "http://", "http://u:p@x:1", "http://x:1/?a=b", "http://x:1/#f", "x:1", "http://" + "a" * 400])
def test_bad_addresses_are_refused(tmp_path, url):
    c, *_ = make(tmp_path)
    assert c.put("/admin/backends/one", headers=PW, json={"url": url}).status_code == 422


def test_bad_bodies_and_unknown_slots(tmp_path):
    c, *_ = make(tmp_path)
    assert c.put("/admin/backends/one", headers=PW, json={"url": 5}).status_code == 422
    assert c.put("/admin/backends/one", headers=PW, json={"url": "http://a:1", "token": "x" * 501}).status_code == 422
    assert c.put("/admin/backends/one", headers=PW, content=b"not json").status_code == 422
    assert c.put("/admin/backends/one", headers=PW, json={"token": "x"}).status_code == 422  # no url: not a request to go local
    for method in ("put", "delete"):
        assert getattr(c, method)("/admin/backends/nope", headers=PW, **({"json": {"url": ""}} if method == "put" else {})).status_code == 404
    assert c.post("/admin/backends/nope/test", headers=PW).status_code == 404


def test_the_test_button_uses_the_form_values_and_falls_back_to_the_saved_token(tmp_path):
    c, _, _, seen = make(tmp_path)
    c.put("/admin/backends/one", headers=PW, json={"url": "http://a:1", "token": "saved"})
    ok = c.post("/admin/backends/one/test", headers=PW, json={"url": "http://other:2"}).json()
    assert ok == {"ok": True, "message": "Reachable"} and seen[-1] == ("http://other:2", "saved")
    c.post("/admin/backends/one/test", headers=PW, json={"url": "http://other:2", "token": "typed"})
    assert seen[-1] == ("http://other:2", "typed")
    c.post("/admin/backends/one/test", headers=PW)  # no body: the saved address and token
    assert seen[-1] == ("http://a:1", "saved")
    assert c.post("/admin/backends/one/test", headers=PW, json={"url": "javascript:x"}).status_code == 422


def test_testing_a_local_slot_connects_to_nothing(tmp_path):
    c, _, _, seen = make(tmp_path)
    body = c.post("/admin/backends/one/test", headers=PW).json()
    assert body["ok"] is True and "inside this service" in body["message"] and seen == []


def test_a_failed_probe_is_reported_not_raised(tmp_path):
    async def failing(url, token):
        raise ProbeError("The service refused the token (401).")

    c, *_ = make(tmp_path, probe=failing)
    body = c.post("/admin/backends/one/test", headers=PW, json={"url": "http://a:1"}).json()
    assert body == {"ok": False, "message": "The service refused the token (401)."}


def test_slots_survive_a_restart_and_a_hand_edit_is_noticed(tmp_path):
    _, _, backends, _ = make(tmp_path)
    backends.set("one", "http://a:1", "t")
    assert Backends(tmp_path / "backends.json").get("one") == ("http://a:1", "t")
    (tmp_path / "backends.json").write_text(json.dumps({"one": {"url": "http://edited:3", "token": ""}}), "utf-8")
    assert backends.get("one") == ("http://edited:3", "")
    (tmp_path / "backends.json").write_text("{broken", "utf-8")
    assert backends.get("one") is None  # an unreadable file is treated as nothing saved


@pytest.mark.parametrize("status,words", [(401, "token"), (403, "not allowed"), (429, "busy"), (500, "500")])
async def test_check_turns_every_failure_into_a_safe_message(monkeypatch, status, words):
    handler = lambda request: httpx.Response(status, text="boom at /secret/path http://inner:1")
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: REAL_CLIENT(transport=httpx.MockTransport(handler), **kw))
    with pytest.raises(ProbeError) as raised:
        await check("http://svc:1", "tok", "/health")
    assert words in str(raised.value) and "secret" not in str(raised.value) and "inner" not in str(raised.value)


async def test_check_sends_the_token_and_hides_connection_errors(monkeypatch):
    seen = {}

    def handler(request):
        seen["token"] = request.headers.get("X-Internal-Token")
        if request.url.path == "/down":
            raise httpx.ConnectError("refused http://svc:1")
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: REAL_CLIENT(transport=httpx.MockTransport(handler), **kw))
    assert (await check("http://svc:1", "tok", "/health")).json() == {"ok": True} and seen["token"] == "tok"
    with pytest.raises(ProbeError, match="Could not reach"):
        await check("http://svc:1", "", "/down")
    assert seen["token"] is None
