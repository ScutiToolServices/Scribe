import shutil
import subprocess
import asyncio

import pytest

import httpx

from src.engine import PCM_BYTES_PER_SECOND, HttpWhisperEngine, WhisperEngine
from src.errors import AudioRejected, EngineFailed

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not on PATH")


@needs_ffmpeg
async def test_decode_a_generated_tone(tmp_path):
    wav = tmp_path / "tone.wav"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=1", str(wav)], check=True
    )
    pcm = await WhisperEngine(tmp_path, "base.en", "ffmpeg").decode(wav)
    assert abs(len(pcm) / PCM_BYTES_PER_SECOND - 1.0) < 0.1


@needs_ffmpeg
async def test_decode_rejects_garbage(tmp_path):
    junk = tmp_path / "junk.bin"
    junk.write_bytes(b"not audio at all")
    with pytest.raises(AudioRejected):
        await WhisperEngine(tmp_path, "base.en", "ffmpeg").decode(junk)


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
        await WhisperEngine(tmp_path, "base.en", "ffmpeg", max_seconds=2).decode(tmp_path / "audio")


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
    task = asyncio.create_task(WhisperEngine(tmp_path, "base.en", "ffmpeg", decode_timeout_s=0.02).decode(tmp_path / "audio"))
    await process.started.wait()
    if cancel:
        task.cancel()
    with pytest.raises(asyncio.CancelledError if cancel else EngineFailed):
        await task
    assert process.killed.is_set() and process.reaped


def remote(monkeypatch, handler):
    """HttpWhisperEngine's per-call client, answered by `handler` instead of the network."""
    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: real(transport=httpx.MockTransport(handler), **kwargs))


async def test_http_engine_posts_pcm_with_the_token_and_maps_the_reply(monkeypatch):
    seen = {}

    def handler(request):
        seen.update(url=str(request.url), token=request.headers["X-Internal-Token"], size=len(request.content))
        return httpx.Response(200, json={
            "text": "hello world", "language": "en", "duration_s": 1.0, "model": "base.en",
            "segments": [{"start_s": 0.0, "end_s": 1.0, "text": "hello world"}],
            "words": [{"start_s": 0.0, "end_s": 0.5, "text": "hello"}, {"start_s": 0.5, "end_s": 1.0, "text": "world"}],
        })

    remote(monkeypatch, handler)
    engine = HttpWhisperEngine("http://whisper:9230/", "whisper_x", "base.en", "ffmpeg")
    transcript = await engine.transcribe(b"\0\0" * 10)
    assert seen == {"url": "http://whisper:9230/v1/transcribe?language=en&beam_size=1&word_timestamps=true",
                    "token": "whisper_x", "size": 20}
    assert transcript.text == "hello world" and transcript.language == "en" and engine.model_name == "base.en"
    assert [(w.start_s, w.end_s, w.text) for w in transcript.words] == [(0.0, 0.5, "hello"), (0.5, 1.0, "world")]
    assert transcript.segments[0].text == "hello world"


@pytest.mark.parametrize("answer", [
    httpx.Response(429, json={"detail": "busy"}), httpx.Response(500, text="boom at /secret/path"),
    httpx.Response(200, text="not json"), httpx.Response(200, json={"text": "no segments"}),
])
async def test_http_engine_failures_are_one_safe_message(monkeypatch, answer):
    remote(monkeypatch, lambda request: answer)
    with pytest.raises(EngineFailed) as raised:
        await HttpWhisperEngine("http://whisper:9230", "", "base.en", "ffmpeg").transcribe(b"\0\0")
    assert str(raised.value) == "Transcription failed. Try again." and "secret" not in str(raised.value)


async def test_http_engine_unreachable_service_is_engine_failed(monkeypatch):
    def refuse(request):
        raise httpx.ConnectError("connection refused to http://whisper:9230")

    remote(monkeypatch, refuse)
    with pytest.raises(EngineFailed):
        await HttpWhisperEngine("http://whisper:9230", "", "base.en", "ffmpeg").transcribe(b"\0\0")
