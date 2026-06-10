"""Fan-out: summarize each selected video by REUSING the AIVideoSummary backend.

We call the deployed backend's ``POST /api/jobs`` (type=url) and poll. This inherits the
backend's Bilibili cookies, ASR relay, and content-addressed cache (re-running a topic
or a previously-seen video is then free). Stdlib only.

Subtitle policy (cfg.allow_asr): human subtitles are always used (free + best). When
allow_asr is on, videos with only AI captions are re-transcribed via a `force_asr` url
job (the backend skips its subtitle-first path and runs Whisper). When off, AI captions
are reused as-is. The duration filter + selection remain the main cost gates.
"""
from __future__ import annotations

import json
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
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
    client=None,
    poll_interval: float = 6.0,
    per_video_timeout: float = 420.0,
    fetch_delay: float = 1.0,
    submit_workers: int = 4,
    logger=None,
) -> List[VideoSummary]:
    """Summarize each selected hit via the backend. Submits all jobs, then polls.

    To avoid Bilibili risk-control (HTTP 412) from the backend's per-video yt-dlp
    download, we fetch each video's subtitle here (light WBI API calls) and submit a
    `transcript` job; only videos with no subtitle fall back to a `url` (ASR) job, and
    only when cfg.allow_asr is set. Subtitle-fetch + submission run concurrently across
    videos (capped pool), and polling starts fast then backs off, so the wall-clock is
    dominated by the slowest single video rather than the sum. Returns one VideoSummary
    per input.
    """
    base = cfg.backend_url.rstrip("/")
    key = cfg.backend_api_key
    log = logger or (lambda *a, **k: None)
    allow_asr = getattr(cfg, "allow_asr", False)

    # 1) submit — subtitle -> transcript job; else (opt-in) ASR url job.
    # Fetch + submit run in parallel across videos (the old serial 1s-per-video loop
    # was pure latency). The pool is capped to stay gentle on Bilibili's WBI endpoint.
    def _submit_one(sh: ScoredHit):
        hit = sh.hit
        segs, media, kind = [], None, "none"   # kind: human | ai | none
        if client is not None:
            try:
                segs, media, kind = client.fetch_subtitle(hit.bvid)
            except Exception as e:
                log("subtitle fetch failed %s: %s" % (hit.bvid, e))
                segs, kind = [], "none"
        try:
            if kind == "human" and segs:
                # human subtitle: best quality + free, always use it
                resp = _post("%s/api/jobs" % base,
                             {"type": "transcript", "media": media, "segments": segs}, key)
                return hit.bvid, {"hit": hit, "job_id": resp.get("job_id"), "error": None}
            elif allow_asr:
                # only AI captions (or none): re-transcribe with whisper for quality
                # (force_asr makes the backend skip subtitles instead of reusing them)
                resp = _post("%s/api/jobs" % base,
                             {"type": "url", "url": hit.url, "force_asr": True}, key)
                return hit.bvid, {"hit": hit, "job_id": resp.get("job_id"), "error": None}
            elif segs:
                # AI captions, ASR off: use them rather than nothing
                resp = _post("%s/api/jobs" % base,
                             {"type": "transcript", "media": media, "segments": segs}, key)
                return hit.bvid, {"hit": hit, "job_id": resp.get("job_id"), "error": None}
            else:
                return hit.bvid, {"hit": hit, "job_id": None, "error": "无字幕（未开启ASR）"}
        except Exception as e:
            # HTTP 451 = the backend's creator opt-out blocklist (legal takedown).
            # Skip just this video; the rest of the report proceeds without it.
            # (_post uses urllib, so this surfaces as urllib.error.HTTPError —
            # detected via .code to stay agnostic of the exact exception type.)
            if getattr(e, "code", None) == 451:
                log("skipped %s: creator opt-out (451)" % hit.bvid)
                return hit.bvid, {"hit": hit, "job_id": None, "error": "已按创作者要求跳过"}
            return hit.bvid, {"hit": hit, "job_id": None, "error": "submit:%s" % e}

    jobs: Dict[str, dict] = {}   # bvid -> {hit, job_id, error}
    workers = max(1, min(submit_workers, len(selected) or 1))
    if workers == 1 or len(selected) <= 1:
        for sh in selected:
            bv, info = _submit_one(sh)
            jobs[bv] = info
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for bv, info in pool.map(_submit_one, selected):
                jobs[bv] = info

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

    round_i = 0
    while pending and time.time() < deadline:
        # adaptive cadence: poll quickly at first (cache hits finish in seconds),
        # then settle to poll_interval to avoid hammering the backend.
        time.sleep(1.0 if round_i < 3 else (3.0 if round_i < 6 else poll_interval))
        round_i += 1
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
