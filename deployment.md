# Deploying Scribe

Scribe is one HTTP and MCP service (speech to text) on port 8091. It runs no model itself: it needs a STTWhisperModel connection saved on its admin page.

## Requirements

- Docker with Compose v2, or native Windows with uv and ffmpeg.
- A running STTWhisperModel service and a token for Scribe made on its `/admin/` page.
- The external `citadel_core` network, so Armory and ember_api can reach Scribe.

## Settings

In `.env` next to `docker-compose.yml` (copy `.env.example`):

| Variable | Default | Effect |
|---|---|---|
| `INTERNAL_API_TOKEN` | empty | Shared token for `/mcp` and `/api/*`. Required when `SCRIBE_HOST` is not loopback. |
| `SCRIBE_HOST` / `SCRIBE_PORT` | `127.0.0.1` / `8091` | Listen address (Compose sets the host itself). |
| `SCRIBE_BIND` | see `.env.example` | Docker only: the address the published port listens on. |
| `SCRIBE_ADMIN_PASSWORD` | empty | Unlocks `/admin/`. Empty = page off, so no model connection can be saved. |

Everything else (limits, data folder, model connection) is set on the admin page. See `docs/guide-web-ui.md`.

## Start

```bash
docker compose up -d --build
curl http://127.0.0.1:8091/health
```

Then open `/admin/`, save a connection to the STTWhisperModel service under **Model services**, and press **Test connection**.

## Updating

`docker compose up -d --build`. Settings, saved connections and stored data live in the data volume and survive it.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| Calls fail with "not connected" | No model service connection is saved on the admin page yet. |
| 401 | The caller sends no `INTERNAL_API_TOKEN`, or the wrong one. |
| The service refuses to start | `SCRIBE_HOST` is not loopback and `INTERNAL_API_TOKEN` is empty. |

## Releasing

A plain publish (`tools\deploy.bat` option 1) only updates the `live` branch. For a release set `VERSION` (and `pyproject.toml` to the same number), commit, then run `tools\deploy.bat` option R or `py tools\publish_live.py --release`. `tools\make_zip.bat` then builds `zip_versions\Scribe-<VERSION>.zip`.
