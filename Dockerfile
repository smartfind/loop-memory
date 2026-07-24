# syntax=docker/dockerfile:1.6
# Loop Memory — local-first memory system for Codex / Claude / Hermes / OpenClaw.
# Single image: Python 3.11 + the project + its optional [serve] deps. SQLite
# is embedded (no external DB container needed) — the database file is mounted
# as a named volume so memories survive container restarts.
#
# Build:
#   docker build -t loop-memory .
# Run (dev):
#   docker run --rm -p 7767:7767 \
#     -v loop_memory_data:/data \
#     -v ~/.codex/sessions:/watch/codex:ro \
#     -v ~/.claude:/watch/claude:ro \
#     loop-memory
#
# Or use the compose file:
#   docker compose up --build

FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    LOOP_MEMORY_DB=/data/loop_memory.db \
    LOOP_MEMORY_HOME=/data

# Build tools only needed for transitive C extensions (numpy via
# sentence-transformers). ``--no-install-recommends`` keeps the
# image at ~250MB instead of ~700MB.
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy ONLY pyproject.toml first to leverage Docker layer caching for
# the dependency install (re-builds skip this layer unless the deps
# change).
COPY pyproject.toml ./
COPY loop_memory ./loop_memory

# Install the project + its [serve] extras (FastAPI / uvicorn).
# The optional [openai] / [chroma] / [sentence] extras are NOT installed
# by default — the container is a minimal memory server. Add them in
# your own override if a specific feature requires them.
RUN pip install --upgrade pip \
 && pip install ".[serve]"

EXPOSE 7767

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD curl -fsS http://localhost:7767/api/healthz || exit 1

# ``serve.py`` reads the static dir from inside the package
# (loop_memory/serve/static), so no extra mount is needed for the UI.
# ``--no-browser`` keeps the container headless.
CMD ["loop-memory", "serve", "--host", "0.0.0.0", "--port", "7767", "--no-browser"]
