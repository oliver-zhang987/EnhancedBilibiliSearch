FROM python:3.11-slim
WORKDIR /app

# Only what's needed to install + run (core is stdlib; server extra = fastapi/uvicorn).
COPY pyproject.toml README.md /app/
COPY ebsearch /app/ebsearch
RUN pip install --no-cache-dir ".[server]"

# Bind localhost only; nginx terminates TLS and reverse-proxies.
ENV EBS_HOST=127.0.0.1 EBS_PORT=8020
EXPOSE 8020
CMD ["python", "-m", "ebsearch.server.app"]
