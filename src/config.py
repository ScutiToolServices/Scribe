"""Scribe settings, read from the environment (and a local .env, never committed)."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, replace
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parent.parent
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


@dataclass(frozen=True)
class Settings:
    host: str = "127.0.0.1"
    port: int = 29401
    internal_token: str = ""
    data_dir: Path = PROJECT_DIR / ".data"
    config_dir: Path | None = None  # set when the data folder was moved: the folder that keeps settings.json
    # base.en and 150 s: measured x1.6 real time on a 6-core CPU, and Armory cancels a tool call after 120 s.
    # small.en was x0.5 there. Raise both together on a faster or GPU host.
    model: str = "base.en"
    ffmpeg: str = "ffmpeg"
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
    def state_dir(self) -> Path:
        """Where settings.json and backends.json live: the first data folder, even after the data folder was moved."""
        return self.config_dir or self.data_dir

    @property
    def audio_dir(self) -> Path:
        return self.data_dir / "audio"

    @property
    def staging_dir(self) -> Path:
        return self.data_dir / "staging"

    @property
    def exposed_without_token(self) -> bool:
        """Listening beyond loopback with no token lets anything that can reach the port call every tool."""
        return not self.internal_token and self.host not in LOOPBACK_HOSTS


def _from_environment() -> Settings:
    env = os.environ.get
    return Settings(
        host=env("SCRIBE_HOST", "127.0.0.1"),
        port=int(env("SCRIBE_PORT", "29401")),
        internal_token=env("INTERNAL_API_TOKEN", ""),
        data_dir=Path(env("SCRIBE_DATA_DIR") or PROJECT_DIR / ".data"),
        model=env("SCRIBE_MODEL", "base.en"),
        ffmpeg=env("SCRIBE_FFMPEG", "ffmpeg"),
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


def with_saved_data_path(settings: Settings) -> Settings:
    """The admin page's "Data folder" setting moves stored data (audio, uploads, voices, results) to another folder.
    It is read once, here at start. Settings and saved model connections stay in the first folder, which is where this
    value is saved. A folder that cannot be written is ignored."""
    try:
        saved = json.loads((settings.data_dir / "settings.json").read_text("utf-8")).get("data_path")
    except (OSError, ValueError, AttributeError):
        return settings
    if not isinstance(saved, str) or not saved.strip():
        return settings
    target = Path(saved.strip()).expanduser()
    if not target.is_absolute():
        target = PROJECT_DIR / target
    try:
        target.mkdir(parents=True, exist_ok=True)
        probe = target / ".write-test"
        probe.write_text("x")
        probe.unlink()
    except OSError:
        logging.getLogger(__name__).warning("Data folder %s cannot be written; using %s", target, settings.data_dir)
        return settings
    return replace(settings, data_dir=target, config_dir=settings.data_dir)


def load_settings() -> Settings:
    return with_saved_data_path(_from_environment())
