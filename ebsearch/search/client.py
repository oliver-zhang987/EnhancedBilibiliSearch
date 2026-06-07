"""WBI-signed Bilibili search client (stdlib only).

Validated live against the real API:
  * search/type requires WBI signing + cookies (buvid3/SESSDATA) + UA + Referer.
  * `rank_score` is NULL in practice -> we rely on result ORDER (position).
  * Caps: numResults 1000 / 50 pages / 20 per page.
  * Junk rows (empty bvid, duration 0) and multi-hour course dumps must be filtered
    (the former here; the latter in rank.select).

Executes a list of SearchPlan (from query.build_plans), throttled, and returns a merged,
deduplicated list of VideoHit in first-seen (≈ relevance) order.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, List, Optional

from ..models import VideoHit
from .query import build_plans, SearchPlan

_MIXIN = [46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
          33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40, 61,
          26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36,
          20, 34, 44, 52]
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
_REFERER = "https://www.bilibili.com/"
_NAV = "https://api.bilibili.com/x/web-interface/nav"
_SEARCH = "https://api.bilibili.com/x/web-interface/wbi/search/type"
_VIEW = "https://api.bilibili.com/x/web-interface/view"
_PLAYER = "https://api.bilibili.com/x/player/wbi/v2"
_SUB_PREF = ["zh-Hans", "ai-zh", "zh-CN", "zh"]
_EM = re.compile(r"</?em[^>]*>")


def _strip_em(s: str) -> str:
    return _EM.sub("", s or "")


def _norm_url(u: str) -> str:
    return ("https:" + u) if u.startswith("//") else u


def _pick_sub(subs: list):
    """Return (subtitle_url, is_ai). Human subtitles are preferred over B站's AI
    auto-captions (lan starting with 'ai-'); (None, False) when there's none."""
    def _best(cands, prefs):
        for lan in prefs:
            for s in cands:
                if s.get("lan") == lan:
                    return _norm_url(s["subtitle_url"])
        return _norm_url(cands[0]["subtitle_url"]) if cands else None

    human = [s for s in subs if s.get("subtitle_url")
             and not str(s.get("lan", "")).startswith("ai-")]
    ai = [s for s in subs if s.get("subtitle_url")
          and str(s.get("lan", "")).startswith("ai-")]
    h = _best(human, ["zh-Hans", "zh-CN", "zh"])
    if h:
        return h, False
    a = _best(ai, ["ai-zh"])
    return (a, True) if a else (None, False)


def _parse_duration(s) -> int:
    try:
        sec = 0
        for part in str(s).split(":"):
            sec = sec * 60 + int(part)
        return sec
    except Exception:
        return 0


def _load_cookies(path: Optional[str]) -> Dict[str, str]:
    jar: Dict[str, str] = {}
    if not path:
        return jar
    try:
        with open(path) as f:
            for line in f:
                if line.startswith("#") or not line.strip():
                    continue
                p = line.rstrip("\n").split("\t")
                if len(p) >= 7 and "bilibili" in p[0]:
                    jar[p[5]] = p[6]
    except OSError:
        pass
    return jar


