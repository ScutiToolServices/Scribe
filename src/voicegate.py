"""Main-voice gate: silence every speech segment that does not sound like the enrolled voice, before the PCM goes to the
speech model. Silenced (not cut) so word timestamps stay aligned. Needs numpy; the embedder is injected, so the logic runs
and tests without a model. Standalone on purpose: Converse can import this file as it is."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Protocol

import numpy as np

RATE = 16000
FRAME = RATE // 50          # 20 ms
GAP_FRAMES = 15             # 0.3 s of quiet ends a segment
MAX_SEGMENT_S = 3.0         # longer speech is judged in pieces
MIN_JUDGE_S = 1.0           # shorter pieces are too unreliable to judge (own voice scored 0.13-0.52 at 0.4-0.7 s in noise): kept


class Embedder(Protocol):
    def __call__(self, samples: np.ndarray) -> np.ndarray:
        """float32 mono 16 kHz in [-1, 1] -> voiceprint vector."""


def _unit(vector: np.ndarray) -> np.ndarray:
    return vector / (np.linalg.norm(vector) or 1.0)


def enroll(embed: Embedder, pcm: bytes) -> np.ndarray:
    """The voiceprint of clean speech of the main voice (10-30 s)."""
    return _unit(embed(np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768))


def save_voiceprint(path: Path, voiceprint: np.ndarray) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    np.save(path, voiceprint)


def load_voiceprint(path: Path) -> np.ndarray | None:
    try:
        return _unit(np.load(path))
    except (OSError, ValueError):
        return None


def _segments(samples: np.ndarray, floor: float) -> list[tuple[int, int]]:
    """[start, end) sample ranges of speech: frames above `floor` rms, gaps under 0.3 s bridged, long runs split."""
    frames = len(samples) // FRAME
    loud = np.sqrt((samples[: frames * FRAME].reshape(frames, FRAME) ** 2).mean(axis=1)) > floor
    out, start, quiet = [], None, 0
    for i, is_loud in enumerate(loud):
        if is_loud:
            start, quiet = (i if start is None else start), 0
        elif start is not None:
            quiet += 1
            if quiet >= GAP_FRAMES:
                out.append((start * FRAME, (i - quiet + 1) * FRAME))
                start, quiet = None, 0
    if start is not None:
        out.append((start * FRAME, frames * FRAME))
    step = int(MAX_SEGMENT_S * RATE)
    return [(a, min(a + step, b)) for a, b in out for a in range(a, b, step)]


def noise_floor(samples: np.ndarray, minimum: float = 0.01) -> float:
    """1.5x the quiet end (10th percentile) of the 20 ms frame levels: a fixed floor treats steady background noise as
    speech and finds no pauses (measured on a noisy recording: noise sat at rms 0.03, the old floor was 0.01)."""
    frames = len(samples) // FRAME
    if frames < 1:
        return minimum
    levels = np.sqrt((samples[: frames * FRAME].reshape(frames, FRAME) ** 2).mean(axis=1))
    return max(minimum, 1.5 * float(np.percentile(levels, 10)))


def drop_ranges(samples: np.ndarray, voiceprint: np.ndarray, embed: Embedder, threshold: float = 0.4,
                floor: float | None = None) -> list[tuple[int, int]]:
    """[start, end) sample ranges (16 kHz) that are not the voiceprint's voice: judged segments under `threshold`
    cosine similarity. Empty when nothing is dropped or when every judged segment fails: an empty transcript is worse
    than a noisy one (shortcut: a wrong voiceprint is not detected here, upgrade by surfacing a 'no match' note)."""
    drop, kept = [], 0
    for a, b in _segments(samples, noise_floor(samples) if floor is None else floor):
        if (b - a) < MIN_JUDGE_S * RATE or float(_unit(embed(samples[a:b])) @ voiceprint) >= threshold:
            kept += 1
        else:
            drop.append((a, b))
    return drop if kept else []


def dominant_voiceprint(samples: np.ndarray, embed: Embedder, floor: float | None = None, match: float = 0.3) -> np.ndarray | None:
    """The voice that speaks most in this recording, for a clip with no enrolled voice (a voice-clone reference): the
    judged segment that the most speech time agrees with (cosine >= `match`), averaged over that supporting speech.
    Counting time, not segments, so a tight cluster of two clean segments cannot beat a longer, noisier speaker (measured
    on a noisy recording against a text-to-speech voice: a noisy speaker's own segments agree at only 0.3-0.5, a clean
    voice's at about 0.7, hence a match lower than the keep threshold). None when there is no segment long enough to judge."""
    spans = [(a, b) for a, b in _segments(samples, noise_floor(samples) if floor is None else floor) if (b - a) >= MIN_JUDGE_S * RATE]
    if not spans:
        return None
    stack = np.stack([_unit(embed(samples[a:b])) for a, b in spans])
    seconds = np.array([b - a for a, b in spans], dtype=np.float32)
    support = ((stack @ stack.T) >= match) @ seconds
    best = int(np.argmax(support))
    agree = (stack @ stack[best]) >= match
    return _unit((stack[agree] * seconds[agree, None]).sum(axis=0))


def gate(pcm: bytes, voiceprint: np.ndarray, embed: Embedder, threshold: float = 0.4, floor: float | None = None) -> bytes:
    """PCM with other voices silenced (see `drop_ranges`)."""
    if len(pcm) < 2:
        return pcm
    samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768
    drop = drop_ranges(samples, voiceprint, embed, threshold, floor)
    if not drop:
        return pcm
    out = bytearray(pcm)
    for a, b in drop:
        out[a * 2 : b * 2] = bytes(2 * (b - a))
    return bytes(out)


def _mel_bank(bins: int = 80, nfft: int = 512, low: float = 20.0) -> np.ndarray:
    mel = lambda hz: 1127.0 * np.log1p(hz / 700.0)
    points = np.linspace(mel(low), mel(RATE / 2), bins + 2)
    hz = 700.0 * np.expm1(points / 1127.0)
    edges = hz / (RATE / 2) * (nfft // 2)
    x = np.arange(nfft // 2 + 1)
    bank = np.zeros((bins, nfft // 2 + 1), dtype=np.float32)
    for i in range(bins):
        a, b, c = edges[i], edges[i + 1], edges[i + 2]
        bank[i] = np.maximum(0, np.minimum((x - a) / (b - a), (c - x) / (c - b)))
    return bank


_BANK = _mel_bank()
_WINDOW = (0.5 - 0.5 * np.cos(2 * np.pi * np.arange(400) / 399)) ** 0.85  # kaldi "povey"


def fbank(samples: np.ndarray) -> np.ndarray:
    """Kaldi-style 80-bin log-mel features (25 ms window, 10 ms shift), the input WeSpeaker models expect."""
    x = samples * 32768
    count = 1 + (len(x) - 400) // 160
    if count < 1:
        raise ValueError("audio too short")
    frames = np.lib.stride_tricks.sliding_window_view(x, 400)[::160][:count].copy()
    frames -= frames.mean(axis=1, keepdims=True)
    frames[:, 1:] -= 0.97 * frames[:, :-1]
    power = np.abs(np.fft.rfft(frames * _WINDOW, n=512)) ** 2
    return np.log(np.maximum(power @ _BANK.T, np.finfo(np.float32).eps)).astype(np.float32)


class OnnxEmbedder:
    """A WeSpeaker-style speaker-embedding ONNX model from a local file (80-bin fbank in, vector out). Nothing is
    downloaded here; put the file in the data folder yourself."""

    def __init__(self, model_path: Path) -> None:
        import onnxruntime as ort  # optional extra "voice"
        self._session = ort.InferenceSession(str(model_path), providers=["CPUExecutionProvider"])
        self._input = self._session.get_inputs()[0].name

    def __call__(self, samples: np.ndarray) -> np.ndarray:
        feats = fbank(samples)
        feats = feats - feats.mean(axis=0)  # cepstral mean normalisation
        return self._session.run(None, {self._input: feats[None]})[0][0]


class VoicePrefilter:
    """`bytes -> bytes` callable for RoutedEngine. `tag` changes when the voiceprint file does, so cached transcripts
    made with another voiceprint are not reused."""

    def __init__(self, embed: Embedder, voiceprint: np.ndarray, threshold: float, tag: str) -> None:
        self._embed, self._voiceprint, self._threshold, self.tag = embed, voiceprint, threshold, tag

    def __call__(self, pcm: bytes) -> bytes:
        return gate(pcm, self._voiceprint, self._embed, self._threshold)


def build_prefilter(voice_dir: Path, threshold: float, _cache: dict = {}) -> VoicePrefilter | None:
    """None (gate off) unless voice_dir holds speaker.onnx and main.npy and the "voice" extra is installed. The ONNX
    session is loaded once per model file."""
    model, voiceprint = voice_dir / "speaker.onnx", voice_dir / "main.npy"
    try:
        stamp = (model.stat().st_mtime_ns, voiceprint.stat().st_mtime_ns)
        if _cache.get("stamp") != stamp:
            _cache.clear()
            _cache.update(stamp=stamp, embed=OnnxEmbedder(model), voiceprint=load_voiceprint(voiceprint))
        if _cache["voiceprint"] is None:
            return None
        return VoicePrefilter(_cache["embed"], _cache["voiceprint"], threshold, f"voice{stamp[1]}")
    except (OSError, ImportError):
        return None


def build_embedder(model: Path, _cache: dict = {}) -> OnnxEmbedder | None:
    """The speaker model from a local file, loaded once per file version. None when the file or the "voice" extra
    (numpy, onnxruntime) is missing."""
    try:
        stamp = model.stat().st_mtime_ns
        if _cache.get("stamp") != stamp:
            _cache.clear()
            _cache.update(stamp=stamp, embed=OnnxEmbedder(model))
        return _cache["embed"]
    except (OSError, ImportError):
        return None


def speech_seconds(samples: np.ndarray, floor: float | None = None) -> float:
    """Seconds of speech (above the noise floor) in 16 kHz samples."""
    return sum(b - a for a, b in _segments(samples, noise_floor(samples) if floor is None else floor)) / RATE
