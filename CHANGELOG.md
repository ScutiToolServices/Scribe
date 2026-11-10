# Changelog

## Unreleased

### Main-voice gate on or off per request

`tool_scribe_transcribe` takes `voice_gate`, and `POST /api/transcribe`, `POST /api/transcribe-partial` and `WS /api/stream` take `?voice_gate=true|false`. Omitted follows the service setting. The result carries `voice_gate` (whether the gate ran); forcing it on with no voice set up runs without it and says so in `message`. Gated and plain transcripts are cached apart.

### Main-voice gate (optional)

Silences speech that does not sound like an enrolled voice before the PCM goes to the Whisper service, so other voices are not transcribed. Off by default: needs `uv sync --extra voice`, a WeSpeaker-style ONNX speaker model at `<data>/voice/speaker.onnx` and a voiceprint from `tools/enroll_voice.py`. Admin settings `voice_gate` and `voice_gate_threshold` (env `SERVICE_VOICE_GATE`, `SERVICE_VOICE_GATE_THRESHOLD`). Gated transcripts are cached apart from plain ones. Not tuned on real recordings.

### Playground works behind a path-prefix proxy

The Try it panel built its request URLs from the page origin, so behind ServiceHub (`/s/<id>/`) it called the hub's root and got 404 Not Found. It now resolves routes against the service root (`../` from the admin page). Same-origin checking is unchanged.


### Playground: no token to paste, model picker, autoplay and batch

The playground signs in with a temporary caller token made from the admin session (named `playground (temporary)`, revoked on sign-out), so the service token field is gone. Choices such as voices load by themselves, and a strip above the form shows which models are connected. Services whose lookup reply carries `models` get a Model picker that narrows the choices. Operations with a text field and audio back get an Autoplay toggle and a "One request per line" toggle that sends each line as its own request and plays the results in order.

### Model connections page redesign

Each model slot now shows its state first (saved connection, direct address, runs in this service, or not connected) with the address and whether a token is saved. Editing opens in place: pick a source (saved connection, direct address or in this service) and only the fields for that source appear. A slot that has not been connected opens its editor. Test connection also updates the slot's state. Saved connections are a table with credentials and the slots using them; Add connection opens the form only on request, and a connection in use has its Delete button disabled. The admin API is unchanged.

### Transcript cleanup (optional)

A second model-service slot, `s1mini` (the `STC_S1Mini_Model` service, default `http://127.0.0.1:29280`), is saved on the admin page next to `whisper`. `tool_scribe_transcribe` and `POST /api/transcribe` take opt-in `cleanup` plus `styling` (`casual`, `semi-casual`, `semi-formal`, `formal`), `structure` (`prose`, `lists`) and `context` (`general`, `email`); on the HTTP route they are query options (`?cleanup=true&styling=formal`). The cached transcript stays raw and cleanup is applied per call; segments and timestamps stay as transcribed. With no address saved, or if the service fails, the raw transcript comes back and `message` says cleanup was skipped (`cleaned: false`). It removes filler and fixes punctuation, casing and tone; it cannot use text from the screen, because S1Mini's `context` is only `general` or `email`. Live dictation and `/api/transcribe-partial` are unchanged.

### Admin navigation

The admin page now has seven sidebar pages: Overview, Playground, Access tokens, Model connections, Runtime & settings, Usage and Connect & API. Overview reads live health and service status; links and browser history select pages. The sidebar collapses to an icon rail and remembers its display preference. Existing connections, tokens, settings and public Playground authentication keep their behavior.

### Breaking: callers use admin-page tokens, not `INTERNAL_API_TOKEN`

`INTERNAL_API_TOKEN` is no longer read. Set `SERVICE_ADMIN_PASSWORD`, open `/admin/`, make one token per caller on the new **Tokens** card (stored hashed in `tokens.json` in the data folder; sent in `X-Internal-Token`) and put it in the caller's configuration (Armory: the extension's `headers`). A token can carry a calls-per-minute limit and an expiry, and **Revoke** cuts a caller off at once. Without an admin password there are no checks, and the service refuses to start on a non-loopback host.

### Breaking: common service environment variables

Service-owned environment variables now use `SERVICE_*`. Old names are no longer read.
Rename old names once in your real `.env`, Compose `environment:` block, and CI/secret settings.

| Old name | New name |
|---|---|
| `SCRIBE_ADMIN_PASSWORD` | `SERVICE_ADMIN_PASSWORD` |
| `SCRIBE_BIND` | `SERVICE_BIND` |
| `SCRIBE_CONCURRENCY` | `SERVICE_CONCURRENCY` |
| `SCRIBE_DATA_DIR` | `SERVICE_DATA_DIR` |
| `SCRIBE_FFMPEG` | `SERVICE_FFMPEG` |
| `SCRIBE_HOST` | `SERVICE_HOST` |
| `SCRIBE_MAX_BYTES` | `SERVICE_MAX_BYTES` |
| `SCRIBE_MAX_SECONDS` | `SERVICE_MAX_SECONDS` |
| `SCRIBE_MODEL` | `SERVICE_MODEL` |
| `SCRIBE_PORT` | `SERVICE_PORT` |
| `SCRIBE_QUOTA_MB` | `SERVICE_QUOTA_MB` |
| `SCRIBE_STREAM_CAPACITY` | `SERVICE_STREAM_CAPACITY` |
| `SCRIBE_STREAM_IDLE_S` | `SERVICE_STREAM_IDLE_S` |
| `SCRIBE_STREAM_MAX_FRAME_BYTES` | `SERVICE_STREAM_MAX_FRAME_BYTES` |
| `SCRIBE_STREAM_WINDOW_S` | `SERVICE_STREAM_WINDOW_S` |
| `SCRIBE_TIMEOUT_S` | `SERVICE_TIMEOUT_S` |
| `SCRIBE_TTL_HOURS` | `SERVICE_TTL_HOURS` |
| `SCRIBE_WEB_DIR` | `SERVICE_WEB_DIR` |

- Default service port moves from 8091 to 29401. Update any explicit port in your real `.env`, saved model-service connections in `backends.json`, and browser or MCP client configurations once; model-service connection ports also move to the new model port blocks. Docker users who used the Compose default receive the new host and container ports together; explicit host-port overrides remain in effect until changed.
- Model connection labels, Docker CONNECT hosts and documentation use the purpose-prefixed model-service names. Update saved model-service connections to the renamed load balancer hosts using the current connection defaults.

- `run.bat` loads `.env` from the service folder for a local run: it passes `--dotenv`, the service prints which variable names it loaded (never the values), and a real environment variable still wins. A plain `uv run python -m src.run`, Docker and the tests never read `.env`.
- Untracked folders now start with `.` or `_`: `zip_versions/` is `_zip_versions/`. A local `voices`, `tem_code` or `zip_versions` folder from an older checkout needs renaming by hand.
- `run.bat` is now a menu (run the service, deploy, build the release ZIP) and `run.bat run|deploy|zip` skips it. `tools/deploy.bat`, `tools/make_zip.bat` and `tools/deploy.local.example.bat` are gone; the local deploy target is `tools/deploy.local.ini` (copy `tools/deploy.local.example.ini`).
- Admin page: **Connect to this service** card with the addresses other services can use (this page's address, same PC, Docker network, `host.docker.internal`) and every endpoint, each with a Copy button.
- Admin **Try it** panel sends real public API requests with a requester username, readable status/errors, latency, cancellation, media/text results and redacted curl. Auth choice (b): the public token stays in memory, separate from admin auth; existing quotas, ownership and caches apply.