class BilibiliSearchClient:
    """Search Bilibili by topic. Construct once (caches WBI keys for the day)."""

    def __init__(self, cfg):
        self.cfg = cfg
        self._cookies = _load_cookies(getattr(cfg, "cookies_file", None))
        self._mixin_key: Optional[str] = None
        self._throttle = max(float(getattr(cfg, "request_delay_sec", 1.0)), 1.2)

    # --- low-level HTTP ---------------------------------------------------- #
    def cookie_header(self) -> str:
        return "; ".join("%s=%s" % (k, v) for k, v in self._cookies.items())

    def _headers(self) -> Dict[str, str]:
        h = {"User-Agent": _UA, "Referer": _REFERER}
        ch = self.cookie_header()
        if ch:
            h["Cookie"] = ch
        return h

    def _get_json(self, url: str, timeout: float = 20.0) -> dict:
        req = urllib.request.Request(url, headers=self._headers())
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))

    # --- WBI signing ------------------------------------------------------- #
    def _mixin(self) -> str:
        if self._mixin_key:
            return self._mixin_key
        d = self._get_json(_NAV)
        wi = d["data"]["wbi_img"]
        img = wi["img_url"].rsplit("/", 1)[-1].split(".")[0]
        sub = wi["sub_url"].rsplit("/", 1)[-1].split(".")[0]
        s = img + sub
        self._mixin_key = "".join(s[i] for i in _MIXIN)[:32]
        return self._mixin_key

    def _sign(self, params: dict) -> dict:
        params = dict(params)
        params["wts"] = int(time.time())
        san = lambda v: "".join(c for c in str(v) if c not in "!'()*")
        q = urllib.parse.urlencode([(k, san(v)) for k, v in sorted(params.items())],
                                   quote_via=urllib.parse.quote)
        params["w_rid"] = hashlib.md5((q + self._mixin()).encode()).hexdigest()
        return params

    # --- search ------------------------------------------------------------ #
    def _search_page(self, plan: SearchPlan, page: int) -> List[dict]:
        params = {"search_type": "video", "keyword": plan.keyword,
                  "order": plan.order, "page": page, "page_size": 20}
        if plan.duration:
            params["duration"] = plan.duration
        url = _SEARCH + "?" + urllib.parse.urlencode(self._sign(params),
                                                     quote_via=urllib.parse.quote)
        d = self._get_json(url)
        if d.get("code") != 0:
            return []
        return (d.get("data") or {}).get("result") or []

    def _to_hit(self, r: dict) -> Optional[VideoHit]:
        bvid = r.get("bvid")
        if not bvid:
            return None
        dur = _parse_duration(r.get("duration"))
        if dur <= 0:
            return None
        tag = r.get("tag") or ""
        tags = [t.strip() for t in tag.split(",") if t.strip()] if isinstance(tag, str) else []
        return VideoHit(
            bvid=bvid, aid=int(r.get("aid") or r.get("id") or 0),
            title=_strip_em(r.get("title")), author=r.get("author") or "",
            mid=int(r.get("mid") or 0), play=int(r.get("play") or 0),
            danmaku=int(r.get("video_review") or 0), favorites=int(r.get("favorites") or 0),
            review=int(r.get("review") or 0), pubdate=int(r.get("pubdate") or 0),
            duration_sec=dur, description=(r.get("description") or "")[:500],
            tags=tags, typename=r.get("typename") or "",
            rank_score=float(r.get("rank_score") or 0.0),
            hit_columns=list(r.get("hit_columns") or []),
            pic=(r.get("pic") or ""), url="https://www.bilibili.com/video/%s" % bvid,
        )

    def run_plans(self, plans: List[SearchPlan]) -> List[VideoHit]:
        hits: List[VideoHit] = []
        seen = set()
        first = True
        for plan in plans:
            for page in range(1, max(1, plan.pages) + 1):
                if not first:
                    time.sleep(self._throttle)
                first = False
                try:
                    rows = self._search_page(plan, page)
                except Exception:
                    rows = []
                for r in rows:
                    hit = self._to_hit(r)
                    if hit and hit.bvid not in seen:
                        seen.add(hit.bvid)
                        hits.append(hit)
        return hits

    def search_topic(self, topic: str,
                     llm_complete: Optional[Callable[[str, str], str]] = None) -> List[VideoHit]:
        plans = build_plans(topic, self.cfg, llm_complete=llm_complete,
                            cookie_header=self.cookie_header())
        return self.run_plans(plans)

    # --- subtitle fetch (avoids the backend's heavy yt-dlp download) -------- #
    def _download_subtitle(self, url: str) -> List[Dict[str, Any]]:
        req = urllib.request.Request(_norm_url(url),
                                     headers={"User-Agent": _UA, "Referer": _REFERER})
        with urllib.request.urlopen(req, timeout=20) as r:
            d = json.loads(r.read().decode("utf-8"))
        out = []
        for it in (d.get("body") or []):
            t = (it.get("content") or "").strip()
            if t:
                out.append({"start": float(it.get("from") or 0.0),
                            "end": float(it.get("to") or 0.0),
                            "text": t, "source": "subtitle"})
        return out

    def fetch_subtitle(self, bvid: str):
        """Fetch CC/AI subtitles for *bvid* via the WBI player API (light: a few API
        calls, no media download). Returns (segments, media_dict, kind) where kind is
        'human' | 'ai' | 'none' so the caller can prefer ASR over low-quality AI
        captions. Raises on network errors."""
        v = self._get_json(_VIEW + "?bvid=" + bvid)
        data = (v.get("data") or {}) if v.get("code") == 0 else {}
        cid = data.get("cid") or ((data.get("pages") or [{}])[0] or {}).get("cid")
        media = {
            "platform": "bilibili", "id": bvid,
            "url": "https://www.bilibili.com/video/%s" % bvid,
            "title": data.get("title", ""),
            "uploader": (data.get("owner") or {}).get("name", ""),
            "description": (data.get("desc") or "")[:500],
            "duration": float(data.get("duration") or 0.0),
            "language": "zh", "tags": [],
        }
        if not cid:
            return [], media, "none"
        params = self._sign({"bvid": bvid, "cid": cid})
        purl = _PLAYER + "?" + urllib.parse.urlencode(params, quote_via=urllib.parse.quote)
        p = self._get_json(purl)
        subs = (((p.get("data") or {}).get("subtitle") or {}).get("subtitles")) or []
        suburl, is_ai = _pick_sub(subs)
        if not suburl:
            return [], media, "none"
        return self._download_subtitle(suburl), media, ("ai" if is_ai else "human")
