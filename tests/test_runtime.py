import pytest

from src.config import Settings
from src.options import live, make_config
from src.runtime import Config, Setting, as_getter

COUNT = Setting("count", "Count", "int", 5, "T_COUNT", 1, 10)


def test_saved_beats_environment_beats_default(tmp_path, monkeypatch):
    config = Config(tmp_path / "s.json", [COUNT])
    assert config.get("count") == 5
    monkeypatch.setenv("T_COUNT", "7")
    assert config.get("count") == 7
    config.set({"count": 3})
    assert config.get("count") == 3
    config.set({"count": None})
    assert config.get("count") == 7


@pytest.mark.parametrize("value", [0, 11, 2.5, "3", True])
def test_bad_values_are_refused_and_nothing_is_saved(tmp_path, value):
    config = Config(tmp_path / "s.json", [COUNT])
    with pytest.raises(ValueError):
        config.set({"count": value})
    assert config.get("count") == 5


def test_unknown_key_is_refused(tmp_path):
    with pytest.raises(ValueError):
        Config(tmp_path / "s.json", [COUNT]).set({"nope": 1})


def test_as_getter_takes_a_number_or_a_function():
    assert as_getter(4)() == 4
    assert as_getter(lambda: 9)() == 9


def test_no_password_means_no_config(tmp_path):
    assert make_config(Settings(data_dir=tmp_path)) is None


def test_live_settings_follow_a_saved_change(tmp_path):
    base = Settings(data_dir=tmp_path, admin_password="pw", ttl_seconds=3600)
    config = make_config(base)
    settings = live(base, config)
    assert (settings.ttl_seconds, settings.port) == (3600, 29401)
    config.set({"ttl_hours": 2, "quota_mb": 5, "max_seconds": 60, "stream_idle_s": 3})
    assert settings.ttl_seconds == 7200
    assert settings.quota_bytes == 5 * 1024 * 1024
    assert (settings.max_seconds, settings.stream_idle_s) == (60, 3.0)
