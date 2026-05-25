FROM python:3.11-slim

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# Install dependencies (cached layer — only reruns when lockfile changes)
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# Copy source
COPY uvrad/ uvrad/
COPY api/ api/

# Bake git version (sha + commit timestamp) into _version.py at build time.
# Needs .git present in build context (always true for HF Spaces / git-cloned builds).
COPY .git/ .git/
RUN apt-get update -qq \
    && apt-get install -y --no-install-recommends git \
    && SHA=$(git log -1 --format=%h) \
    && TS=$(git log -1 --format=%cd --date='format:%Y%m%dT%H%M%SZ') \
    && printf 'VERSION = "0.1.0+%s.%s"\n' "$SHA" "$TS" > uvrad/_version.py \
    && apt-get purge -y --auto-remove git \
    && rm -rf /var/lib/apt/lists/* .git/

ENV PYTHONUNBUFFERED=1

EXPOSE 7860
CMD ["uv", "run", "--no-sync", "uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "7860"]
