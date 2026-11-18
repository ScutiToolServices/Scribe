"""Tool result models. `message` is last: ember's generic formatter shows it first."""

from __future__ import annotations

from pydantic import BaseModel


class SegmentOut(BaseModel):
    start_s: float
    end_s: float
    text: str


class TranscribeResult(BaseModel):
    text: str
    duration_s: float
    language: str
    segments: list[SegmentOut] | None = None
    cached: bool
    cleaned: bool = False  # text went through the S1Mini cleanup; segments stay as transcribed
    voice_gate: bool = False  # the main-voice gate silenced other voices before transcribing
    message: str
