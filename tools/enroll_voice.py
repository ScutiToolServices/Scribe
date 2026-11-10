"""Enroll the main voice: python tools/enroll_voice.py clean_speech.wav  (10-30 s of one person talking, no one else).

Needs `uv sync --extra voice` and the speaker model at <data folder>/voice/speaker.onnx. Writes voice/main.npy.
Then turn on "Keep only the main voice" on the admin page (or SERVICE_VOICE_GATE=1)."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import load_settings  # noqa: E402
from src.engine import PCM_BYTES_PER_SECOND, Decoder  # noqa: E402
from src.voicegate import OnnxEmbedder, enroll, save_voiceprint  # noqa: E402


async def main(audio: Path) -> None:
    settings = load_settings()
    model = settings.voice_dir / "speaker.onnx"
    if not model.is_file():
        sys.exit(f"Put the WeSpeaker-style ONNX speaker model at {model} first.")
    pcm = await Decoder(settings.ffmpeg, max_seconds=60).decode(audio)
    if len(pcm) < 5 * PCM_BYTES_PER_SECOND:
        sys.exit("Need at least 5 seconds of speech; 10-30 s is better.")
    voiceprint = await asyncio.to_thread(lambda: enroll(OnnxEmbedder(model), pcm))
    save_voiceprint(settings.voice_dir / "main.npy", voiceprint)
    print(f"Saved {settings.voice_dir / 'main.npy'} from {len(pcm) / PCM_BYTES_PER_SECOND:.0f} s of audio.")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    asyncio.run(main(Path(sys.argv[1])))
