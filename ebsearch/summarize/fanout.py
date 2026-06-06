"""Fan-out: summarize each selected video by REUSING the AIVideoSummary backend.

We call the deployed backend's ``POST /api/jobs`` (type=url) and poll. This inherits the
backend's Bilibili cookies, ASR relay, and content-addressed cache (re-running a topic
or a previously-seen video is then free). Stdlib only.

Cost note: a url job is subtitle-first and only falls back to ASR when a video has no
subtitle. `cfg.allow_asr` is recorded for intent; strict no-ASR enforcement would need a
backend flag (TODO) — for now the duration filter + selection are the main cost gates.
"""
from __future__ import annotations

import json
import time
import urllib.request
from typing import Dict, List, Optional

from ..models import ScoredHit, VideoSummary


def _post(url: str, body: dict, api_key: Optional[str], timeout: float = 30.0) -> dict:
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["X-API-Key"] = api_key
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"),
                                 headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _get(url: str, api_key: Optional[str], timeout: float = 30.0) -> dict:
    headers = {"X-API-Key": api_key} if api_key else {}
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def summarize_selected(
    selected: List[ScoredHit],
    cfg,
    *,
    poll_interval: float = 6.0,
    per_video_timeout: float = 420.0,
    logger=None,
) -> List[VideoSummary]:
    """Summarize each selected hit via the backend. Submits all jobs, then polls.

    Returns a VideoSummary per input (ok=False with an error on failure), in input order.
    """
    base = cfg.backend_url.rstrip("/")
    key = cfg.backend_api_key
    log = logger or (lambda *a, **k: None)

    # 1) submit
    jobs: Dict[str, dict] = {}   # bvid -> {hit, job_id, error}
    for sh in selected:
        hit = sh.hit
        try:
            resp = _post("%s/api/jobs" % base, {"type": "url", "url": hit.url}, key)
            jobs[hit.bvid] = {"hit": hit, "job_id": resp.get("job_id"), "error": None}
        except Exception as e:
            jobs[hit.bvid] = {"hit": hit, "job_id": None, "error": "submit:%s" % e}

    # 2) poll outstanding until done/error or timeout
    results: Dict[str, VideoSummary] = {}
    deadline = time.time() + per_video_timeout
    pending = {bv for bv, j in jobs.items() if j["job_id"]}
    # immediately record submit failures
    for bv, j in jobs.items():
        if not j["job_id"]:
            h = j["hit"]
            results[bv] = VideoSummary(bvid=bv, title=h.title, url=h.url,
                                       ok=False, error=j["error"])

    while pending and time.time() < deadline:
        time.sleep(poll_interval)
        for bv in list(pending):
            j = jobs[bv]
            try:
                st = _get("%s/api/jobs/%s" % (base, j["job_id"]), key)
            except Exception as e:
                continue  # transient; retry next round
            status = st.get("status")
            if status == "done":
                h = j["hit"]
                res = st.get("result") or {}
                summary = res.get("summary")
                src = ((res.get("transcript") or {}).get("origin") or "").split("+")[0]
                results[bv] = VideoSummary(bvid=bv, title=h.title, url=h.url,
                                           summary=summary, source=src,
                                           ok=bool(summary), error=None if summary else "empty")
                pending.discard(bv)
                log("summarized %s (%s)" % (bv, src or "?"))
            elif status == "error":
                h = j["hit"]
                results[bv] = VideoSummary(bvid=bv, title=h.title, url=h.url,
                                           ok=False, error=st.get("error") or "job error")
                pending.discard(bv)
                log("failed %s: %s" % (bv, st.get("error")))

    # timeouts
    for bv in pending:
        h = jobs[bv]["hit"]
        results[bv] = VideoSummary(bvid=bv, title=h.title, url=h.url,
                                   ok=False, error="timeout")

    return [results[sh.hit.bvid] for sh in selected if sh.hit.bvid in results]
