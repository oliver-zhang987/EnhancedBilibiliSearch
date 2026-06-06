"""FastAPI app: topic in -> background research job -> Markdown report out.

A thin delivery surface over :func:`ebsearch.pipeline.research`. Heavy lifting is the
core's; this module only:

  * runs ``research`` on a background thread (it blocks ~1-2 min),
  * streams the pipeline's logger lines into a per-job progress list,
  * exposes a small JSON API + serves the standalone web page,
  * optionally gates ``/api/*`` behind an ``X-API-Key`` header.

Jobs live in an in-process dict — fine for the intended single-worker deployment.
The core stays stdlib-only; FastAPI/uvicorn are the ``server`` optional-deps.

Env:
  EBS_SERVER_API_KEY  if set, ``/api/*`` requires header ``X-API-Key``.
  EBS_CORS_ORIGINS    comma-separated allowed origins (default ``*``).
  EBS_HOST/EBS_PORT   uvicorn bind (default 0.0.0.0:8020).
"""
from __future__ import annotations

import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from ..config import Config
from ..pipeline import research

# --------------------------------------------------------------------------- #
# Paths / static assets
# --------------------------------------------------------------------------- #
_WEB_DIR = Path(__file__).resolve().parent.parent / "web"
_INDEX_HTML = _WEB_DIR / "index.html"


# --------------------------------------------------------------------------- #
# In-process job registry
# --------------------------------------------------------------------------- #
class _Job:
    """A single research run. Mutated from the worker thread, read by the API."""

    __slots__ = ("id", "topic", "status", "progress", "result", "error", "_lock")

    def __init__(self, job_id: str, topic: str) -> None:
        self.id = job_id
        self.topic = topic
        self.status = "pending"          # pending | running | done | error
        self.progress: List[str] = []
        self.result: Optional[Dict[str, Any]] = None
        self.error: Optional[str] = None
        self._lock = threading.Lock()

    def log(self, line: str) -> None:
        with self._lock:
            self.progress.append(str(line))

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            out: Dict[str, Any] = {
                "status": self.status,
                "progress": list(self.progress),
            }
            if self.status == "done" and self.result is not None:
                out.update(self.result)
            if self.status == "error":
                out["error"] = self.error or "unknown error"
            return out


_JOBS: Dict[str, _Job] = {}
_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="ebs-research")


def _run_job(job: _Job, overrides: Dict[str, Any]) -> None:
    job.status = "running"
    job.log("任务已启动…")
    try:
        cfg = Config.from_env()
        for k, v in overrides.items():
            if v is not None and hasattr(cfg, k):
                setattr(cfg, k, v)
        res = research(job.topic, cfg, logger=job.log)
        job.result = {
            "markdown": res.markdown,
            "report": res.report.to_dict(),
            "n_candidates": res.n_candidates,
            "n_selected": res.n_selected,
            "n_summarized": res.n_summarized,
            "stages": res.stages,
        }
        job.log(
            "完成：%d 候选 → %d 入选 → %d 摘要"
            % (res.n_candidates, res.n_selected, res.n_summarized)
        )
        job.status = "done"
    except Exception as e:  # surface to the client; keep the server alive
        job.error = "%s: %s" % (type(e).__name__, e)
        job.log("出错：%s" % job.error)
        job.status = "error"


# --------------------------------------------------------------------------- #
# Request / response models
# --------------------------------------------------------------------------- #
class ResearchRequest(BaseModel):
    topic: str
    max_videos: Optional[int] = None
    duration_filter: Optional[int] = None
    allow_asr: Optional[bool] = None
    query_expand: Optional[bool] = None
    query_suggest: Optional[bool] = None
    allow_llm_rerank: Optional[bool] = None


# --------------------------------------------------------------------------- #
# Auth dependency (optional, env-gated)
# --------------------------------------------------------------------------- #
def _require_api_key(x_api_key: Optional[str] = Header(default=None)) -> None:
    expected = os.environ.get("EBS_SERVER_API_KEY")
    if expected and x_api_key != expected:
        raise HTTPException(status_code=401, detail="invalid or missing X-API-Key")


# --------------------------------------------------------------------------- #
# App factory
# --------------------------------------------------------------------------- #
def create_app() -> FastAPI:
    app = FastAPI(title="EnhancedBilibiliSearch", version="0.1.0")

    origins_raw = os.environ.get("EBS_CORS_ORIGINS", "*").strip()
    origins = ["*"] if origins_raw in ("", "*") else [
        o.strip() for o in origins_raw.split(",") if o.strip()
    ]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/")
    def index() -> Any:
        if _INDEX_HTML.is_file():
            return FileResponse(str(_INDEX_HTML), media_type="text/html")
        raise HTTPException(status_code=404, detail="web/index.html not found")

    @app.get("/health")
    def health() -> Dict[str, bool]:
        return {"ok": True}

    @app.post("/api/research", status_code=202, dependencies=[Depends(_require_api_key)])
    def start_research(req: ResearchRequest) -> Dict[str, str]:
        topic = (req.topic or "").strip()
        if not topic:
            raise HTTPException(status_code=400, detail="topic is required")
        job_id = uuid.uuid4().hex
        job = _Job(job_id, topic)
        _JOBS[job_id] = job
        overrides = {
            "max_videos": req.max_videos,
            "duration_filter": req.duration_filter,
            "allow_asr": req.allow_asr,
            "query_expand": req.query_expand,
            "query_suggest": req.query_suggest,
            "allow_llm_rerank": req.allow_llm_rerank,
        }
        _EXECUTOR.submit(_run_job, job, overrides)
        return {"job_id": job_id}

    @app.get("/api/research/{job_id}", dependencies=[Depends(_require_api_key)])
    def get_research(job_id: str) -> Any:
        job = _JOBS.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="unknown job_id")
        return JSONResponse(job.snapshot())

    return app


app = create_app()


def main() -> None:
    import uvicorn

    host = os.environ.get("EBS_HOST", "0.0.0.0")
    port = int(os.environ.get("EBS_PORT", "8020"))
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
