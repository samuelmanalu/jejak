# Jejak server image. Build from the repo root:
#   docker build -f deploy/Dockerfile -t jejak-server .
FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv
ENV UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/app/server/.venv USER=jejak \
    JEJAK_HOST=0.0.0.0 JEJAK_PORT=7700 JEJAK_SERVER_CONFIG=/nonexistent
WORKDIR /app/server
COPY server/pyproject.toml server/uv.lock ./
RUN uv sync --frozen --no-dev
COPY server/jejak_server ./jejak_server
COPY hooks/jejak_common.py hooks/jejak.html /app/hooks/
RUN useradd --system --uid 10001 jejak
USER jejak
EXPOSE 7700
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:7700/healthz',timeout=4)"
ENTRYPOINT ["/app/server/.venv/bin/python", "-m", "jejak_server"]
CMD ["serve"]
