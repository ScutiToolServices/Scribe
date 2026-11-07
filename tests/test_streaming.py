from dataclasses import replace
import asyncio
import threading
import time

import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from src.app import build_app
from src.engine import Segment, Transcript
from src.errors import AudioRejected, EngineFailed
from src.service import ScribeService
from src.store import AudioStore
from tests.conftest import FakeEngine, audio
from tests.seed import seed_token

ANN = {"X-Requester-Username": "ann"}


async def test_local_agreement_commits_prefix_and_trims_pcm():
    from src.streaming import LocalAgreement
    stream = LocalAgreement(max_seconds=10, window_seconds=4)
    stream.append(audio(2))
    stream.update(Transcript("hello there", [Segment(0, 1, "hello"), Segment(1, 2, "there")], "en"))
    stream.append(audio(1))
    assert stream.update(Transcript("hello world", [Segment(0, 1, "hello"), Segment(1, 3, "world")], "en")) == "hello world"
    assert len(stream.pcm) == 64000
    assert stream.finish(Transcript("world again", [], "en")) == "hello world again"


def test_websocket_requires_token_and_identity_before_accept(settings):
    client = TestClient(build_app(seed_token(replace(settings, admin_password="pw"), "secret"), FakeEngine()))
    for headers in [ANN, {"X-Internal-Token": "secret"}]:
        with pytest.raises(WebSocketDisconnect) as exc:
            with client.websocket_connect("/api/stream", headers=dict(headers)):
                pytest.fail("unauthorized connection accepted")
        assert exc.value.code == 1008


def test_websocket_partial_then_final(settings):
    client = TestClient(build_app(settings, FakeEngine()))
    with client.websocket_connect("/api/stream", headers=dict(ANN)) as ws:
        ws.send_bytes(audio())
        assert ws.receive_json() == {"type": "partial", "text": "hello world"}
        ws.send_json({"type": "stop"})
        assert ws.receive_json() == {"type": "final", "text": "hello world"}


def test_websocket_capacity_and_frame_size(settings):
    client = TestClient(build_app(replace(settings, stream_capacity=1, stream_max_frame_bytes=100), FakeEngine()))
    with client.websocket_connect("/api/stream", headers=dict(ANN)) as first:
        with pytest.raises(WebSocketDisconnect) as exc:
            with client.websocket_connect("/api/stream", headers=dict(ANN)) as second:
                second.receive_json()
        assert exc.value.code == 1013
        assert client.post("/api/transcribe-partial", content=audio(), headers=ANN).status_code == 200
        first.send_bytes(audio())
        assert first.receive_json()["type"] == "error"
        with pytest.raises(WebSocketDisconnect) as exc:
            first.receive_json()
        assert exc.value.code == 1009


def test_partial_route_is_ephemeral_and_validates(settings):
    client = TestClient(build_app(seed_token(replace(settings, admin_password="pw"), "secret"), FakeEngine()))
    headers = {**ANN, "X-Internal-Token": "secret"}
    assert client.post("/api/transcribe-partial", content=audio(), headers=ANN).status_code == 401
    assert client.post("/api/transcribe-partial", content=audio(), headers={"X-Internal-Token": "secret"}).status_code == 401
    response = client.post("/api/transcribe-partial", content=audio(), headers=headers)
    assert response.status_code == 200
    assert response.json() == {"text": "hello world", "duration_s": 1.0}
    assert not list(settings.data_dir.rglob("*.bin"))
    assert not list(settings.staging_dir.iterdir())
    assert client.post("/api/transcribe-partial", content=audio(bad=True), headers=headers).status_code == 400
    assert client.post("/api/transcribe-partial", content=audio(11), headers=headers).status_code == 400


def test_partial_route_byte_and_quota_limits(settings):
    for changes in [{"max_bytes": 10}, {"quota_bytes": 10}]:
        client = TestClient(build_app(replace(settings, **changes), FakeEngine()))
        assert client.post("/api/transcribe-partial", content=audio(), headers=ANN).status_code == 413


def test_stream_total_bytes_and_invalid_commands(settings):
    client = TestClient(build_app(replace(settings, max_bytes=100), FakeEngine()))
    with client.websocket_connect("/api/stream", headers=dict(ANN)) as ws:
        ws.send_bytes(audio(0.1))
        assert ws.receive_json()["type"] == "error"
    for command in ["bad json", "[]", '{"type":"unknown"}']:
        with client.websocket_connect("/api/stream", headers=dict(ANN)) as ws:
            ws.send_text(command)
            assert ws.receive_json()["type"] == "error"


