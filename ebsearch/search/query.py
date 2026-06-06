"""Query preparation: turn a user TOPIC into an ordered list of SearchPlan.

Evidence-backed defaults (from live exploration on two topics):
  * A duration filter is the single biggest quality lever — it drops the multi-hour
    course/合集 dumps that pollute raw results and keeps summarizable explainers, at
    zero extra cost. So the default plan applies cfg.duration_filter.
  * LLM expansion (optional) gives the best precision + creator diversity.
  * Suggest (optional) is a cheap native-phrasing normalizer / expansion seed.
  * Multi-order (order=click) is exposed but OFF by default — it drifts off-topic into
    clips/fiction/memes.

Stdlib only. The no-LLM / no-network path (suggest+expand off) does zero I/O.
"""
from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Callable, List, Optional, Sequence

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
_REFERER = "https://www.bilibili.com/"
_SUGGEST_URL = "https://s.search.bilibili.com/main/suggest"
_EM = re.compile(r"</?em[^>]*>")
_WS = re.compile(r"\s+")


@dataclass
class SearchPlan:
    """One concrete search request for the WBI client to execute.

    duration: 0=all, 1=<10m, 2=10-30m, 3=30-60m, 4=>60m  (B站 `duration` param)
    """
    keyword: str
    order: str = "totalrank"
    duration: int = 0
    pages: int = 2
    origin: str = "raw"   # provenance: raw|suggest|expand|click


def normalize_topic(topic: str) -> str:
    return _WS.sub(" ", (topic or "").strip())


def _strip_em(s: str) -> str:
    return _EM.sub("", s or "")


def _dedup_keep_order(items: Sequence[str]) -> List[str]:
    seen, out = set(), []
    for it in items:
        it = normalize_topic(it)
        if it and it.lower() not in seen:
            seen.add(it.lower())
            out.append(it)
    return out


def fetch_suggestions(topic: str, cookie_header: str = "", limit: int = 3,
                      timeout: float = 8.0) -> List[str]:
    """B站-native query phrasings (no WBI needed). [] on any failure."""
    url = _SUGGEST_URL + "?" + urllib.parse.urlencode({"term": normalize_topic(topic)})
    headers = {"User-Agent": _UA, "Referer": _REFERER}
    if cookie_header:
        headers["Cookie"] = cookie_header
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
    except Exception:
        return []
    tags = (data.get("result") or {}).get("tag") or []
    return _dedup_keep_order([_strip_em(t.get("value", "")) for t in tags])[:limit]


_EXPAND_SYSTEM = "你是B站搜索专家，只输出JSON数组，不要任何解释。"
_EXPAND_USER = (
    "给定一个主题，生成{n}个适合在B站搜索框输入的中文检索词变体，"
    "覆盖该主题的不同常见叫法/细分角度，偏向能搜到讲解类/教程类/科普类视频。"
    "只输出JSON字符串数组，例如[\"词1\",\"词2\",\"词3\"]。主题：{topic}"
)

LLMComplete = Callable[[str, str], str]


def expand_queries(topic: str, llm_complete: LLMComplete, n: int = 3) -> List[str]:
    """Ask a cheap LLM for n B站-style query variants. [] on failure."""
    try:
        raw = llm_complete(_EXPAND_SYSTEM, _EXPAND_USER.format(n=n, topic=topic))
    except Exception:
        return []
    m = re.search(r"\[.*\]", raw or "", re.S)
    if not m:
        return []
    try:
        variants = json.loads(m.group(0))
    except Exception:
        return []
    return _dedup_keep_order([v for v in variants if isinstance(v, str)])[:n]


def build_plans(topic: str, cfg, llm_complete: Optional[LLMComplete] = None,
                cookie_header: str = "") -> List[SearchPlan]:
    """Ordered, deduplicated SearchPlans for *topic*. First plan is always the raw
    topic at the configured order/duration, so behavior degrades to a plain search."""
    base = normalize_topic(topic)
    plans: List[SearchPlan] = []
    seen = set()
    dur = getattr(cfg, "duration_filter", 0)

    def add(kw: str, origin: str, order: str, pages: int) -> None:
        kw = normalize_topic(kw)
        key = (kw.lower(), order, dur)
        if not kw or key in seen:
            return
        seen.add(key)
        plans.append(SearchPlan(keyword=kw, order=order, duration=dur,
                                pages=pages, origin=origin))

    add(base, "raw", getattr(cfg, "search_order", "totalrank"),
        getattr(cfg, "candidate_pages", 2))

    if getattr(cfg, "query_multi_order", False):
        add(base, "click", "click", getattr(cfg, "candidate_pages", 2))

    suggestions: List[str] = []
    if getattr(cfg, "query_suggest", False):
        suggestions = fetch_suggestions(base, cookie_header=cookie_header, limit=2)
        for s in suggestions:
            add(s, "suggest", getattr(cfg, "search_order", "totalrank"),
                getattr(cfg, "query_expand_pages", 1))

    if getattr(cfg, "query_expand", False) and llm_complete is not None:
        seed = base if not suggestions else "%s（亦称：%s）" % (base, suggestions[0])
        for v in expand_queries(seed, llm_complete, n=3):
            add(v, "expand", getattr(cfg, "search_order", "totalrank"),
                getattr(cfg, "query_expand_pages", 1))

    return plans
