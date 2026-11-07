"""The "Data folder" setting moves stored data at the next start; settings and connections stay where they are."""

import json

from src.config import load_settings
from src.options import make_config


def first_folder(tmp_path, monkeypatch, saved):
    monkeypatch.setenv("SERVICE_DATA_DIR", str(tmp_path / "first"))
    monkeypatch.setenv("SERVICE_ADMIN_PASSWORD", "pw")
    (tmp_path / "first").mkdir(exist_ok=True)
    (tmp_path / "first" / "settings.json").write_text(json.dumps(saved))


def test_a_saved_data_folder_moves_stored_data_but_not_settings(tmp_path, monkeypatch):
    first_folder(tmp_path, monkeypatch, {"data_path": str(tmp_path / "moved")})
    loaded = load_settings()
    assert loaded.data_dir == tmp_path / "moved" and loaded.state_dir == tmp_path / "first"
    assert (tmp_path / "moved").is_dir()
    assert make_config(loaded).get("data_path") == str(tmp_path / "moved")  # still read from the first folder


def test_no_saved_folder_or_an_empty_one_changes_nothing(tmp_path, monkeypatch):
    for saved in ({}, {"data_path": ""}, {"data_path": 5}):
        first_folder(tmp_path, monkeypatch, saved)
        loaded = load_settings()
        assert loaded.data_dir == tmp_path / "first" and loaded.state_dir == tmp_path / "first"


def test_a_folder_that_cannot_be_written_is_ignored(tmp_path, monkeypatch):
    blocker = tmp_path / "file.txt"
    blocker.write_text("x")
    first_folder(tmp_path, monkeypatch, {"data_path": str(blocker / "inside")})
    assert load_settings().data_dir == tmp_path / "first"


def test_the_admin_page_lists_the_setting_as_a_restart_option(tmp_path, monkeypatch):
    first_folder(tmp_path, monkeypatch, {})
    row = next(r for r in make_config(load_settings()).describe() if r["key"] == "data_path")
    assert row["restart"] is True and row["value"] == "" and str(tmp_path / "first") in row["help"]
