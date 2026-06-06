"""Select the top-N most relevant + substantive + summarizable videos from a pool.

Design (validated against a live B站 fixture for "大语言模型 RAG 检索增强"):

  * The search API's ``rank_score`` is NULL/absent in practice, so we DO NOT use it.
    The real relevance signal is the *result order* (``pos`` = the index B站 returned
    the hit in), reinforced by ``hit_columns`` strength, ``play``, recency, and a
    duration "fit" window.
  * Junk (empty bvid / duration 0) and giant course compilations (multi-hour) are not
    cheaply summarizable, so they are filtered or down-ranked.

Two layers, both cheap and the second optional:

  1. ``select`` — a transparent, deterministic, dependency-light heuristic. No network,
     no LLM. This is the default and always runs (it produces the hard prefilter + the
     weighted score).
  2. ``llm_rerank`` — an optional re-rank of the heuristic survivors by a *cheap* LLM
     (e.g. deepseek-v4-flash). One call, structured JSON in/out, and it degrades
     gracefully to the heuristic order on any error.

The function contract matches ``ebsearch.models`` (VideoHit, ScoredHit) and
``ebsearch.config.Config``.
"""
from __future__ import annotations

import json
import math
import re
from typing import List, Optional, Sequence

from ..models import VideoHit, ScoredHit

try:  # config import is best-effort so this module stays usable in isolation/tests
    from ..config import Config
except Exception:  # pragma: no cover
    Config = object  # type: ignore


# --------------------------------------------------------------------------- #
# Tunables for the heuristic. Weights sum is irrelevant (scores are relative); the
# ratios are what matter. Chosen by inspecting the fixture (see module notes / the
# exploration write-up). Override via Config if/when these graduate to knobs.
# --------------------------------------------------------------------------- #
W_POSITION = 0.34   # trust B站's relevance order (its strongest honest signal)
W_HITCOL = 0.20   # title/tag matches >> description-only >> none
W_PLAY = 0.16   # popularity / vetting, but log-damped and capped
W_RECENCY = 0.12   # fast-moving field; favor fresh but don't tyrannize
W_DURATION = 0.18   # "is this a summarizable explainer, not a clip or a course?"

# Duration fit (seconds): a soft bell centered on a typical explainer length.
DUR_IDEAL = 1200      # ~20 min: long enough to be substantive, short enough to summarize
DUR_SIGMA = 900       # spread of the bell
DUR_HARD_MAX = 3600   # > 60 min => treat as a course; heavy penalty (see COURSE_PENALTY)
COURSE_PENALTY = 0.45  # multiplier applied to the final score for course-length videos

# Recency: half-life in days for an exponential decay on age.
RECENCY_HALFLIFE_DAYS = 540.0   # ~18 months; RAG content older than that is stale-ish

# Off-topic guard: if the title/tags don't mention the topic's core tokens at all and
# B站 didn't flag a title/tag hit, the result is probably a tangential popular video.
TOPIC_TOKEN_MIN_HITS = 1


def _now() -> float:
    import time
    return time.time()


def _norm_text(s: str) -> str:
    return (s or "").lower()


def _topic_tokens(topic: str) -> List[str]:
    """Split a topic into meaningful tokens for a cheap on-topic check.

    Handles mixed CN/EN: keeps ASCII words (>=2 chars) and CJK bigrams/unigrams.
    """
    topic = topic or ""
    toks: List[str] = []
    # ASCII words / acronyms, e.g. "rag", "llm".
    for m in re.findall(r"[a-zA-Z0-9]{2,}", topic):
        toks.append(m.lower())
    # CJK runs -> keep the whole run and 2-grams so "检索增强" matches partial titles.
    for run in re.findall(r"[一-鿿]{2,}", topic):
        toks.append(run)
        for i in range(len(run) - 1):
            toks.append(run[i:i + 2])
    # de-dupe, drop ultra-generic single tokens
    seen, out = set(), []
    for t in toks:
        if t in seen:
            continue
        seen.add(t)
        out.append(t)
    return out


def _topic_hits(hit: VideoHit, tokens: Sequence[str]) -> int:
    hay = _norm_text(hit.title) + " " + " ".join(_norm_text(t) for t in (hit.tags or []))
    return sum(1 for t in tokens if t and t in hay)


def _hitcol_strength(hit_columns: Sequence[str]) -> float:
    """0..1 strength of where B站 matched the query. title/tag >> description >> author."""
    cols = {c.lower() for c in (hit_columns or [])}
    if not cols:
        return 0.0
    s = 0.0
    if "title" in cols:
        s += 0.6
    if "tag" in cols:
        s += 0.25
    if "description" in cols:
        s += 0.1
    if "author" in cols:
        s += 0.05
    return min(1.0, s)


