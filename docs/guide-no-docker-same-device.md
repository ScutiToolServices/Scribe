# Scribe: without Docker, on Windows

Run Scribe directly with Python: one process, no load balancer, listening on `127.0.0.1:8091`.

## Requirements

- Windows with uv (`winget install astral-sh.uv`) and ffmpeg.
- A running STTWhisperModel service and a token for Scribe made on its `/admin/` page.

## Start

The service reads `.env` in this folder (copy `.env.example`). The variables are listed under Settings in [guide-docker-same-device.md](guide-docker-same-device.md#settings); the ones that name Docker or Compose do not apply here.

Double-click `run.bat`, or run it in a terminal from `Scribe\`. It runs `uv sync` to create or update `.venv`, then starts `python -m src.run`.

```bash
uv sync
uv run python -m src.run
```

## Check

```bash
curl http://127.0.0.1:8091/health
```

Then open `/admin/`, save a connection to the STTWhisperModel service under **Model services**, and press **Test connection**.

## Stop

Ctrl+C in the terminal. Do not run this and the Docker stack together: both use port 8091.

## Tests

```bash
uv run pytest -q
```
