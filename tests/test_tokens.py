"""Shared by all five tool services (the same file in each): the token store and the middleware that checks it."""

import time

from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from src.middleware import TokenMiddleware
from src.tokens import TokenStore


def store(tmp_path):
    return TokenStore(tmp_path / "tokens.json", "tst_")


def test_a_created_token_works_and_is_stored_only_as_a_hash(tmp_path):
    tokens = store(tmp_path)
    made = tokens.create("armory")
    assert made["token"].startswith("tst_") and made["name"] == "armory"
    assert made["token"] not in (tmp_path / "tokens.json").read_text("utf-8")
    assert tokens.lookup(made["token"])["id"] == made["id"]
    assert tokens.lookup("tst_wrong") is None and tokens.lookup("") is None
    assert all("token" not in row and "hash" not in row for row in tokens.list())


def test_revoke_and_expiry_end_a_token(tmp_path):
    tokens = store(tmp_path)
    gone, old = tokens.create("gone"), tokens.create("old", expires_days=1)
    assert tokens.revoke(gone["id"]) and not tokens.revoke(gone["id"])
    assert tokens.lookup(gone["token"]) is None
    rows = tokens._load()
    rows[-1]["expires"] = int(time.time()) - 1
    tokens._save(rows)
    assert tokens.lookup(old["token"]) is None


def test_throttle_counts_calls_per_minute(tmp_path):
    tokens = store(tmp_path)
    row = tokens.lookup(tokens.create("slow", rate=2)["token"])
    assert tokens.throttle(row) is None and tokens.throttle(row) is None
    assert tokens.throttle(row) >= 1
    assert tokens.throttle({"id": "x"}) is None


def client_for(tokens):
    def ok(request):
        return PlainTextResponse("ok")

    inner = Starlette(routes=[Route("/api/x", ok), Route("/mcp", ok), Route("/health", ok), Route("/admin/y", ok)])
    inner.add_middleware(TokenMiddleware, tokens=tokens)
    return TestClient(inner)


def test_middleware_guards_mcp_and_api_but_not_health_or_admin(tmp_path):
    tokens = store(tmp_path)
    good = tokens.create("armory")["token"]
    client = client_for(tokens)
    for path in ("/api/x", "/mcp"):
        assert client.get(path).status_code == 401
        assert client.get(path, headers={"X-Internal-Token": "wrong"}).status_code == 401
        assert client.get(path, headers={"X-Internal-Token": good}).status_code == 200
    assert client.get("/health").status_code == 200 and client.get("/admin/y").status_code == 200


def test_middleware_answers_429_with_retry_after_over_the_rate(tmp_path):
    tokens = store(tmp_path)
    headers = {"X-Internal-Token": tokens.create("slow", rate=1)["token"]}
    client = client_for(tokens)
    assert client.get("/api/x", headers=headers).status_code == 200
    again = client.get("/api/x", headers=headers)
    assert again.status_code == 429 and int(again.headers["retry-after"]) >= 1


def test_middleware_without_a_store_is_open():
    assert client_for(None).get("/api/x").status_code == 200


def test_a_revoked_token_stops_working_at_once(tmp_path):
    tokens = store(tmp_path)
    made = tokens.create("armory")
    client = client_for(tokens)
    headers = {"X-Internal-Token": made["token"]}
    assert client.get("/api/x", headers=headers).status_code == 200
    tokens.revoke(made["id"])
    assert client.get("/api/x", headers=headers).status_code == 401
