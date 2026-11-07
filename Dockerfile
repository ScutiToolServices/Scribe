# syntax=docker/dockerfile:1
FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.13.6 /uv /usr/local/bin/uv
ENV PYTHONUNBUFFERED=1 UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/opt/venv PATH="/opt/venv/bin:$PATH" SCRIBE_DATA_DIR=/data SCRIBE_HOST=0.0.0.0 SCRIBE_WEB_DIR=/app/web
WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
COPY web ./web
RUN uv sync --frozen --no-dev --no-editable

EXPOSE 8091
# No model runs here: save a STT_Whisper_Model connection on the admin page.
CMD ["python", "-m", "src.run"]
