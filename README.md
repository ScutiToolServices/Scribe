# Scribe

Speech to text for Citadel and Ember, with HTTP and MCP on port 29401. A user attaches an English audio file (mp3, wav, m4a, ogg, flac, webm) in chat; the agent calls `scribe__tool_scribe_transcribe` and answers with the transcript. The transcription runs on a `STT_Whisper_Model` service: connect one on the admin page first.

## Tools and API

- Upload: `POST /api/files` (raw bytes, `X-Requester-Username`, `X-Filename`), used by ember_api and the authenticated admin playground.
- Tool: `tool_scribe_transcribe(file_id, timestamps=false)` over MCP at `/mcp`.
- Dictation: `POST /api/transcribe` accepts the same raw audio and identity headers as `/api/files`, reuses upload validation, quota and cached transcription, and returns `{text, duration_s}`. Optional cleanup through the `s1mini` slot: add `?cleanup=true&styling=&structure=&context=` (the reply then also has `cleaned`). Ember records WebM for up to 150 s and appends the transcript to the editable draft through `/api/attachments/dictate` (`files.upload`, 20 MB cap). It never sends the draft automatically.
- Live dictation: `WS /api/stream` uses `X-Internal-Token` (when configured) and `X-Requester-Username`, checked before acceptance. Send binary 16 kHz mono signed 16-bit little-endian PCM; send `{"type":"stop"}` to finish. Responses are `{"type":"partial","text":"..."}`, `{"type":"final","text":"..."}` or `{"type":"error","message":"..."}`. Text is the complete current transcript. Roughly each second of received PCM triggers a new pass. Consecutive passes commit agreeing words and trim their timestamped audio; stop transcribes the remaining window once more.
- Fallback: `POST /api/transcribe-partial` accepts a complete encoded audio snapshot with the same headers and returns `{text, duration_s}`. It deletes its staging file after decoding and retains neither audio nor transcript. Live PCM is also never stored. Both paths honor the per-user quota, including retained uploads and concurrent transient audio.

## Setup

- Run: `run.bat run` (loads `.env`) or `uv run python -m src.run --dotenv` (first `uv sync`). Docker: `docker compose up -d --build` (the Citadel network must exist).

## Limits worth knowing

- Limits: 25 MB, 150 s of audio, 200 MB per user. Uploads and transcripts are deleted after 24 hours.
- Privacy: user audio sits on this machine for 24 hours.
- Live limits: 150 s total audio (or lower `SERVICE_MAX_SECONDS`), `SERVICE_MAX_BYTES`, 64,000 bytes per frame, 15 s idle. The wall deadline allows the audio limit plus `SERVICE_TIMEOUT_S` for inference. Unstable speech that cannot commit within the 30 s audio window ends with a sanitized error; it is never silently discarded. Saturated WebSockets accept then close with 1013 so proxies can detect fallback; unauthorized sockets close before acceptance. Fallback HTTP requests return 503 when their own admission pool is full.
- Settings: `SERVICE_STREAM_CAPACITY=2` caps simultaneous WebSockets and separately caps ephemeral HTTP requests, allowing fallback when sockets are full. `SERVICE_STREAM_MAX_FRAME_BYTES=64000`, `SERVICE_STREAM_IDLE_S=15`, and `SERVICE_STREAM_WINDOW_S=30` tune the other bounds. Both paths share `SERVICE_CONCURRENCY` with ordinary transcription. A timed-out worker keeps its inference slot until it actually finishes. The server queues at most four incoming WebSocket messages per connection.
- Decoding is bounded before transcription: ffmpeg emits at most one second beyond the audio limit (to detect oversize input), rejects that oversize PCM, and is killed and reaped on cancellation or `SERVICE_TIMEOUT_S` decode timeout.

### Releasing

A plain publish (`run.bat`, Deploy, option 1) only updates the `live` branch, so it is fine for test deploys. A release is a separate step: set `VERSION` (and `pyproject.toml` where it has a version, to the same number), commit, then run `run.bat`, Deploy, option R or `py tools\publish_live.py --release`. That publishes to `live` and tags the `main` commit `vX.Y.Z` and the live commit `live-vX.Y.Z`. A version already released with different live files is refused, so bump `VERSION`. While a version is not officially released yet, add `--replace` to move its tags.

`run.bat`, Build the release ZIP, then builds `_zip_versions\Scribe-<VERSION>.zip` (no `tools/`, no git-ignored files, no older ZIPs; `.env.example` is included) and opens GitHub's new-release page for the tag.

## Admin page

With `SERVICE_ADMIN_PASSWORD` set, open `/admin/` and sign in. The shared sidebar has **Overview**, **Playground**, **Access tokens**, **Model connections**, **Runtime & settings**, **Usage** and **Connect & API**. Each page has a direct URL hash. The sidebar collapses to an icon rail and remembers its display preference in this browser.

The Speech to text slot calls STT_Whisper_Model. Keep the runtime model option equal to the remote model because it keys the transcript cache. Overview reports the model and where transcription runs.

Overview reads `/health` and the service status. Usage shows calls, errors and average time per requester, and resets on restart. Model connections keeps slot test/save/reset and reusable named connections. Runtime & settings saves options and marks those needing a restart. Access tokens creates and revokes caller tokens; Connect & API lists addresses and endpoints with Copy buttons. Admin authentication remains separate from public request tokens. See [the web UI guide](docs/guide-web-ui.md).

## Tests

```bash
uv sync
uv run pytest -q
```

## Try it from the admin page

Open `/admin/#playground` and use **Playground** with a token from **Access tokens** and a requester username. The token is held only in memory, separate from admin sign-in. Requests use the ordinary public API, quotas and cache; the panel shows HTTP status, latency, output, cancellation and curl with a token placeholder. See [the web UI guide](docs/guide-web-ui.md).

Enter the requester username and upload encoded audio. The panel posts its bytes to `POST /api/transcribe` with `X-Requester-Username` and `X-Filename`, then displays the JSON transcript. Scribe keeps its existing ffmpeg decoding, duration/byte caps, per-user quota, cached transcription and retention behavior. This upload playground does not start a live dictation WebSocket.

## Documentation

- [Purpose: input, output and what it is for](docs/purpose.md)
- [Deployment](deployment.md)
- [Changelog](CHANGELOG.md)
- [Docker on this PC](docs/guide-docker-same-device.md)
- [Docker on another machine](docs/guide-docker-deployment-device.md)
- [Run without Docker](docs/guide-no-docker-same-device.md)