def test_stream_idle_timeout_and_sanitized_engine_error(settings):
    client = TestClient(build_app(replace(settings, stream_idle_s=0.02), FakeEngine()))
    with client.websocket_connect("/api/stream", headers=dict(ANN)) as ws:
        assert ws.receive_json()["type"] == "error"
    class BrokenEngine(FakeEngine):
        async def transcribe(self, pcm):
            raise RuntimeError("C:/private/model secret-token")
    client = TestClient(build_app(settings, BrokenEngine()))
    with client.websocket_connect("/api/stream", headers=dict(ANN)) as ws:
        ws.send_bytes(audio())
        error = ws.receive_json()
        assert error["type"] == "error"
        assert "private" not in str(error) and "secret-token" not in str(error)
    response = client.post("/api/transcribe-partial", content=audio(), headers=ANN)
    assert response.status_code == 503
    assert "private" not in response.text


def test_bounded_window_and_audio_duration():
    from src.streaming import LocalAgreement
    stream = LocalAgreement(max_seconds=3, window_seconds=2)
    stream.append(audio(2))
    with pytest.raises(AudioRejected):
        stream.append(audio(0.1))
    stream = LocalAgreement(max_seconds=1, window_seconds=3)
    with pytest.raises(AudioRejected):
        stream.append(audio(2))
    with pytest.raises(AudioRejected):
        stream.append(b"x")


async def test_timeout_keeps_thread_inference_slot(settings):
    release = threading.Event()
    entered = threading.Event()
    class ThreadEngine(FakeEngine):
        async def transcribe(self, pcm):
            def work():
                entered.set()
                release.wait(2)
                return Transcript("finished", [], "en")
            self.transcribe_calls += 1
            return await asyncio.to_thread(work)
    engine = ThreadEngine()
    service = ScribeService(engine, AudioStore(settings.audio_dir, 100, 1000000), replace(settings, timeout_s=0.03))
    try:
        with pytest.raises(EngineFailed):
            await service.infer(audio())
        assert entered.is_set()
        with pytest.raises(EngineFailed):
            await service.infer(audio())
        assert engine.transcribe_calls == 1
    finally:
        release.set()
    await asyncio.sleep(0.02)
    assert (await service.infer(audio())).text == "finished"


async def test_upload_honors_active_stream_quota(settings):
    from src.errors import QuotaExceeded
    settings = replace(settings, quota_bytes=50000)
    service = ScribeService(FakeEngine(), AudioStore(settings.audio_dir, 100, 50000), settings)
    key = object()
    await service.reserve(key, "ann", 32000)
    with pytest.raises(QuotaExceeded):
        await service.upload("ann", "audio.wav", audio())
    service.unreserve(key)
    assert (await service.upload("ann", "audio.wav", audio()))[0].size_bytes == 32000


def test_partial_reserves_decoded_pcm_quota(settings):
    class CompressedEngine(FakeEngine):
        async def decode(self, path):
            return audio()
    client = TestClient(build_app(replace(settings, quota_bytes=100), CompressedEngine()))
    assert client.post("/api/transcribe-partial", content=b"compressed", headers=ANN).status_code == 413


def test_stop_allows_final_inference_after_recording_duration(settings):
    client = TestClient(build_app(replace(settings, max_seconds=0.05), FakeEngine()))
    with client.websocket_connect("/api/stream", headers=dict(ANN)) as ws:
        ws.send_bytes(audio(0.04))
        time.sleep(0.08)
        ws.send_json({"type": "stop"})
        assert ws.receive_json() == {"type": "final", "text": "hello world"}


def test_audio_is_read_while_a_pass_is_running(settings):
    release, finished = threading.Event(), threading.Event()
    class SlowEngine(FakeEngine):
        async def transcribe(self, pcm):
            await asyncio.to_thread(release.wait, 5)
            finished.set()
            return Transcript("hello world", [], "en")
    client = TestClient(build_app(replace(settings, stream_max_frame_bytes=32000), SlowEngine()))
    try:
        with client.websocket_connect("/api/stream", headers=dict(ANN)) as ws:
            ws.send_bytes(audio())  # starts a pass that stays blocked in the engine
            ws.send_bytes(audio(2))  # too large: only answered if the loop still reads while the pass runs
            assert ws.receive_json() == {"type": "error", "message": "Audio frame is too large."}
            assert not finished.is_set()  # the engine call is still blocked
    finally:
        release.set()
