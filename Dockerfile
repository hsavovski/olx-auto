# The web app (`olx-auto serve`). data/ and .env are mounted at run time (compose.yaml), never baked in.
FROM python:3.15-slim

COPY --from=ghcr.io/astral-sh/uv:0.13.7 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH=/opt/venv/bin:$PATH PYTHONUNBUFFERED=1

WORKDIR /app

# dependencies first, so a code change doesn't reinstall them
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# installed editable, so config.ROOT is /app: .env, data/ and context/ are found there
COPY README.md ./
COPY context ./context
COPY src ./src
RUN uv sync --frozen --no-dev

RUN useradd --create-home --uid 1000 app && mkdir -p /app/data && chown app /app/data
USER app

EXPOSE 8000
CMD ["olx-auto", "serve", "--host", "0.0.0.0", "--port", "8000"]
