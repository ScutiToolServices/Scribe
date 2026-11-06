# Scribe

Speech-to-text MCP server for Citadel and Ember. A standalone service on port 8091, registered in Armory as the `scribe` extension. Design: `Python/Citadel/docs/superpowers/specs/2026-10-25-scribe-design.md`. Plan: `Python/Citadel/docs/superpowers/plans/2026-10-25-scribe.md`. Counterpart of Bard (text to speech, `../Bard`).

## Agent instructions
This file is the one instruction file for every coding agent; `CLAUDE.md` only imports it (`@AGENTS.md`). Edit this file, never `CLAUDE.md`.

## Commands (from this folder, venv `.venv`, managed by uv)
- Install: `uv sync` (`winget install astral-sh.uv` if missing; on this machine set `UV_CACHE_DIR=D:/User/.uv-cache`)
- Test: `uv run pytest -q`
- Run: `uv run python -m src.run` (the model downloads into `.data/models` on first transcription)
- Benchmark: `uv run python scripts/bench.py <audio file>`

## Rules
- Data folder: `Settings.data_dir` holds uploaded audio and model files; the admin setting `data_path` (restart, `src/config.py` `with_saved_data_path`) moves it at the next start and keeps `settings.json` and `backends.json` in `Settings.state_dir` (the first folder). No `.env` variable; `SCRIBE_DATA_DIR` is read only as the base folder (Docker).
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
- Live settings: `SCRIBE_STREAM_CAPACITY=2` (independent WS and fallback HTTP admission pools), `SCRIBE_STREAM_MAX_FRAME_BYTES=64000`, `SCRIBE_STREAM_IDLE_S=15`, `SCRIBE_STREAM_WINDOW_S=30`. Audio is capped at min(150, SCRIBE_MAX_SECONDS); wall deadline adds SCRIBE_TIMEOUT_S. LocalAgreement commits consecutive word matches with timestamps and trims PCM; no agreement at the window bound returns an error. Runtime WS queue is four frames. `serve_stream` runs each pass as a task beside the receive loop (one at a time, started once a second of new audio is in): a slow model must never stop audio being read, or the browser's five-second queue overflows and it falls back to periodic updates.
- Whisper decoding limits ffmpeg output to the audio limit plus one second, rejects oversize PCM, and kills/reaps ffmpeg on cancellation or SCRIBE_TIMEOUT_S decode timeout. Keep this bound before any compressed upload is decoded into memory.
- Runtime settings: with `SCRIBE_ADMIN_PASSWORD` set, `/admin/` (page in `web/`, API `/admin/settings`, header `X-Admin-Password`) edits the options in `options.py`; they are saved to `.data/settings.json` and win over `.env`. `src/runtime.py` is shared with Bard and copies Laya's `settings.py`: keep the copies in step. Live options are read through `LiveSettings`; `restart=True` ones (model, concurrency, stream capacity, frame size) apply at the next start. Without the password the page is off and nothing is changeable. `INTERNAL_API_TOKEN` does not guard `/admin`.
- Model location: with an address saved for the `whisper` slot on the admin page, `HttpWhisperEngine` (in `src/engine.py`) posts PCM to the `AI/ModelServices/STTWhisperModel` service (its saved token as `X-Internal-Token`) and ffmpeg decoding stays here; no address = faster-whisper in this process. `SCRIBE_MODEL` must match the remote model (it keys the transcript cache). Remote failures all become `EngineFailed("Transcription failed. Try again.")`. The admin `model` option has no effect in remote mode.
- Admin page (`web/index.html`, `src/admin_api.py`, `tests/test_admin_api.py`): the same three files in Bard, Scribe and Reader; change one, copy it to the other two. With `SCRIBE_ADMIN_PASSWORD` set, `/admin/` shows status and usage per user (`GET /admin/status`, in-memory `Usage`, reset on restart), the model-service slots (`/admin/backends`: where speech to text (`whisper` slot, `RoutedEngine` in `src/engine.py`) runs, address + token saved in `.data/backends.json`, never sent back, read on every call) and the settings. Header `X-Admin-Password`. Each service passes its own slots, `status` items and probes to `register(...)` in `app.py`. Transcribe, partial and dictation calls are counted through `usage.track`.
- Saved connections: the admin page also keeps a shared list of named model-service addresses (`/admin/connections/{name}`, under the reserved `_connections` key of `backends.json`, token never sent back). A slot picks one with `PUT /admin/backends/{key}` `{"connection": name}`, so editing a connection updates every slot using it; a connection a slot uses cannot be deleted (409). Addresses and tokens are saved only on the admin page (there are no environment variables for them); a slot with no address runs in this service, or fails when it has no local engine.
