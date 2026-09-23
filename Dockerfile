FROM python:3.14-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:0.11.7 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0

WORKDIR /app

RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-install-project --no-dev --no-editable

COPY README.md pyproject.toml uv.lock ./
COPY src ./src

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable


FROM python:3.14-slim

ENV PATH="/app/.venv/bin:$PATH" \
    DATABASE_URL=sqlite:////data/workflows.db

COPY --from=builder /app/.venv /app/.venv

RUN groupadd --system workflows && useradd --system --gid workflows --create-home workflows \
    && mkdir -p /data \
    && chown -R workflows:workflows /data

USER workflows
WORKDIR /data

VOLUME ["/data"]

EXPOSE 8000

CMD ["uvicorn", "--factory", "workflows.app:create_app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
