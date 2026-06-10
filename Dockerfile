FROM python:3.11-slim
WORKDIR /app

# PyPI index override for slow/flaky routes (mainland servers pass
# --build-arg PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/).
ARG PIP_INDEX_URL=https://pypi.org/simple
ENV PIP_INDEX_URL=${PIP_INDEX_URL} PIP_DEFAULT_TIMEOUT=120 PIP_RETRIES=5

# Only what's needed to install + run (core is stdlib; server extra = fastapi/uvicorn).
COPY pyproject.toml README.md /app/
COPY ebsearch /app/ebsearch
RUN pip install --no-cache-dir ".[server]"

# Run as a non-root user with the SAME uid (1000) as the AIVideoSummary backend,
# so both services can read/write the shared SQLite account DB (mounted volume)
# without cross-uid ownership conflicts on the WAL files.
RUN useradd --create-home --uid 1000 --shell /bin/bash appuser \
    && chown -R appuser:appuser /app
USER appuser

# Bind localhost only; nginx terminates TLS and reverse-proxies.
ENV EBS_HOST=127.0.0.1 EBS_PORT=8020
EXPOSE 8020
CMD ["python", "-m", "ebsearch.server.app"]
