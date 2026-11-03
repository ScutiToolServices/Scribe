# syntax=docker/dockerfile:1
FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.13.6 /uv /usr/local/bin/uv
ENV PYTHONUNBUFFERED=1 UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/opt/venv PATH="/opt/venv/bin:$PATH" SCRIBE_DATA_DIR=/data SCRIBE_HOST=0.0.0.0
WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

EXPOSE 8091
# The model downloads into the /data volume on first start; later starts reuse it (and still boot offline).
CMD ["sh", "-c", "python -c \"from faster_whisper import WhisperModel; WhisperModel('${SCRIBE_MODEL:-base.en}', device='cpu', compute_type='int8', download_root='/data/models')\"; exec python -m src.run"]
