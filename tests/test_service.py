import asyncio

import pytest

from src.errors import AudioRejected, EngineFailed, FileNotFound, IdentityRequired
from src.service import ScribeService
from src.store import AudioStore
from tests.conftest import FakeEngine, audio


def make(settings, engine):
    store = AudioStore(settings.audio_dir, settings.ttl_seconds, settings.quota_bytes)
    return ScribeService(engine, store, settings)


async def test_upload_returns_duration_and_id(settings, engine):
    service = make(settings, engine)
    stored, expires_at = await service.upload("ann", "a.wav", audio(2.0))
    assert stored.duration_s == pytest.approx(2.0) and expires_at > stored.created_at


async def test_upload_needs_identity(settings, engine):
    with pytest.raises(IdentityRequired):
        await make(settings, engine).upload("", "a.wav", audio())


async def test_upload_rejects_unreadable_audio(settings, engine):
    with pytest.raises(AudioRejected):
        await make(settings, engine).upload("ann", "a.wav", audio(bad=True))


async def test_upload_rejects_audio_over_the_duration_cap(settings, engine):
    with pytest.raises(AudioRejected, match="longer"):
        await make(settings, engine).upload("ann", "a.wav", audio(11.0))


async def test_transcribe_then_cache_hit(settings, engine):
    service = make(settings, engine)
    stored, _ = await service.upload("ann", "a.wav", audio())
    first = await service.transcribe("ann", stored.file_id, timestamps=False)
    second = await service.transcribe("ann", stored.file_id, timestamps=True)
    assert first.text == "hello world" and first.cached is False and first.segments is None
    assert second.cached is True and [s.text for s in second.segments] == ["hello", "world"]
    assert engine.transcribe_calls == 1


async def test_unknown_or_foreign_file_is_not_found(settings, engine):
    service = make(settings, engine)
    stored, _ = await service.upload("ann", "a.wav", audio())
    with pytest.raises(FileNotFound):
        await service.transcribe("bob", stored.file_id, timestamps=False)
    with pytest.raises(FileNotFound):
        await service.transcribe("ann", "0" * 32, timestamps=False)


async def test_concurrent_identical_requests_transcribe_once(settings):
    engine = FakeEngine(delay=0.05)
    service = make(settings, engine)
    stored, _ = await service.upload("ann", "a.wav", audio())
    results = await asyncio.gather(*(service.transcribe("ann", stored.file_id, False) for _ in range(3)))
    assert engine.transcribe_calls == 1 and {r.text for r in results} == {"hello world"}


async def test_engine_timeout_is_engine_failed(settings):
    engine = FakeEngine(delay=5)
    service = make(settings, engine)
    stored, _ = await service.upload("ann", "a.wav", audio())
    with pytest.raises(EngineFailed):
        await service.transcribe("ann", stored.file_id, False)  # settings.timeout_s is 2


async def test_engine_failure_is_not_cached(settings):
    engine = FakeEngine(fail=True)
    service = make(settings, engine)
    stored, _ = await service.upload("ann", "a.wav", audio())
    with pytest.raises(EngineFailed):
        await service.transcribe("ann", stored.file_id, False)
    engine.fail = False
    assert (await service.transcribe("ann", stored.file_id, False)).cached is False


class FakeCleaner:
    def __init__(self, connected=True, fail=False):
        self.connected, self.fail, self.seen = connected, fail, []

    async def clean(self, text, cleanup):
        self.seen.append((text, cleanup))
        if self.fail:
            raise EngineFailed("Cleanup failed.")
        return "Hello, world."


async def cleaned(settings, engine, cleaner, cleanup):
    service = ScribeService(engine, AudioStore(settings.audio_dir, lambda: 86400, lambda: 10**9), settings, cleaner)
    stored, _ = await service.upload("ann", "a.wav", audio())
    return await service.transcribe("ann", stored.file_id, False, cleanup)


async def test_cleanup_replaces_the_text_but_the_cache_keeps_the_raw_transcript(settings, engine):
    from src.engine import Cleanup
    cleaner = FakeCleaner()
    result = await cleaned(settings, engine, cleaner, Cleanup("formal"))
    assert (result.text, result.cleaned) == ("Hello, world.", True)
    assert cleaner.seen == [("hello world", Cleanup("formal"))]


async def test_no_cleanup_request_never_calls_the_cleaner(settings, engine):
    cleaner = FakeCleaner()
    result = await cleaned(settings, engine, cleaner, None)
    assert (result.text, result.cleaned, cleaner.seen) == ("hello world", False, [])


@pytest.mark.parametrize("cleaner, note", [(FakeCleaner(connected=False), "no cleanup service"), (FakeCleaner(fail=True), "Cleanup failed")])
async def test_cleanup_that_cannot_run_returns_the_raw_transcript_and_says_so(settings, engine, cleaner, note):
    from src.engine import Cleanup
    result = await cleaned(settings, engine, cleaner, Cleanup())
    assert (result.text, result.cleaned) == ("hello world", False) and note in result.message
