from src.config import Settings, load_settings


def test_loopback_without_token_is_fine():
    assert not Settings(host="127.0.0.1").exposed_without_auth


def test_exposed_without_auth_is_flagged():
    assert Settings(host="0.0.0.0").exposed_without_auth
    assert not Settings(host="0.0.0.0", admin_password="x").exposed_without_auth


def test_defaults_fit_armory_call_budget():
    settings = Settings()
    assert settings.model == "base.en" and settings.max_seconds == 150 and settings.timeout_s < 120


def test_environment_overrides(monkeypatch, tmp_path):
    monkeypatch.setenv("SERVICE_PORT", "9001")
    monkeypatch.setenv("SERVICE_MAX_SECONDS", "90")
    monkeypatch.setenv("SERVICE_TTL_HOURS", "2")
    monkeypatch.setenv("SERVICE_DATA_DIR", str(tmp_path))
    settings = load_settings()
    assert settings.port == 9001 and settings.max_seconds == 90
    assert settings.ttl_seconds == 7200 and settings.audio_dir == tmp_path / "audio"
