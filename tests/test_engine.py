import json
import shutil
from pathlib import Path
import subprocess
import asyncio

import pytest

import httpx

from src.engine import PCM_BYTES_PER_SECOND, Cleaner, Cleanup, Decoder, RoutedEngine, probe_s1mini, probe_whisper
from src.errors import AudioRejected, EngineFailed

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not on PATH")


@needs_ffmpeg
async def test_decode_a_generated_tone(tmp_path):
    wav = tmp_path / "tone.wav"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=1", str(wav)], check=True
    )
    pcm = await Decoder("ffmpeg").decode(wav)
    assert abs(len(pcm) / PCM_BYTES_PER_SECOND - 1.0) < 0.1


@needs_ffmpeg
async def test_decode_rejects_garbage(tmp_path):
    junk = tmp_path / "junk.bin"
    junk.write_bytes(b"not audio at all")
    with pytest.raises(AudioRejected):
        await Decoder("ffmpeg").decode(junk)


async def test_decode_bounds_ffmpeg_output_and_rejects_long_audio(tmp_path, monkeypatch):
    class Process:
        returncode = 0
        async def communicate(self):
            return b"\0\0" * 48000, None
    async def spawn(*args, **kwargs):
        assert args[args.index("-t") + 1] == "3"
        assert kwargs["stderr"] == asyncio.subprocess.DEVNULL
        return Process()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    with pytest.raises(AudioRejected):
        await Decoder("ffmpeg", max_seconds=2).decode(tmp_path / "audio")


@pytest.mark.parametrize("cancel", [False, True])
async def test_decode_timeout_and_cancel_kill_and_reap_process(tmp_path, monkeypatch, cancel):
    class Process:
        returncode = None
        def __init__(self):
            self.started = asyncio.Event()
            self.killed = asyncio.Event()
            self.reaped = False
        async def communicate(self):
            self.started.set()
            await self.killed.wait()
            self.reaped = True
            self.returncode = -9
            return b"", None
        def kill(self):
            self.killed.set()
    process = Process()
    async def spawn(*args, **kwargs):
        return process
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    task = asyncio.create_task(Decoder("ffmpeg", decode_timeout_s=0.02).decode(tmp_path / "audio"))
    await process.started.wait()
    if cancel:
        task.cancel()
    with pytest.raises(asyncio.CancelledError if cancel else EngineFailed):
        await task
    assert process.killed.is_set() and process.reaped


def routed(url, token):
    return RoutedEngine(Decoder("ffmpeg"), "base.en", lambda: (url, token))


REAL_CLIENT = httpx.AsyncClient  # captured at import: remote() may run twice in one test


def remote(monkeypatch, handler):
    """RoutedEngine's per-call client, answered by `handler` instead of the network."""
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: REAL_CLIENT(transport=httpx.MockTransport(handler), **kwargs))


async def test_routed_engine_posts_pcm_with_the_token_and_maps_the_reply(monkeypatch):
    seen = {}

    def handler(request):
        seen.update(url=str(request.url), token=request.headers["X-Internal-Token"], size=len(request.content))
        return httpx.Response(200, json={
            "text": "hello world", "language": "en", "duration_s": 1.0, "model": "base.en",
            "segments": [{"start_s": 0.0, "end_s": 1.0, "text": "hello world"}],
            "words": [{"start_s": 0.0, "end_s": 0.5, "text": "hello"}, {"start_s": 0.5, "end_s": 1.0, "text": "world"}],
        })

    remote(monkeypatch, handler)
    engine = routed("http://whisper:29300", "whisper_x")
    transcript = await engine.transcribe(b"\0\0" * 10)
    assert seen == {"url": "http://whisper:29300/v1/transcribe?language=en&beam_size=1&word_timestamps=true",
                    "token": "whisper_x", "size": 20}
    assert transcript.text == "hello world" and transcript.language == "en" and engine.model_name == "base.en"
    assert [(w.start_s, w.end_s, w.text) for w in transcript.words] == [(0.0, 0.5, "hello"), (0.5, 1.0, "world")]
    assert transcript.segments[0].text == "hello world"


