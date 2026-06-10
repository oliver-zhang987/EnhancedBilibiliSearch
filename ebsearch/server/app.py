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
  EBS_USER_REPORTS_PER_HOUR  per-user sliding-hour cap on /api/research
                      (default 6, 0 = disabled); exceeding it returns HTTP 429.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from ..account import balance as _credits_balance
from ..account import build_auth_router as _build_auth_router
from ..account import cost_report as _cost_report
from ..account import deduct as _credits_deduct
from ..account import init_db as _account_init_db
from ..account import refund as _credits_refund
from ..account import require_user as _require_user
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

    __slots__ = ("id", "topic", "owner", "user_id", "charged",
                 "status", "progress", "result", "error", "_lock")

    def __init__(self, job_id: str, topic: str, owner: str = "_shared",
                 user_id: Optional[int] = None, charged: int = 0) -> None:
        self.id = job_id
        self.topic = topic
        self.owner = owner               # per-user history partition (str(user_id))
        self.user_id = user_id           # who to bill/refund
        self.charged = int(charged)      # credits pre-authorized for this run
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


# --------------------------------------------------------------------------- #
# Per-user rate limit (anti-abuse): sliding-hour cap on report starts.
# --------------------------------------------------------------------------- #
class _UserRateLimiter:
    """Sliding-window limiter keyed by user id. Thread-safe; limit 0 = disabled."""

    def __init__(self, limit: int, window_seconds: float = 3600.0) -> None:
        self.limit = limit
        self._window = window_seconds
        self._lock = threading.Lock()
        self._stamps: Dict[str, List[float]] = {}

    def is_allowed(self, key: str) -> bool:
        if self.limit == 0:
            return True
        now = time.time()
        cutoff = now - self._window
        with self._lock:
            stamps = [t for t in self._stamps.get(key, []) if t >= cutoff]
            if len(stamps) >= self.limit:
                self._stamps[key] = stamps
                return False
            stamps.append(now)
            self._stamps[key] = stamps
            return True


# Re-initialised inside create_app so test reloads pick up the env var.
_USER_LIMITER = _UserRateLimiter(0)


# --------------------------------------------------------------------------- #
# History: persist finished reports to disk so they survive restarts. A small
# index file backs the list view; full records are one JSON per id.
# --------------------------------------------------------------------------- #
_DATA_DIR = Path(os.environ.get("EBS_DATA_DIR", "/app/data"))
_HIST_ROOT = _DATA_DIR / "history"      # per-owner subdirs underneath
_HIST_LOCK = threading.Lock()
_HIST_MAX = int(os.environ.get("EBS_HISTORY_MAX", "200"))
_ID_RE = re.compile(r"^[A-Za-z0-9]+$")  # guard against path traversal in {id}


def _owner_id(client_id: Optional[str]) -> str:
    """Stable, filesystem-safe per-client owner id so each user only sees their own
    history. Clients send a persistent X-Client-Id; missing ids share one bucket."""
    cid = (client_id or "").strip()
    if not cid:
        return "_shared"
    return hashlib.sha256(cid.encode("utf-8")).hexdigest()[:24]


def _owner_dir(owner: str) -> Path:
    return _HIST_ROOT / owner


def _owner_index(owner: str) -> Path:
    return _owner_dir(owner) / "index.json"


def _load_index(owner: str) -> List[Dict[str, Any]]:
    try:
        return json.loads(_owner_index(owner).read_text(encoding="utf-8"))
    except Exception:
        return []


