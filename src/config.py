"""Scribe settings, read from the environment (and a local .env, never committed)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_DIR = Path(__file__).resolve().parent.parent
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


@dataclass(frozen=True)
class Settings:
    host: str = "127.0.0.1"
    port: int = 8091
    internal_token: str = ""
    data_dir: Path = PROJECT_DIR / ".data"
    # base.en and 150 s: measured x1.6 real time on a 6-core CPU, and Armory cancels a tool call after 120 s.
    # small.en was x0.5 there. Raise both together on a faster or GPU host.
    model: str = "base.en"
    ffmpeg: str = "ffmpeg"
    # Empty = run faster-whisper inside Scribe. Set = call the STTWhisperModel service (SCRIBE_MODEL must match its model).
    whisper_url: str = ""
    whisper_token: str = ""
    max_bytes: int = 25 * 1024 * 1024
    max_seconds: int = 150
    ttl_seconds: int = 24 * 3600
    quota_bytes: int = 200 * 1024 * 1024
    concurrency: int = 1
    timeout_s: int = 110
    stream_capacity: int = 2
    stream_max_frame_bytes: int = 64000
    stream_idle_s: float = 15
    stream_window_s: float = 30
    # Unlocks /admin/ (settings page). Empty = the page is off and settings stay as the environment set them.
    admin_password: str = ""

    @property
    def audio_dir(self) -> Path:
        return self.data_dir / "audio"

    @property
    def models_dir(self) -> Path:
        return self.data_dir / "models"

    @property
    def staging_dir(self) -> Path:
        return self.data_dir / "staging"

    @property
    def exposed_without_token(self) -> bool:
        """Listening beyond loopback with no token lets anything that can reach the port call every tool."""
        return not self.internal_token and self.host not in LOOPBACK_HOSTS


def load_settings() -> Settings:
    load_dotenv(PROJECT_DIR / ".env")
    env = os.environ.get
    return Settings(
        host=env("SCRIBE_HOST", "127.0.0.1"),
        port=int(env("SCRIBE_PORT", "8091")),
        internal_token=env("INTERNAL_API_TOKEN", ""),
        data_dir=Path(env("SCRIBE_DATA_DIR") or PROJECT_DIR / ".data"),
        model=env("SCRIBE_MODEL", "base.en"),
        ffmpeg=env("SCRIBE_FFMPEG", "ffmpeg"),
        whisper_url=env("SCRIBE_WHISPER_URL", ""),
        whisper_token=env("SCRIBE_WHISPER_TOKEN", ""),
        max_bytes=int(env("SCRIBE_MAX_BYTES", str(25 * 1024 * 1024))),
        max_seconds=int(env("SCRIBE_MAX_SECONDS", "150")),
        ttl_seconds=int(env("SCRIBE_TTL_HOURS", "24")) * 3600,
        quota_bytes=int(env("SCRIBE_QUOTA_MB", "200")) * 1024 * 1024,
        concurrency=int(env("SCRIBE_CONCURRENCY", "1")),
        timeout_s=int(env("SCRIBE_TIMEOUT_S", "110")),
        stream_capacity=int(env("SCRIBE_STREAM_CAPACITY", "2")),
        stream_max_frame_bytes=int(env("SCRIBE_STREAM_MAX_FRAME_BYTES", "64000")),
        stream_idle_s=float(env("SCRIBE_STREAM_IDLE_S", "15")),
        stream_window_s=float(env("SCRIBE_STREAM_WINDOW_S", "30")),
        admin_password=env("SCRIBE_ADMIN_PASSWORD", ""),
    )
