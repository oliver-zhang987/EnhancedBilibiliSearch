FROM python:3.11-slim
WORKDIR /app

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