def _duration_fit(duration_sec: int) -> float:
    """Bell curve in [0,1] peaking at DUR_IDEAL. Very short clips and very long
    courses both score low. Returns 0 for nonsense durations."""
    if duration_sec <= 0:
        return 0.0
    d = float(duration_sec)
    return math.exp(-((d - DUR_IDEAL) ** 2) / (2.0 * DUR_SIGMA ** 2))


def _recency(pubdate: int, now: float) -> float:
    if pubdate <= 0:
        return 0.5  # unknown date: neutral
    age_days = max(0.0, (now - pubdate) / 86400.0)
    return 0.5 ** (age_days / RECENCY_HALFLIFE_DAYS)


def _position_decay(pos: int, n: int) -> float:
    """Rank-reciprocal-ish decay in [0,1]. Earlier results score higher; flat tail."""
    if pos < 0:
        pos = n  # missing position => treat as last
    return 1.0 / (1.0 + 0.12 * pos)


def _log_play(play: int, pool_max_play: int) -> float:
    """log-damped, normalized to the pool's max so a 2.5M-view course can't dominate."""
    if play <= 0:
        return 0.0
    cap = max(play, pool_max_play, 1)
    return math.log10(1 + play) / math.log10(1 + cap)


# --------------------------------------------------------------------------- #
# Hard prefilter
# --------------------------------------------------------------------------- #
def _passes_hard_filters(
    hit: VideoHit,
    cfg,
    tokens: Sequence[str],
    max_duration_sec: int,
) -> Optional[str]:
    """Return a rejection reason string, or None if the hit passes."""
    if not hit.bvid:
        return "no_bvid"
    if hit.duration_sec <= 0:
        return "no_duration"
    if hit.duration_sec < getattr(cfg, "min_duration_sec", 120):
        return "too_short"
    if hit.duration_sec > max_duration_sec:
        return "too_long_course"
    if hit.play < getattr(cfg, "min_play", 1000):
        return "low_play"
    # Off-topic guard: no title/tag token match AND B站 didn't flag a title/tag hit.
    cols = {c.lower() for c in (hit.hit_columns or [])}
    title_tag_hit = bool(cols & {"title", "tag"})
    if not title_tag_hit and _topic_hits(hit, tokens) < TOPIC_TOKEN_MIN_HITS:
        return "off_topic"
    return None


# --------------------------------------------------------------------------- #
# Heuristic scoring
# --------------------------------------------------------------------------- #
def _score_hit(hit: VideoHit, n: int, now: float, pool_max_play: int) -> ScoredHit:
    pos_s = _position_decay(getattr(hit, "_pos", -1) if hasattr(hit, "_pos") else -1, n)
    # `pos` is carried on the hit via the reasons dict by callers that have it; but the
    # canonical VideoHit has no pos field, so we read it from an attribute if present.
    hc_s = _hitcol_strength(hit.hit_columns)
    play_s = _log_play(hit.play, pool_max_play)
    rec_s = _recency(hit.pubdate, now)
    dur_s = _duration_fit(hit.duration_sec)

    raw = (
        W_POSITION * pos_s
        + W_HITCOL * hc_s
        + W_PLAY * play_s
        + W_RECENCY * rec_s
        + W_DURATION * dur_s
    )
    # Course-length videos (already past the hard filter only if the caller widened the
    # window) get an explicit multiplicative penalty so they sink below explainers.
    course = hit.duration_sec > DUR_HARD_MAX
    score = raw * (COURSE_PENALTY if course else 1.0)

    reasons = {
        "position": round(pos_s, 3),
        "hit_columns": round(hc_s, 3),
        "play": round(play_s, 3),
        "recency": round(rec_s, 3),
        "duration_fit": round(dur_s, 3),
        "course_penalty": course,
        "components": {
            "W_POSITION": W_POSITION, "W_HITCOL": W_HITCOL, "W_PLAY": W_PLAY,
            "W_RECENCY": W_RECENCY, "W_DURATION": W_DURATION,
        },
    }
    return ScoredHit(hit=hit, score=round(score, 4), reasons=reasons)


