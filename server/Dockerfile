FROM python:3.13-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install uv (fast Python package manager).
RUN pip install --no-cache-dir uv==0.5.14

# Instagram Reels can reject videos over 300 MiB. The publisher has a
# pre-upload transcode path for oversized files, so the runtime image must
# include ffmpeg/ffprobe.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Install dependencies first for better cache reuse.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

# Copy app code + static config.
COPY app ./app
COPY config ./config
COPY avatars ./avatars

# data/ is a volume mount in production.
RUN mkdir -p /app/data

# Run as a non-root user; only /app/data needs write access at runtime.
RUN addgroup --system app && adduser --system --ingroup app app \
    && chown -R app:app /app
USER app

EXPOSE 8000

# Call the venv's uvicorn directly. Using `uv run` here would try to write a
# cache to $HOME/.cache/uv, but the system `app` user has no real home dir.
CMD [".venv/bin/uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
