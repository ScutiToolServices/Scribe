# Scribe: Docker on a deployment machine

Run Scribe on a separate always-on machine (default `lex@scutiserver1.local`, folder `/media/ZimaOS-HD/Apps/Scribe`). Armory and ember_api reach it over the published port.

## Requirements on the machine

- Docker with Compose v2.
- A running STT_Whisper_Model service and a token for Scribe made on its `/admin/` page.
- The external `citadel_core` network, so Armory and ember_api can reach Scribe.
- ssh access (root over ssh is refused; use the normal user).

## 1. Publish the code (from your PC)

Run `Scribe\run.bat` and choose **Deploy** (or `run.bat deploy`). Menu:

| Option | Does |
|---|---|
| 1 | Publish code to the `live` branch |
| 2 | Copy your `.env` to the server (one ssh/tar session, one password prompt) |
| 3 | Both |
| 4, 5 | Dry runs of 1 and 2 |
| 6 | chown helper, if option 2 says `Permission denied` |
| R | Release: publish, then tag `VERSION` |

Keep your own target in the git-ignored `tools\deploy.local.ini` (copy `tools\deploy.local.example.ini`), or set
`DEPLOY_HOST`, `DEPLOY_DIR` and `DEPLOY_WEB_PORT` before running. Commit `main` first: publish reads committed `main`.

## 2. First-time setup on the machine

```bash
mkdir -p /media/ZimaOS-HD/Apps
git clone -b live <your-repo-url> /media/ZimaOS-HD/Apps/Scribe
cd /media/ZimaOS-HD/Apps/Scribe
```

Create `.env` (deploy option 2 copies yours, or `cp .env.example .env` and edit). The variables are listed under Settings in [guide-docker-same-device.md](guide-docker-same-device.md#settings).

## 3. Start

```bash
docker compose up -d --build
curl http://127.0.0.1:8091/health
```

Then open `/admin/`, save a connection to the STT_Whisper_Model service under **Model services**, and press **Test connection**.

## Updating

```bash
cd /media/ZimaOS-HD/Apps/Scribe
git pull
docker compose up -d --build
```

`docker compose up -d --build`. Settings, saved connections and stored data live in the data volume and survive it.

## Releasing

A plain publish (`run.bat`, Deploy, option 1) only updates the `live` branch. For a release set `VERSION` (and `pyproject.toml` to the same number), commit, then run `run.bat`, Deploy, option R or `py tools\publish_live.py --release`. `run.bat`, Build the release ZIP, then builds `_zip_versions\Scribe-<VERSION>.zip`.

## Problems

See [Problems](guide-docker-same-device.md#problems) in the Docker-on-this-PC guide.
