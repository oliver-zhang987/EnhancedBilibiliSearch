"""Synthesize N per-video summaries on one topic into a single ``TopicReport``.

Design (chosen after comparing three report shapes on a real fixture for
"大语言模型 RAG 检索增强"; see the exploration write-up):

  * **Structured sections** win over a comparison-matrix or a flowing brief. They map
    1:1 onto :class:`TopicReport`, force the model to separate *consensus* from
    *disagreement*, and make provenance trivially checkable per claim. (A matrix is
    great for an at-a-glance view and we keep it as a *render* affordance, but the
    matrix-only prompt drops nuance and the narrative-only prompt buries the dissenting
    view.)

  * **Faithfulness is structural, not hopeful.** We send the model a compact, fully
    self-contained *evidence pack* (only the per-video tldr / key_points / keywords /
    chapter bullets + the bvid that owns each), and the prompt forbids any fact not in
    it. Every concrete claim must carry a 〔BVxxxx〕 provenance tag. Timestamps may only
    come from chapter ``start`` values, so the model cannot invent them.

  * **Cost discipline.** Exactly one strong-model call (``cfg.synth_model``). Transport
    is injected (``llm_call``) or, if absent, a tiny OpenAI-compatible client is built
    from ``cfg`` — mirroring how ``rank.select`` keeps the module transport-light.

  * **Degrade gracefully.** If the model returns prose or broken JSON, we still produce
    a usable ``TopicReport`` (overview from the best-effort text + a deterministic
    per-video / sources / watch_list skeleton built straight from the summaries), so
    the pipeline never hard-fails on a flaky completion.

The input ``summaries`` accepts either the fixture shape
``{bvid: {ok, status, summary: {...}}}`` or a list of ``VideoSummary``-like dicts; both
are normalized internally.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from ..models import TopicReport

try:  # keep importable in isolation / tests
    from ..config import Config
except Exception:  # pragma: no cover
    Config = object  # type: ignore


# --------------------------------------------------------------------------- #
# Evidence pack: the *only* thing the model is allowed to use.
# --------------------------------------------------------------------------- #
def _mmss(sec: float) -> str:
    s = max(0, int(sec or 0))
    return f"{s // 60:02d}:{s % 60:02d}"


def _video_url(bvid: str) -> str:
    return f"https://www.bilibili.com/video/{bvid}"


def _normalize_summaries(summaries: Any) -> List[Dict[str, Any]]:
    """Accept the fixture dict shape or a list; return a flat list of records with a
    guaranteed ``bvid`` and an inner ``summary`` payload. Skips failed/empty entries."""
    items: List[Dict[str, Any]] = []
    if isinstance(summaries, dict):
        for bvid, rec in summaries.items():
            rec = rec or {}
            if rec.get("ok") is False:
                continue
            inner = rec.get("summary") or rec  # tolerate already-flattened
            items.append({"bvid": rec.get("bvid") or bvid, "summary": inner,
                          "title": rec.get("title", ""), "url": rec.get("url", "")})
    elif isinstance(summaries, (list, tuple)):
        for rec in summaries:
            rec = rec or {}
            if rec.get("ok") is False:
                continue
            inner = rec.get("summary") or rec
            bvid = rec.get("bvid") or (inner.get("media") or {}).get("id") or ""
            items.append({"bvid": bvid, "summary": inner,
                          "title": rec.get("title", ""), "url": rec.get("url", "")})
    return [it for it in items if it.get("bvid") and isinstance(it.get("summary"), dict)]


def build_evidence(topic: str, summaries: Any) -> Dict[str, Any]:
    """Build the compact, attributable evidence pack handed to the model."""
    pack: Dict[str, Any] = {"topic": topic, "videos": []}
    for it in _normalize_summaries(summaries):
        bvid = it["bvid"]
        s = it["summary"]
        media = s.get("media") or {}
        duration = media.get("duration") or 0
        chapters = []
        for ch in (s.get("chapters") or [])[:10]:
            # chapter titles + timestamps only (bullets dropped to keep the evidence
            # pack small enough for tight provider token-per-minute limits)
            chapters.append({
                "title": ch.get("title", ""),
                "t": _mmss(ch.get("start", 0)),
                "start_sec": int(ch.get("start", 0) or 0),
            })
        pack["videos"].append({
            "bvid": bvid,
            "title": media.get("title") or it.get("title") or "",
            "author": media.get("uploader") or "",
            "duration_min": round((duration or 0) / 60, 1),
            "url": media.get("url") or it.get("url") or _video_url(bvid),
            "tldr": s.get("tldr", ""),
            "key_points": (s.get("key_points") or [])[:6],
            "keywords": (s.get("keywords") or [])[:8],
            "chapters": chapters,
        })
    return pack


# --------------------------------------------------------------------------- #
# Prompt (strict provenance contract). Returns (system, user).
# --------------------------------------------------------------------------- #
_RULES = (
    "严格规则（必须遵守）：\n"
    "1. 只能使用【证据包】中的内容。禁止引入证据包之外的任何事实、数字、模型名或观点。\n"
    "2. 每一条具体论断后都必须标注来源视频，格式〔BVxxxx〕；多个视频支持就并列，"
    "如〔BV1JLN2z4EZQ；BV1pSpWz2ES5〕。\n"
    "3. 概览可综合多个视频，但其中每个具体说法仍须可被某个视频支撑。\n"
    "4. 若某观点只有一个视频提出（尤其与其它视频相左），必须明确标为少数/异见观点并注明视频。\n"
    "5. 不要编造时间戳；时间戳只能取自证据包 chapters 的 t/start_sec。\n"
    "6. 用简体中文输出。\n"
)

_SYSTEM = (
    "你是一名严谨的研究助理，负责把同一主题下多个B站视频的逐条要点汇总成一份"
    "可信、可追溯、对研究者真正有用的报告。绝不臆造，绝不超出给定证据。"
)


def build_synth_prompt(topic: str, evidence: Dict[str, Any]) -> Dict[str, str]:
    """Return ``{"system": ..., "user": ...}`` for the structured-JSON design."""
    ev_str = json.dumps(evidence, ensure_ascii=False)
    user = (
        f"主题：《{topic}》\n\n"
        "下面是该主题下若干视频的逐条要点【证据包】（JSON）：\n"
        f"{ev_str}\n\n"
        + _RULES +
        "\n请只输出一个 JSON 对象（不要解释、不要代码围栏），结构如下：\n"
        "{\n"
        '  "overview": "5-7句话总览：该主题整体图景 + 这批视频覆盖范围",\n'
        '  "themes": [ {"title":"子主题","summary":"各视频在此子主题讲了什么（带〔BV〕）",'
        '"video_bvids":["BV..."]} ],\n'
        '  "consensus": ["多个视频一致认同的结论，每条带〔BV；BV〕"],\n'
        '  "disagreements": ["分歧或单一视频的异见，写清谁的观点〔BV〕及与谁/与主流的差异"],\n'
        '  "per_video": [ {"bvid":"BV...","title":"...","url":"...",'
        '"highlights":["最值得记住的2-3点"],'
        '"timestamps":[{"t":"mm:ss","start_sec":int,"label":"该处讲了什么"}]} ],\n'
        '  "watch_list": [ {"bvid":"BV...","title":"...","rank":1,"reason":"为何先看/适合谁"} ],\n'
        '  "gaps": ["这批视频未覆盖、但研究该主题应了解的方面"]\n'
        "}\n"
        "themes 取 3-5 个；per_video 覆盖全部视频，每个挑 2-3 个最有信息量的时间戳；"
        "watch_list 给出全部视频的推荐顺序与理由。"
    )
    return {"system": _SYSTEM, "user": user}


# --------------------------------------------------------------------------- #
# Robust JSON extraction (fence-aware; mirrors rank.select._extract_json).
# --------------------------------------------------------------------------- #
def _extract_json(text: str) -> dict:
    if not text:
        return {}
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = re.sub(r"^json\s*", "", text, flags=re.IGNORECASE).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return {}
    blob = text[start:end + 1]
    try:
        return json.loads(blob)
    except Exception:
        # last-ditch: strip trailing commas, retry once
        try:
            return json.loads(re.sub(r",\s*([}\]])", r"\1", blob))
        except Exception:
            return {}


# --------------------------------------------------------------------------- #
# Transport: injected llm_call, or a minimal OpenAI-compatible client from cfg.
# --------------------------------------------------------------------------- #
def _default_llm_call(cfg: "Config") -> Callable[[str, str], str]:
    """Build a one-shot ``(system, user) -> text`` caller from an OpenAI-compatible
    endpoint. Imports the SDK lazily so the module loads without it installed."""
    from openai import OpenAI  # lazy

    client = OpenAI(
        api_key=getattr(cfg, "llm_api_key", None),
        base_url=getattr(cfg, "llm_base_url", None) or "https://api.deepseek.com",
    )
    model = getattr(cfg, "synth_model", "deepseek-v4-pro")

    def _call(system: str, user: str) -> str:
        resp = client.chat.completions.create(
            model=model,
            temperature=0.3,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        return resp.choices[0].message.content or ""

    return _call


# --------------------------------------------------------------------------- #
# Deterministic fallbacks (used when JSON parsing fails or fields are missing).
# --------------------------------------------------------------------------- #
def _sources_from_pack(pack: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [{"bvid": v["bvid"], "title": v["title"], "url": v["url"],
             "author": v.get("author", "")} for v in pack["videos"]]


def _per_video_skeleton(pack: Dict[str, Any]) -> List[Dict[str, Any]]:
    out = []
    for v in pack["videos"]:
        ts = [{"t": ch["t"], "start_sec": ch["start_sec"], "label": ch["title"]}
              for ch in v["chapters"][:3]]
        out.append({
            "bvid": v["bvid"], "title": v["title"], "url": v["url"],
            "highlights": (v.get("key_points") or [])[:3],
            "timestamps": ts,
        })
    return out


def _clean_quote(s: str) -> str:
    """Drop a stray leading/trailing straight or full-width quote the model sometimes
    leaves inside a string value (e.g. ``...探讨。”``). Purely cosmetic; never touches
    interior text or provenance tags."""
    if not isinstance(s, str):
        return s
    return s.strip().strip("“”‘’\"'")


def _coerce_report(
    topic: str, data: dict, pack: Dict[str, Any], raw_text: str, cost: Dict[str, Any]
) -> TopicReport:
    """Map model JSON onto TopicReport, backfilling any missing field deterministically
    so the result is always well-formed and provenance-complete."""
    def _list(key):
        v = data.get(key)
        return v if isinstance(v, list) else []

    overview = _clean_quote(data.get("overview")) if isinstance(data.get("overview"), str) else ""
    if not overview:
        # fall back to whatever prose the model produced, trimmed.
        overview = (raw_text or "").strip()[:1200]

    per_video = _list("per_video") or _per_video_skeleton(pack)
    # ensure urls present on per_video entries
    for pv in per_video:
        if isinstance(pv, dict) and pv.get("bvid") and not pv.get("url"):
            pv["url"] = _video_url(pv["bvid"])

    watch_list = _list("watch_list")
    if not watch_list:
        watch_list = [{"bvid": v["bvid"], "title": v["title"], "rank": i + 1,
                       "reason": ""} for i, v in enumerate(pack["videos"])]

    return TopicReport(
        topic=topic,
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        overview=overview,
        themes=_list("themes"),
        consensus=_list("consensus"),
        disagreements=_list("disagreements"),
        per_video=per_video,
        watch_list=watch_list,
        gaps=_list("gaps"),
        sources=_sources_from_pack(pack),
        cost=cost,
    )


# --------------------------------------------------------------------------- #
# Public entry point.
# --------------------------------------------------------------------------- #
def synthesize(
    topic: str,
    summaries: Any,
    cfg: "Config",
    *,
    llm_call: Optional[Callable[[str, str], str]] = None,
) -> TopicReport:
    """Synthesize one ``TopicReport`` from per-video summaries.

    Args:
        topic: the search topic.
        summaries: fixture-shape dict ``{bvid: {ok, summary}}`` or a list of records.
        cfg: Config (uses ``synth_model``, ``llm_base_url``, ``llm_api_key``).
        llm_call: optional ``(system, user) -> text``. If None, an OpenAI-compatible
                  client is built from ``cfg``.

    Returns:
        A fully-populated ``TopicReport``. On an LLM error it returns a deterministic
        skeleton (no overview synthesis, but real per_video / sources / watch_list) so
        the pipeline never crashes.
    """
    pack = build_evidence(topic, summaries)
    cost: Dict[str, Any] = {"model": getattr(cfg, "synth_model", ""), "calls": 0}

    if not pack["videos"]:
        return TopicReport(
            topic=topic,
            generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            overview="（无可用的视频摘要，无法生成报告。）",
            cost=cost,
        )

    prompt = build_synth_prompt(topic, pack)
    call = llm_call or None
    raw = ""
    try:
        if call is None:
            call = _default_llm_call(cfg)
        raw = call(prompt["system"], prompt["user"]) or ""
        cost["calls"] = 1
    except Exception as e:  # transport / SDK / network failure -> deterministic skeleton
        cost["error"] = f"{type(e).__name__}: {e}"
        return _coerce_report(topic, {}, pack, "", cost)

    data = _extract_json(raw)
    if not isinstance(data, dict):
        data = {}
    cost["chars_in"] = len(prompt["user"]) + len(prompt["system"])
    cost["chars_out"] = len(raw)
    return _coerce_report(topic, data, pack, raw, cost)
