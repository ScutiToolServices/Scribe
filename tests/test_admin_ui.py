"""Playground browser checks through real upload/transcription routes with a fake engine."""

import socket
import threading
import time
from pathlib import Path

import pytest
import uvicorn

sync_api = pytest.importorskip("playwright.sync_api")

from src.app import build_app
from src.config import Settings
from tests.conftest import audio

TOKEN = "playground-service-token"
PASSWORD = "playground-admin-password"


@pytest.fixture
def base_url(tmp_path, engine):
    app = build_app(Settings(data_dir=tmp_path, internal_token=TOKEN, admin_password=PASSWORD), engine)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    try:
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert server.started, "Fake-engine HTTP server did not start"
        yield f"http://127.0.0.1:{port}/admin/"
    finally:
        server.should_exit = True
        thread.join(5)


@pytest.fixture
def page(base_url):
    with sync_api.sync_playwright() as playwright:
        if not Path(playwright.chromium.executable_path).exists():
            pytest.skip("Playwright Chromium is not installed")
        browser = playwright.chromium.launch()
        page = browser.new_page()
        try:
            page.goto(base_url)
            page.fill("#password", PASSWORD)
            page.click("#signin-button")
            sync_api.expect(page.locator("#pg-token")).to_be_visible()
            yield page
        finally:
            browser.close()


def test_playground_uploads_and_transcribes_audio(page, engine):
    page.fill("#pg-token", TOKEN)
    page.set_input_files("#pg-file", {"name": "speech.wav", "mimeType": "audio/wav", "buffer": audio(1)})
    page.click("#pg-send")
    sync_api.expect(page.locator("#pg-status")).to_contain_text("Complete")
    sync_api.expect(page.locator("#pg-status")).to_contain_text("HTTP 200")
    sync_api.expect(page.locator("#pg-output")).to_contain_text("hello world")
    assert engine.transcribe_calls == 1
    curl = page.locator("#pg-curl").text_content()
    assert "/api/transcribe" in curl and "--data-binary" in curl
    assert TOKEN not in curl and PASSWORD not in curl and "<SERVICE_TOKEN>" in curl
    assert TOKEN not in page.evaluate("JSON.stringify([Object.values(sessionStorage), Object.values(localStorage)])")
    page.click("#signout")
    assert page.locator("#pg-token").input_value() == ""


def test_playground_reports_rejected_audio(page):
    page.fill("#pg-token", TOKEN)
    page.set_input_files("#pg-file", {"name": "bad.wav", "mimeType": "audio/wav", "buffer": b"BAD"})
    page.click("#pg-send")
    sync_api.expect(page.locator("#pg-status")).to_contain_text("HTTP 400")
    sync_api.expect(page.locator("#pg-output")).to_contain_text("audio_rejected")