def _hist_save(job: "_Job", result: Dict[str, Any]) -> None:
    """Write the finished report to the job owner's history (best-effort)."""
    try:
        owner = getattr(job, "owner", None) or "_shared"
        d = _owner_dir(owner)
        created = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        rec = {
            "id": job.id, "topic": job.topic, "created_at": created,
            "generated_at": (result.get("report") or {}).get("generated_at", ""),
            "n_candidates": result.get("n_candidates"),
            "n_selected": result.get("n_selected"),
            "n_summarized": result.get("n_summarized"),
            "markdown": result.get("markdown"), "report": result.get("report"),
        }
        meta = {k: rec[k] for k in ("id", "topic", "created_at", "generated_at", "n_summarized")}
        with _HIST_LOCK:
            d.mkdir(parents=True, exist_ok=True)
            (d / (job.id + ".json")).write_text(
                json.dumps(rec, ensure_ascii=False), encoding="utf-8")
            idx = [m for m in _load_index(owner) if m.get("id") != job.id]
            idx.insert(0, meta)
            for old in idx[_HIST_MAX:]:  # trim oldest beyond the cap
                try:
                    (d / (old["id"] + ".json")).unlink()
                except OSError:
                    pass
            idx = idx[:_HIST_MAX]
            _owner_index(owner).write_text(json.dumps(idx, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


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
        # Empty report produced nothing useful → refund (fair billing).
        if res.n_summarized == 0 and job.user_id is not None and job.charged > 0:
            _credits_refund(job.user_id, job.charged, "empty report refund", job.id)
            job.charged = 0
        _hist_save(job, job.result)
        job.status = "done"
    except Exception as e:  # surface to the client; keep the server alive
        job.error = "%s: %s" % (type(e).__name__, e)
        job.log("出错：%s" % job.error)
        # Refund the pre-authorized credits on failure.
        if job.user_id is not None and job.charged > 0:
            try:
                _credits_refund(job.user_id, job.charged, "report failed refund", job.id)
                job.charged = 0
            except Exception:
                pass
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
    global _USER_LIMITER
    _USER_LIMITER = _UserRateLimiter(
        int(os.environ.get("EBS_USER_REPORTS_PER_HOUR", "6") or "0"))

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

    # Account subsystem: same users.db + JWT secret as the AIVideoSummary backend,
    # so one login works across both products. Mounted at /api/auth/*.
    try:
        _account_init_db()
        app.include_router(_build_auth_router(), prefix="/api")
    except Exception as exc:  # pragma: no cover
        import logging
        logging.getLogger("ebsearch.server").warning("account subsystem unavailable: %s", exc)

    @app.get("/")
    def index() -> Any:
        if _INDEX_HTML.is_file():
            return FileResponse(str(_INDEX_HTML), media_type="text/html")
        raise HTTPException(status_code=404, detail="web/index.html not found")

    @app.get("/health")
    def health() -> Dict[str, bool]:
        return {"ok": True}

    @app.post("/api/research", status_code=202)
    def start_research(req: ResearchRequest,
                       user_id: int = Depends(_require_user)) -> Dict[str, str]:
        topic = (req.topic or "").strip()
        if not topic:
            raise HTTPException(status_code=400, detail="topic is required")
        # Anti-abuse: per-user sliding-hour quota, checked BEFORE any charge.
        if not _USER_LIMITER.is_allowed(str(user_id)):
            raise HTTPException(
                status_code=429,
                detail="请求过于频繁，请稍后再试（每小时上限 %d 次）" % _USER_LIMITER.limit,
            )
        # Credit gate: charge the report up front (base + per-video + pro-synth extra),
        # refunded on failure / empty result by _run_job.
        cfg = Config.from_env()
        n_videos = req.max_videos if req.max_videos else getattr(cfg, "max_videos", 4)
        cost = _cost_report(int(n_videos), getattr(cfg, "synth_model", None))
        job_id = uuid.uuid4().hex
        if not _credits_deduct(user_id, cost, "report preauth", job_id):
            raise HTTPException(
                status_code=402,
                detail={"error": "insufficient_credits",
                        "balance": _credits_balance(user_id), "required": cost},
            )
        job = _Job(job_id, topic, owner=str(user_id), user_id=user_id, charged=cost)
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

    @app.get("/api/research/{job_id}")
    def get_research(job_id: str, user_id: int = Depends(_require_user)) -> Any:
        job = _JOBS.get(job_id)
        # Only the user that started a job may poll it.
        if job is None or job.owner != str(user_id):
            raise HTTPException(status_code=404, detail="unknown job_id")
        return JSONResponse(job.snapshot())

    @app.get("/api/history")
    def history_list(user_id: int = Depends(_require_user)) -> Any:
        with _HIST_LOCK:
            return JSONResponse(_load_index(str(user_id)))

    @app.get("/api/history/{rec_id}")
    def history_get(rec_id: str, user_id: int = Depends(_require_user)) -> Any:
        if not _ID_RE.match(rec_id):
            raise HTTPException(status_code=404, detail="not found")
        p = _owner_dir(str(user_id)) / (rec_id + ".json")
        if not p.is_file():
            raise HTTPException(status_code=404, detail="not found")
        return JSONResponse(json.loads(p.read_text(encoding="utf-8")))

    @app.delete("/api/history/{rec_id}")
    def history_delete(rec_id: str, user_id: int = Depends(_require_user)) -> Dict[str, bool]:
        if not _ID_RE.match(rec_id):
            raise HTTPException(status_code=404, detail="not found")
        owner = str(user_id)
        with _HIST_LOCK:
            d = _owner_dir(owner)
            if d.is_dir():  # nothing to do if this client has no history
                try:
                    (d / (rec_id + ".json")).unlink()
                except OSError:
                    pass
                idx = [m for m in _load_index(owner) if m.get("id") != rec_id]
                _owner_index(owner).write_text(json.dumps(idx, ensure_ascii=False), encoding="utf-8")
        return {"ok": True}

    return app


app = create_app()


def main() -> None:
    import uvicorn

    host = os.environ.get("EBS_HOST", "0.0.0.0")
    port = int(os.environ.get("EBS_PORT", "8020"))
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
