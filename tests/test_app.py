from dataclasses import replace

from starlette.testclient import TestClient

from src.app import build_app
from tests.conftest import FakeEngine, audio
from tests.seed import seed_token

ANN = {"X-Requester-Username": "ann"}
TOK = {**ANN, "X-Internal-Token": "secret"}  # a seeded token, for the tests that run with an admin password


def make(settings, **changes):
    settings = replace(settings, **changes)
    if settings.admin_password:
        seed_token(settings, "secret")
    return TestClient(build_app(settings, FakeEngine()))


def test_health(settings):
    assert make(settings).get("/health").json() == {"ok": True, "ready": False}


def test_upload_requires_identity(settings):
    assert make(settings).post("/api/files", content=audio()).status_code == 401


def test_upload_ok(settings):
    response = make(settings).post(
        "/api/files", content=audio(2.0), headers={**ANN, "X-Filename": "../../a.mp3"}
    )
    body = response.json()
    assert response.status_code == 200 and len(body["file_id"]) == 32
    assert body["duration_s"] == 2.0 and body["size_bytes"] == len(audio(2.0)) and body["expires_at"] > 0


def test_upload_rejects_bad_audio_and_long_audio(settings):
    c = make(settings)
    assert c.post("/api/files", content=audio(bad=True), headers=ANN).status_code == 400
    assert c.post("/api/files", content=audio(11.0), headers=ANN).status_code == 400


def test_upload_over_byte_cap_is_413(settings):
    assert make(settings, max_bytes=10).post("/api/files", content=audio(), headers=ANN).status_code == 413


def test_upload_over_quota_is_413(settings):
    c = make(settings, quota_bytes=10)
    response = c.post("/api/files", content=audio(), headers=ANN)
    assert response.status_code == 413 and response.json()["error"] == "quota_exceeded"


def test_upload_needs_token_when_configured(settings):
    c = make(settings, admin_password="pw")
    assert c.post("/api/files", content=audio(), headers=ANN).status_code == 401
    assert c.post("/api/files", content=audio(), headers={**ANN, "X-Internal-Token": "secret"}).status_code == 200


def test_dictation_returns_text_and_reuses_cache(settings):
    engine = FakeEngine()
    client = TestClient(build_app(settings, engine))
    for _ in range(2):
        response = client.post("/api/transcribe", content=audio(2), headers=ANN)
        assert response.status_code == 200
        assert response.json() == {"text": "hello world", "duration_s": 2.0}
    assert engine.transcribe_calls == 1


def test_dictation_auth_and_validation(settings):
    client = make(settings, admin_password="pw")
    assert client.post("/api/transcribe", content=audio(), headers=ANN).status_code == 401
    headers = {**ANN, "X-Internal-Token": "secret"}
    assert client.post("/api/transcribe", content=audio(), headers={"X-Internal-Token": "secret"}).status_code == 401
    assert client.post("/api/transcribe", content=audio(bad=True), headers=headers).status_code == 400
    assert client.post("/api/transcribe", content=audio(11), headers=headers).status_code == 400
    assert make(settings, max_bytes=10).post("/api/transcribe", content=audio(), headers=ANN).status_code == 413
    assert make(settings, quota_bytes=10).post("/api/transcribe", content=audio(), headers=ANN).status_code == 413


def test_dictation_engine_failure_is_503(settings):
    client = TestClient(build_app(settings, FakeEngine(fail=True)))
    response = client.post("/api/transcribe", content=audio(), headers=ANN)
    assert response.status_code == 503
    assert response.json()["error"] == "engine_failed"


def admin(settings):
    return TestClient(build_app(seed_token(replace(settings, admin_password="pw"), "secret"), FakeEngine()))


AUTH = {"X-Admin-Password": "pw"}


def test_admin_is_off_without_a_password(settings):
    assert make(settings).get("/admin/settings").status_code in (404, 405)


def test_admin_needs_the_password(settings):
    c = admin(settings)
    assert c.get("/admin/settings").status_code == 401
    assert c.put("/admin/settings", json={"values": {}}, headers={"X-Admin-Password": "x"}).status_code == 401


def test_admin_saves_and_the_service_uses_it(settings):
    c = admin(settings)
    rows = {r["key"]: r for r in c.get("/admin/settings", headers=AUTH).json()["settings"]}
    assert rows["max_seconds"]["value"] == settings.max_seconds and rows["model"]["restart"]
    assert c.put("/admin/settings", json={"values": {"max_seconds": 1}}, headers=AUTH).status_code == 200
    assert c.post("/api/files", content=audio(2.0), headers=TOK).status_code == 400  # now over the 1 s limit


def test_admin_refuses_bad_values(settings):
    c = admin(settings)
    assert c.put("/admin/settings", json={"values": {"max_seconds": 0}}, headers=AUTH).status_code == 422
    assert c.put("/admin/settings", json={"values": {"nope": 1}}, headers=AUTH).status_code == 422
    assert c.put("/admin/settings", json={"oops": 1}, headers=AUTH).status_code == 422


def test_admin_page_is_served_without_a_token(settings):
    assert admin(settings).get("/admin/").status_code == 200


def test_admin_upload_limit_applies_without_restart(settings):
    c = admin(settings)
    assert c.post("/api/files", content=audio(1.0), headers=TOK).status_code == 200
    assert c.put("/admin/settings", json={"values": {"max_upload_mb": 1}}, headers=AUTH).status_code == 200
    assert c.post("/api/files", content=b"" * (2 * 1024 * 1024), headers=TOK).status_code == 413


def test_the_admin_page_shows_status_usage_and_the_whisper_slot(settings):
    c = make(settings, admin_password="pw")
    pw = {"X-Admin-Password": "pw"}
    assert c.get("/admin/status").status_code == 401
    c.post("/api/transcribe", content=audio(), headers=TOK)
    c.post("/api/transcribe", content=audio(), headers=TOK)
    body = c.get("/admin/status", headers=pw).json()
    assert body["usage"]["calls"] == 2 and body["usage"]["users"][0]["user"] == "ann"
    assert {"label": "Speech model runs", "value": "not connected"} in body["items"]
    slots = c.get("/admin/backends", headers=pw).json()["backends"]
    assert [(s["key"], s["url"]) for s in slots] == [("whisper", "")]
    saved = c.put("/admin/backends/whisper", headers=pw, json={"url": "http://w:29300", "token": "t"}).json()["backends"][0]
    assert saved["mode"] == "remote" and saved["token_set"] is True
    assert {"label": "Speech model runs", "value": "in a model service"} in c.get("/admin/status", headers=pw).json()["items"]

