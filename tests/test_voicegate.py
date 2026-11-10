import pytest

np = pytest.importorskip("numpy")  # optional extra "voice"

from src.voicegate import RATE, enroll, gate  # noqa: E402


def tone(hz: float, seconds: float, amp: float = 0.3) -> np.ndarray:
    t = np.arange(int(seconds * RATE)) / RATE
    return (amp * np.sin(2 * np.pi * hz * t)).astype(np.float32)


def pcm_of(*parts: np.ndarray) -> bytes:
    return (np.concatenate(parts) * 32768).astype("<i2").tobytes()


def embed(samples: np.ndarray) -> np.ndarray:
    """Fake voiceprint: which of two pitches dominates the spectrum."""
    spectrum = np.abs(np.fft.rfft(samples))
    freqs = np.fft.rfftfreq(len(samples), 1 / RATE)
    return np.array([spectrum[abs(freqs - 200) < 20].sum(), spectrum[abs(freqs - 800) < 20].sum()])


def test_other_voice_is_silenced_and_length_kept():
    quiet = np.zeros(int(0.5 * RATE), dtype=np.float32)
    mine, other = tone(200, 1.5), tone(800, 1.5)
    pcm = pcm_of(mine, quiet, other, quiet, mine)
    voiceprint = enroll(embed, pcm_of(tone(200, 2)))
    out = gate(pcm, voiceprint, embed)
    assert len(out) == len(pcm)
    samples = np.frombuffer(out, dtype="<i2")
    a, b = int(2.0 * RATE), int(3.5 * RATE)
    assert not samples[a:b].any()
    assert samples[: int(1.5 * RATE)].any() and samples[int(4 * RATE):].any()


def test_nothing_matches_returns_input_unchanged():
    pcm = pcm_of(tone(800, 2))
    assert gate(pcm, enroll(embed, pcm_of(tone(200, 2))), embed) == pcm


def test_fbank_shape():
    from src.voicegate import fbank
    assert fbank(tone(200, 1.0)).shape == (98, 80)


async def test_engine_runs_prefilter_and_keys_cache_by_tag(monkeypatch):
    import src.engine as engine_module
    from src.engine import RoutedEngine, Transcript

    seen = []

    async def fake_remote(url, token, pcm, timeout_s):
        seen.append(pcm)
        return Transcript("hi", [], "en")

    class Filter:
        tag = "voice1"
        def __call__(self, pcm):
            return b"\0\0"

    monkeypatch.setattr(engine_module, "remote_transcribe", fake_remote)
    assert RoutedEngine(None, "base.en", lambda: ("http://x", "")).model_name == "base.en"
    engine = RoutedEngine(None, "base.en", lambda: ("http://x", ""), prefilter=lambda force=None: Filter())
    await engine.transcribe(b"\1\1\1\1")
    assert seen == [b"\0\0"] and engine.model_name == "base.en+voice1"


def test_dominant_voice_is_the_one_that_talks_most():
    from src.voicegate import dominant_voiceprint, drop_ranges
    quiet = np.zeros(int(0.5 * RATE), dtype=np.float32)
    samples = np.concatenate([tone(200, 2), quiet, tone(800, 1.2), quiet, tone(200, 2), quiet, tone(200, 1.5)])
    voiceprint = dominant_voiceprint(samples, embed)
    drop = drop_ranges(samples, voiceprint, embed)
    assert len(drop) == 1 and abs(drop[0][0] / RATE - 2.5) < 0.1
    assert dominant_voiceprint(np.zeros(RATE, dtype=np.float32), embed) is None


async def test_voice_gate_can_be_forced_per_call(monkeypatch):
    import src.engine as engine_module
    from src.engine import RoutedEngine, Transcript

    seen = []

    async def fake_remote(url, token, pcm, timeout_s):
        seen.append(pcm)
        return Transcript("hi", [], "en")

    class Filter:
        tag = "voice1"
        def __call__(self, pcm):
            return bytes(2)

    # setting is off: only an explicit True builds the filter
    engine = RoutedEngine(None, "base.en", lambda: ("http://x", ""), prefilter=lambda force=None: Filter() if force else None)
    monkeypatch.setattr(engine_module, "remote_transcribe", fake_remote)
    audio = bytes([1, 1, 1, 1])
    await engine.transcribe(audio)
    await engine.transcribe(audio, voice_gate=True)
    await engine.transcribe(audio, voice_gate=False)
    assert seen == [audio, bytes(2), audio]
    assert engine.name_for(None) == "base.en" and engine.name_for(True) == "base.en+voice1" and engine.name_for(False) == "base.en"
    assert engine.gate_active(True) and not engine.gate_active(False)


def test_voice_gate_query_flag():
    from src.app import flag
    assert [flag(v) for v in (None, "", "true", "1", "OFF", "false", "maybe")] == [None, None, True, True, False, False, "bad"]
