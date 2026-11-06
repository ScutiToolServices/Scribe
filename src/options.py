"""The Scribe options the admin page can change. The default shown is the value at start (.env or built in), which
load_settings already read; a saved value wins over it."""

from __future__ import annotations

from src.config import Settings
from src.runtime import Config, LiveSettings, Setting

MB = 1024 * 1024

# attribute of Settings -> (setting key, conversion to the attribute's unit)
FIELDS = {
    "model": ("model", str),
    "max_seconds": ("max_seconds", int),
    "timeout_s": ("timeout_s", int),
    "max_bytes": ("max_upload_mb", lambda mb: int(mb * MB)),
    "ttl_seconds": ("ttl_hours", lambda hours: hours * 3600),
    "quota_bytes": ("quota_mb", lambda mb: mb * MB),
    "concurrency": ("concurrency", int),
    "stream_capacity": ("stream_capacity", int),
    "stream_max_frame_bytes": ("stream_max_frame_bytes", int),
    "stream_idle_s": ("stream_idle_s", float),
    "stream_window_s": ("stream_window_s", float),
    "ffmpeg": ("ffmpeg", str),
}

_TUNED = "Armory cancels a call after 120 s: raise only after benchmarking (scripts/bench.py)"


def schema(base: Settings) -> list[Setting]:
    return [
        Setting("model", "Whisper model", "text", base.model, blank=False, restart=True,
                help="Slower models are more accurate. " + _TUNED),
        Setting("max_seconds", "Longest audio (seconds)", "int", base.max_seconds, minimum=1, maximum=3600,
                help="Live dictation is capped at 150 s. " + _TUNED),
        Setting("timeout_s", "Transcription time limit (seconds)", "int", base.timeout_s, minimum=1, maximum=600,
                help=_TUNED),
        Setting("max_upload_mb", "Largest upload (MB)", "float", base.max_bytes / MB, minimum=0.1, maximum=1024),
        Setting("ttl_hours", "Keep uploads for (hours)", "int", base.ttl_seconds // 3600, minimum=1, maximum=8760),
        Setting("quota_mb", "Audio storage per user (MB)", "int", base.quota_bytes // MB, minimum=1, maximum=100000),
        Setting("concurrency", "Transcriptions at once", "int", base.concurrency, minimum=1, maximum=16, restart=True),
        Setting("stream_capacity", "Live dictation streams", "int", base.stream_capacity, minimum=1, maximum=16,
                restart=True, help="Simultaneous WebSocket streams, and separately the same for HTTP previews"),
        Setting("stream_max_frame_bytes", "Largest dictation frame (bytes)", "int", base.stream_max_frame_bytes,
                minimum=1024, maximum=1048576, restart=True),
        Setting("stream_idle_s", "Dictation idle timeout (seconds)", "float", base.stream_idle_s, minimum=1, maximum=300),
        Setting("stream_window_s", "Dictation audio window (seconds)", "float", base.stream_window_s, minimum=5, maximum=120),
        Setting("ffmpeg", "ffmpeg program", "text", base.ffmpeg, blank=False, help="A name on PATH or a full path"),
        Setting("data_path", "Data folder", "text", "", restart=True,
                help=f"Where stored data (uploaded audio and model files) goes. Empty = {base.state_dir}. Applies after a restart; files already stored are not moved. "
                     f"Settings and saved model connections stay in the standard folder."),
    ]


def make_config(base: Settings) -> Config | None:
    """None, so nothing is changeable at run time, unless SCRIBE_ADMIN_PASSWORD is set."""
    return Config(base.state_dir / "settings.json", schema(base)) if base.admin_password else None


def live(base: Settings, config: Config | None):
    return LiveSettings(base, config, FIELDS) if config else base
