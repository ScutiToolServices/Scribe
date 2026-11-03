# Scribe

Speech-to-text MCP server for Citadel and Ember. A standalone service on port 8091, registered in Armory as the `scribe` extension. Design: `Python/Citadel/docs/superpowers/specs/2026-10-25-scribe-design.md`. Plan: `Python/Citadel/docs/superpowers/plans/2026-10-25-scribe.md`. Counterpart of Bard (text to speech, `../Bard`).

## Agent instructions
This file is the one instruction file for every coding agent; `CLAUDE.md` only imports it (`@AGENTS.md`). Edit this file, never `CLAUDE.md`.

## Commands (from this folder, venv `.venv`, managed by uv)
- Install: `uv sync` (`python -m pip install uv` if missing; on this machine set `UV_CACHE_DIR=D:/User/.uv-cache`)
- Test: `uv run pytest -q`
- Run: `uv run python -m src.run` (the model downloads into `.data/models` on first transcription)
- Benchmark: `uv run python scripts/bench.py <audio file>`

## Rules
- Never read or print `.env`.
- Logic lives in `service.py`, `engine.py`, `store.py`, `streaming.py`; `app.py` is glue. Tools never call an AI.
- Every blocking call (model, file IO) runs through `asyncio.to_thread`; ffmpeg runs as an asyncio subprocess.
- `INTERNAL_API_TOKEN` guards `/mcp` and `/api/*`; an exposed bind without it must refuse to start.
- Result models end with `message`. Error messages never contain paths.
- The owner key is the username (`_meta.requester` for tools, `X-Requester-Username` for HTTP).
- Defaults are tuned to the Armory call timeout (120 s): `base.en`, 150 s of audio, 110 s transcribe timeout. Measured here: `small.en` x0.5 real time, `base.en` x1.6 (6-core CPU). Raise model and caps together, only after benchmarking.
- Uploaded audio is kept 24 hours on this machine, then deleted.
- Live dictation: `/api/stream` WebSocket takes 16k mono s16le PCM and `{type:stop}`; returns full-text `{type:partial|final,text}` or sanitized `{type:error,message}`. Token and HTTP username identity are checked before accepting. Capacity closes 1013 after acceptance so proxies can observe it.
- `/api/transcribe-partial` decodes an encoded snapshot without retaining audio/transcript and returns `{text,duration_s}`. Both dictation paths honor stored+transient per-user quota, byte and duration bounds; share the existing inference semaphore; timed-out threads keep their slot until done.
- Live settings: `SCRIBE_STREAM_CAPACITY=2` (independent WS and fallback HTTP admission pools), `SCRIBE_STREAM_MAX_FRAME_BYTES=64000`, `SCRIBE_STREAM_IDLE_S=15`, `SCRIBE_STREAM_WINDOW_S=30`. Audio is capped at min(150, SCRIBE_MAX_SECONDS); wall deadline adds SCRIBE_TIMEOUT_S. LocalAgreement commits consecutive word matches with timestamps and trims PCM; no agreement at the window bound returns an error. Runtime WS queue is four frames.
- Whisper decoding limits ffmpeg output to the audio limit plus one second, rejects oversize PCM, and kills/reaps ffmpeg on cancellation or SCRIBE_TIMEOUT_S decode timeout. Keep this bound before any compressed upload is decoded into memory.
