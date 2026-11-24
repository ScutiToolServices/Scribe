# Deploying Scribe

Scribe is one HTTP and MCP service (speech to text) on port 29401. It runs no model itself: it needs a STT_Whisper_Model connection (or STT_Moonshine_Model, English only, no word timestamps) saved on its admin page; the Speech engine option (`SERVICE_ENGINE`, `whisper` or `moonshine`) picks which one transcribes.

## Ways to run it

Each path has its own guide. Pick one.

| Path | Guide |
|---|---|
| Docker Desktop on this PC | [guide-docker-same-device.md](docs/guide-docker-same-device.md) |
| Docker on another always-on machine, published with `run.bat` (Deploy) | [guide-docker-deployment-device.md](docs/guide-docker-deployment-device.md) |
| Directly on Windows, no Docker | [guide-no-docker-same-device.md](docs/guide-no-docker-same-device.md) |

Once it runs, the admin page is covered by [guide-web-ui.md](docs/guide-web-ui.md).