def select(
    hits: List[VideoHit],
    cfg: "Config",
    topic: str,
    *,
    use_llm: Optional[bool] = None,
    llm_call=None,
) -> List[ScoredHit]:
    """Pick the top ``cfg.max_videos`` videos to summarize for ``topic``.

    Pure-heuristic by default (no network). The pipeline is:

        hard prefilter (junk / too short / too long / low play / off-topic)
          -> transparent weighted heuristic score (position, hit_columns, play,
             recency, duration-fit)
          -> [optional] cheap-LLM re-rank of the survivors
          -> top-N

    The result order is preserved from highest to lowest score. ``hits`` is expected to
    be in B站 result order (index 0 == first result); that index is used as the position
    signal. If a hit carries an integer ``pos`` attribute it is used instead.

    Args:
        hits: pool of VideoHit in B站 result order.
        cfg:  Config (uses ``max_videos``, ``min_play``, ``min_duration_sec`` and the
              optional ``max_duration_sec`` / ``allow_llm_rerank`` if present).
        topic: the search topic, used for the on-topic guard and LLM rerank.
        use_llm: force LLM rerank on/off; default reads ``cfg.allow_llm_rerank`` (False).
        llm_call: optional ``callable(prompt:str)->str`` returning the model's text.
                  Injected so this module has no transport dependency. If None and
                  ``use_llm`` is True, the LLM step is skipped (heuristic order kept).

    Returns:
        list[ScoredHit] of length <= cfg.max_videos, best first.
    """
    n = len(hits)
    if n == 0:
        return []
    now = _now()
    max_videos = int(getattr(cfg, "max_videos", 6))
    # Default course cutoff = cfg.max_duration_sec if present, else DUR_HARD_MAX.
    max_duration_sec = int(getattr(cfg, "max_duration_sec", DUR_HARD_MAX) or DUR_HARD_MAX)
    tokens = _topic_tokens(topic)
    pool_max_play = max((h.play for h in hits), default=1)

    # Stamp positional rank onto each hit (input order is the relevance order).
    survivors: List[ScoredHit] = []
    for idx, h in enumerate(hits):
        pos = getattr(h, "pos", None)
        if not isinstance(pos, int):
            pos = idx
        # carry pos for scoring without mutating the dataclass schema
        try:
            object.__setattr__(h, "_pos", pos)
        except Exception:
            pass
        reason = _passes_hard_filters(h, cfg, tokens, max_duration_sec)
        if reason is not None:
            continue
        sh = _score_hit(h, n, now, pool_max_play)
        sh.reasons["pos"] = pos
        survivors.append(sh)

    survivors.sort(key=lambda s: s.score, reverse=True)

    # Optional cheap-LLM rerank over a small candidate set (cost discipline: only the
    # heuristic top slice is sent, never the whole pool).
    want_llm = use_llm if use_llm is not None else bool(getattr(cfg, "allow_llm_rerank", False))
    if want_llm and llm_call is not None and survivors:
        cand = survivors[: max(max_videos * 2, max_videos + 4)]
        reranked = _llm_rerank(cand, topic, max_videos, llm_call)
        if reranked:
            survivors = reranked + [s for s in survivors if s not in reranked]

    return survivors[:max_videos]


# --------------------------------------------------------------------------- #
# Optional cheap-LLM rerank (one call, structured, fails safe)
# --------------------------------------------------------------------------- #
def build_rerank_prompt(cands: Sequence[ScoredHit], topic: str, top_n: int) -> str:
    """Build a compact prompt: titles + truncated descriptions + key stats."""
    lines = [
        f"任务：从下面的B站视频候选中，为主题《{topic}》挑选最相关、信息量最大、"
        f"且适合做成总结报告的 {top_n} 个视频。",
        "优先：紧扣主题的原理/综述/实战讲解；排除：跑题、纯营销、超长课程合集、"
        "重复内容。",
        "",
        "候选（编号 | 时长分钟 | 播放 | 标题 | 简介节选）：",
    ]
    for i, sh in enumerate(cands):
        h = sh.hit
        desc = (h.description or "").replace("\n", " ").strip()[:120]
        lines.append(
            f"[{i}] {h.duration_sec // 60}分 | {h.play}播放 | {h.title[:60]} | {desc}"
        )
    lines += [
        "",
        f"只输出 JSON，不要解释。格式："
        f'{{"picks":[{{"i":<编号>,"reason":"<一句话理由>"}}]}}，'
        f"按相关度从高到低，最多 {top_n} 个。",
    ]
    return "\n".join(lines)


def _llm_rerank(
    cands: Sequence[ScoredHit], topic: str, top_n: int, llm_call
) -> List[ScoredHit]:
    """Call ``llm_call(prompt)`` once, parse picks, reorder. Fail-safe -> []."""
    try:
        prompt = build_rerank_prompt(cands, topic, top_n)
        raw = llm_call(prompt)
        if not raw:
            return []
        data = _extract_json(raw)
        picks = data.get("picks") if isinstance(data, dict) else None
        if not picks:
            return []
        ordered: List[ScoredHit] = []
        seen = set()
        for p in picks:
            i = p.get("i") if isinstance(p, dict) else None
            if not isinstance(i, int) or i < 0 or i >= len(cands) or i in seen:
                continue
            seen.add(i)
            sh = cands[i]
            sh.reasons["llm_reason"] = (p.get("reason") or "")[:160]
            sh.reasons["llm_rank"] = len(ordered)
            ordered.append(sh)
            if len(ordered) >= top_n:
                break
        return ordered
    except Exception:
        return []  # any failure: keep the deterministic heuristic order


def _extract_json(text: str) -> dict:
    """Pull the first JSON object out of a model response (handles ```json fences)."""
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = re.sub(r"^json\s*", "", text, flags=re.IGNORECASE).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return {}
    try:
        return json.loads(text[start : end + 1])
    except Exception:
        return {}
