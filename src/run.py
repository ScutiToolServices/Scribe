"""python -m src.run"""

from __future__ import annotations

import sys

import uvicorn

from src.app import build_app
from src.config import PROJECT_DIR, load_settings
from src.runtime import load_env_file
from src.options import live, make_config


def serve(app, **options) -> None:
    """uvicorn.run, plus the admin page link under uvicorn's own "running on" line."""

    class Server(uvicorn.Server):
        async def startup(self, sockets=None):
            await super().startup(sockets)
            host = "127.0.0.1" if self.config.host in ("0.0.0.0", "::") else self.config.host
            print(f"INFO:     Admin page: http://{host}:{self.config.port}/admin/", flush=True)

    Server(uvicorn.Config(app, **options)).run()


def main() -> None:
    if "--dotenv" in sys.argv[1:]:  # run.bat asks for it; Docker and the tests never read .env
        env_file = PROJECT_DIR / ".env"
        if env_file.is_file():
            names = load_env_file(env_file)
            print(f".env: loaded {', '.join(names) or 'no variables'} (names only; a real environment variable wins)", flush=True)
        else:
            print(".env: no file in the service folder", flush=True)
    settings = load_settings()
    if settings.exposed_without_auth:
        sys.exit("Scribe refuses to start: SERVICE_HOST is not a loopback address and SERVICE_ADMIN_PASSWORD is empty (it unlocks the token page).")
    config = make_config(settings)
    serve(
        build_app(settings, config=config), host=settings.host, port=settings.port, log_level="info",
        ws="websockets", ws_max_size=live(settings, config).stream_max_frame_bytes, ws_max_queue=4,
    )


if __name__ == "__main__":
    main()