@pytest.mark.parametrize("answer", [
    httpx.Response(429, json={"detail": "busy"}), httpx.Response(500, text="boom at /secret/path"),
    httpx.Response(200, text="not json"), httpx.Response(200, json={"text": "no segments"}),
])
async def test_routed_engine_failures_are_one_safe_message(monkeypatch, answer):
    remote(monkeypatch, lambda request: answer)
    with pytest.raises(EngineFailed) as raised:
        await routed("http://whisper:29300", "").transcribe(b"\0\0")
    assert str(raised.value) == "Transcription failed. Try again." and "secret" not in str(raised.value)


async def test_routed_engine_unreachable_service_is_engine_failed(monkeypatch):
    def refuse(request):
        raise httpx.ConnectError("connection refused to http://whisper:29300")

    remote(monkeypatch, refuse)
    with pytest.raises(EngineFailed):
        await routed("http://whisper:29300", "").transcribe(b"\0\0")


async def test_the_slot_is_read_on_every_call_and_empty_means_not_connected(monkeypatch):
    slot = {"now": None}
    engine = RoutedEngine(Decoder("ffmpeg"), "small.en", lambda: slot["now"])
    assert engine.model_name == "small.en" and not engine.is_remote
    with pytest.raises(EngineFailed, match="admin page"):
        await engine.transcribe(b"\0\0")
    remote(monkeypatch, lambda request: httpx.Response(200, json={"text": "far", "language": "en", "segments": [], "words": []}))
    slot["now"] = ("http://whisper:29300", "t")
    assert engine.is_remote and (await engine.transcribe(b"\0\0")).text == "far"
    slot["now"] = None
    with pytest.raises(EngineFailed):
        await engine.transcribe(b"\0\0")


async def test_probe_whisper_checks_health_then_sends_the_token_with_a_little_silence(monkeypatch):
    seen = []

    def handler(request):
        seen.append((request.method, request.url.path, request.headers.get("X-Internal-Token"), len(request.content)))
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        return httpx.Response(200, json={"text": "", "model": "base.en", "segments": [], "words": []})

    remote(monkeypatch, handler)
    assert await probe_whisper("http://w:29300", "tok") == "Connected, token accepted, model base.en."
    assert seen == [("GET", "/health", None, 0), ("POST", "/v1/transcribe", "tok", 3200)]
    remote(monkeypatch, lambda request: httpx.Response(200, json={"status": "ok"}))
    with pytest.raises(Exception, match="not a STT_Whisper_Model service"):
        await probe_whisper("http://w:29300", "tok")


async def test_cleaner_posts_the_control_values_and_returns_content(monkeypatch):
    seen = {}

    def handler(request):
        seen.update(url=str(request.url), token=request.headers["X-Internal-Token"], body=request.content)
        return httpx.Response(200, json={"content": "Hello, world.", "model": "s1-mini"})

    remote(monkeypatch, handler)
    cleaner = Cleaner(lambda: ("http://s1:29280", "s1mini_x"))
    assert cleaner.connected
    assert await cleaner.clean("um hello world", Cleanup("formal", "lists", "email")) == "Hello, world."
    assert seen["url"] == "http://s1:29280/v1/normalize" and seen["token"] == "s1mini_x"
    assert json.loads(seen["body"]) == {"text": "um hello world", "styling": "formal", "structure": "lists", "context": "email"}


async def test_cleaner_without_an_address_or_text_leaves_the_text_alone():
    assert not Cleaner(lambda: None).connected
    assert await Cleaner(lambda: None).clean("um hi", Cleanup()) == "um hi"
    assert await Cleaner(lambda: ("http://s1:29280", "")).clean("  ", Cleanup()) == "  "


async def test_cleaner_failure_is_one_safe_message(monkeypatch):
    remote(monkeypatch, lambda request: httpx.Response(500, text="boom at /secret/path"))
    with pytest.raises(EngineFailed) as raised:
        await Cleaner(lambda: ("http://s1:29280", "")).clean("hi", Cleanup())
    assert str(raised.value) == "Cleanup failed." and "secret" not in str(raised.value)


async def test_probe_s1mini_checks_health_then_sends_the_token(monkeypatch):
    remote(monkeypatch, lambda request: httpx.Response(200, json={"status": "ok", "content": "Ok.", "model": "s1-mini"}))
    assert await probe_s1mini("http://s1:29280", "tok") == "Connected, token accepted, model s1-mini."
    remote(monkeypatch, lambda request: httpx.Response(200, json={"status": "ok"}))
    with pytest.raises(Exception, match="not a STC_S1Mini_Model service"):
        await probe_s1mini("http://s1:29280", "tok")
