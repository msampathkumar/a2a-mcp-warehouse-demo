FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY demo-app/ ./demo-app/

ENV LEVEL=5 \
    PORT=8080 \
    RUNS_DIR=/tmp/runs \
    A2A_CACHE_DIR=/tmp/a2a_cache \
    PUBLIC_DEMO=true \
    A2A_ENABLED=false \
    ISOLATED_WAREHOUSES=true \
    CLOUD_LOGGING=true \
    BQ_LOCATION=us-central1

CMD ["sh", "-c", "uv run --frozen uvicorn --factory warehouse.app:create_app --app-dir demo-app --host 0.0.0.0 --port ${PORT:-8080}"]
