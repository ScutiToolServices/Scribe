# Scribe: Docker on this PC

Run Scribe in Docker Desktop on the machine you work on, so Armory and ember_api on it can reach it.

## Requirements

- Docker with Compose v2.
- A running STT_Whisper_Model service and a token for Scribe made on its `/admin/` page.
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

Then open `/admin/`, save a connection to the STT_Whisper_Model service under **Model services**, and press **Test connection**.

## Updating

`docker compose up -d --build`. Settings, saved connections and stored data live in the data volume and survive it.

## Problems

| Symptom | Likely cause |
|---|---|
| Calls fail with "not connected" | No model service connection is saved on the admin page yet. |
| 401 | The caller sends no `INTERNAL_API_TOKEN`, or the wrong one. |
| The service refuses to start | `SCRIBE_HOST` is not loopback and `INTERNAL_API_TOKEN` is empty. |
