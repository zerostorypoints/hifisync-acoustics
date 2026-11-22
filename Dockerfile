# syntax=docker/dockerfile:1
FROM python:3.13-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.9 /uv /usr/local/bin/uv
ENV UV_PYTHON_DOWNLOADS=0 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/opt/venv
WORKDIR /src
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY README.md LICENSE ./
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

FROM python:3.13-slim
RUN groupadd --gid 10001 hifisync \
 && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin hifisync \
 && mkdir /data && chown 10001:10001 /data
COPY --from=build /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH" \
    HIFISYNC_ACOUSTICS_DATA=/data \
    PYTHONUNBUFFERED=1
USER 10001:10001
VOLUME /data
EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/api/v1/health', timeout=3)"]
CMD ["hifisync-acoustics", "serve", "--host", "0.0.0.0", "--port", "8765"]
