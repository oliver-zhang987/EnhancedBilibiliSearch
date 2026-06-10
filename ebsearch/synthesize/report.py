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

from ..classify.query import REPORT_TYPES
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
    """Build the attributable evidence pack handed to the model.

    Each video gets a 1-based ``n`` used as its citation number ([n]); the report's
    ``sources`` are emitted in the same order so [n] maps to sources[n-1].
    """
    pack: Dict[str, Any] = {"topic": topic, "videos": []}
    for i, it in enumerate(_normalize_summaries(summaries)):
        bvid = it["bvid"]
        s = it["summary"]
        media = s.get("media") or {}
        duration = media.get("duration") or 0
        chapters = []
        for ch in (s.get("chapters") or [])[:12]:
            chapters.append({
                "title": ch.get("title", ""),
                "t": _mmss(ch.get("start", 0)),
                "start_sec": int(ch.get("start", 0) or 0),
                "points": (ch.get("bullets") or [])[:4],
            })
        pack["videos"].append({
            "n": i + 1,  # citation number for [n]
            "bvid": bvid,
            "title": media.get("title") or it.get("title") or "",
            "author": media.get("uploader") or "",
            "duration_min": round((duration or 0) / 60, 1),
            "url": media.get("url") or it.get("url") or _video_url(bvid),
            "tldr": s.get("tldr", ""),
            "key_points": (s.get("key_points") or [])[:10],
            "keywords": (s.get("keywords") or [])[:10],
            "chapters": chapters,
        })
    return pack


# --------------------------------------------------------------------------- #
# Prompt (strict provenance contract). Returns (system, user).
# --------------------------------------------------------------------------- #
_RULES = (
    "严格规则（必须遵守）：\n"
    "1. 只能使用【证据包】中的内容，禁止引入证据包之外的任何事实、数字、模型名或观点；"
    "证据不足以支撑的说法宁可不写，不要推测。\n"
    "2. 引用来源时，在论断后用**方括号数字**标注，数字 = 证据包中视频的 n（编号），"
    "多个来源连写，如 [1] 或 [1][3]。n 只能取证据包中真实存在的编号，禁止超出范围或编造编号。"
    "【绝对不要】输出 BV 号或视频标题作为引用记号，只用 [n]。\n"
    "3. 概览与各段要写成**连贯、自然的中文叙述**，信息充分、具体（包含关键概念/方法/数字/工具名），"
    "不要罗列式堆砌、不要过度简略；但每个具体说法仍须有 [n] 支撑。\n"
    "4. 若某观点只有一个视频提出（尤其与其它视频相左），明确点出是少数/异见观点并注明 [n] 与分歧所在。\n"
    "5. 不要编造时间戳；时间戳只能取自证据包 chapters 的 t/start_sec。\n"
    "6. 用简体中文。宁可详实，不要泛泛而谈。\n"
)

_SYSTEM = (
    "你是一名严谨且擅长写作的研究助理，负责把同一主题下多个B站视频的逐条要点汇总成一份"
    "可信、可追溯、信息密度高、对研究者真正有用的中文报告。绝不臆造，绝不超出给定证据，"
    "同时尽量写得充实、有条理、可读性强。"
)


# Shared JSON fields every report carries (the existing general schema). The
# type-specific paths append one extra block and a short focus note, but keep this
# whole contract so a report always renders and provenance stays uniform.
_BASE_JSON_FIELDS = (
    '  "overview": "一段 8-12 句、信息充分的总览：主题的整体图景、这批视频的主要结论与分歧、'
    '覆盖的范围与深度；要具体到关键概念/方法/工具，并用 [n] 标注来源。",\n'
    '  "themes": [ {"title":"子主题",'
    '"summary":"用 2-4 句把各视频在此子主题下讲了什么、异同与关键细节写清楚（带 [n]）",'
    '"video_bvids":["BV..."]} ],\n'
    '  "consensus": ["多个视频一致认同的结论，写具体，每条带 [n][n]"],\n'
    '  "disagreements": ["分歧或单一视频的异见：写清谁的观点 [n]、与谁/与主流如何不同"],\n'
    '  "per_video": [ {"bvid":"BV...","title":"...","url":"...",'
    '"highlights":["该视频最有价值的 3-5 点，具体一些"],'
    '"timestamps":[{"t":"mm:ss","start_sec":int,"label":"该处讲了什么"}]} ],\n'
    '  "watch_list": [ {"bvid":"BV...","title":"...","rank":1,"reason":"为何按此顺序看/适合谁"} ],\n'
    '  "gaps": ["这批视频未覆盖、但研究该主题应了解的方面，写出 3-5 条"]'
)

_BASE_TAIL = (
    "themes 取 4-6 个；per_video 覆盖全部视频，每个挑 2-3 个最有信息量的时间戳；"
    "watch_list 给出全部视频的推荐顺序与理由。整体宁详勿略。"
)

# Per-type add-ons: a one-line focus note (steers overview/themes) + the extra JSON
# block to request + a one-line tail emphasis. Keys are report_type labels; "general"
# (and any unknown type) uses no add-on and yields the original prompt verbatim.
_TYPE_FOCUS: Dict[str, str] = {
    "how_to": "这是一个【操作教程 / how-to】类主题。请把重点放在“如何一步步做到”，"
              "概览先给出整体路线，再把可执行流程整理成有序步骤。\n",
    "news": "这是一个【新闻 / 事件】类主题。请把重点放在“发生了什么、何时、进展如何”，"
            "概览先交代事件全貌，并尽量梳理出时间线（时间点只能取自证据包，不要编造日期）。\n",
    "comparison": "这是一个【对比 / 选择】类主题。请把重点放在“几个对象在关键维度上的优劣异同、"
                  "各自适合谁”，概览给出选购/选择结论，并整理成对比矩阵。\n",
    "concept": "这是一个【概念 / 原理科普】类主题。请把重点放在“它是什么、为什么、怎么运作”，"
               "概览循序渐进地解释核心概念，并整理关键术语表。\n",
    "review": "这是一个【测评 / 体验】类主题。请把重点放在“好不好、值不值、优缺点与体验”，"
              "概览给出总体结论，并按对象整理评价卡片（结论/评分/优点/缺点）。\n",
}

_TYPE_BLOCK: Dict[str, str] = {
    "how_to": '  "steps": [ {"n":1,"title":"步骤标题",'
              '"detail":"该步要做什么、注意点，写具体（带 [n]）",'
              '"bvid":"BV...","start_sec":int} ]',
    "news": '  "timeline": [ {"date":"时间(如 2026-03 或“发布会当天”，只能取自证据包)",'
            '"event":"该时间点发生了什么（带 [n]）","bvids":["BV..."]} ]',
    "comparison": '  "comparison": {"dimensions":["对比维度1","对比维度2"],'
                  '"options":[ {"name":"对象A","bvid":"BV...",'
                  '"cells":{"对比维度1":"A 在此维度的表现/数值","对比维度2":"..."}} ]}',
    "concept": '  "glossary": [ {"term":"术语","definition":"一句话定义，准确、好懂（可带 [n]）"} ]',
    "review": '  "verdicts": [ {"subject":"被测对象","rating":"总体结论/评分(如 8/10 或 推荐)",'
              '"pros":["优点，具体一些"],"cons":["缺点/槽点"],"bvid":"BV..."} ]',
}

_TYPE_TAIL: Dict[str, str] = {
    "how_to": " 另外请给出 steps：把全过程拆成 4-9 个有序步骤，每步尽量标注最能演示该步的视频 bvid 与"
              " start_sec（取自证据包 chapters，不要编造）。",
    "news": " 另外请给出 timeline：按时间先后排列关键事件，每条注明 [n] 与对应 bvids。",
    "comparison": " 另外请给出 comparison：dimensions 取 3-6 个最关键的对比维度，options 覆盖被对比的各对象，"
                  "cells 用证据包内容填写，缺失就留空字符串。",
    "concept": " 另外请给出 glossary：列出 5-12 个理解该主题必须先懂的核心术语及简明定义。",
    "review": " 另外请给出 verdicts：每个被测对象一张卡片，给出总体结论/评分、3-5 条优点、2-4 条缺点，"
              "并标注主要依据的视频 bvid。",
}


def build_synth_prompt(
    topic: str, evidence: Dict[str, Any], report_type: str = "general"
) -> Dict[str, str]:
    """Return ``{"system": ..., "user": ...}`` for the structured-JSON design.

    ``report_type`` selects an optional add-on: a focus note that steers the shared
    fields plus one extra JSON block (steps / timeline / comparison / glossary /
    verdicts). ``general`` (or any unknown type) reproduces the original prompt
    verbatim, so the general path is unchanged.
    """
    ev_str = json.dumps(evidence, ensure_ascii=False)
    focus = _TYPE_FOCUS.get(report_type, "")
    extra_block = _TYPE_BLOCK.get(report_type)
    extra_tail = _TYPE_TAIL.get(report_type, "")
    fields_block = _BASE_JSON_FIELDS + ((",\n" + extra_block) if extra_block else "")
    user = (
        f"主题：《{topic}》\n\n"
        + focus +
        "下面是该主题下若干视频的逐条要点【证据包】（JSON，每个视频带编号 n、bvid、标题、要点、章节）：\n"
        f"{ev_str}\n\n"
        + _RULES +
        "\n请只输出一个 JSON 对象（不要解释、不要代码围栏），结构如下：\n"
        "{\n"
        + fields_block + "\n"
        "}\n"
        + _BASE_TAIL + extra_tail
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
    topic: str, data: dict, pack: Dict[str, Any], raw_text: str, cost: Dict[str, Any],
    report_type: str = "general",
) -> TopicReport:
    """Map model JSON onto TopicReport, backfilling any missing field deterministically
    so the result is always well-formed and provenance-complete.

    The type-specific block (steps / timeline / comparison / glossary / verdicts) is
    passed through when present and simply stays empty when missing — so a report of
    any ``report_type`` always renders even if the model omitted its tailored block.
    """
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

    comparison = data.get("comparison")
    if not isinstance(comparison, dict):
        comparison = {}

    return TopicReport(
        topic=topic,
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        report_type=report_type if report_type in REPORT_TYPES else "general",
        overview=overview,
        themes=_list("themes"),
        consensus=_list("consensus"),
        disagreements=_list("disagreements"),
        per_video=per_video,
        watch_list=watch_list,
        gaps=_list("gaps"),
        sources=_sources_from_pack(pack),
        cost=cost,
        steps=_list("steps"),
        timeline=_list("timeline"),
        comparison=comparison,
        glossary=_list("glossary"),
        verdicts=_list("verdicts"),
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
    report_type: str = "general",
) -> TopicReport:
    """Synthesize one ``TopicReport`` from per-video summaries.

    Args:
        topic: the search topic.
        summaries: fixture-shape dict ``{bvid: {ok, summary}}`` or a list of records.
        cfg: Config (uses ``synth_model``, ``llm_base_url``, ``llm_api_key``).
        llm_call: optional ``(system, user) -> text``. If None, an OpenAI-compatible
                  client is built from ``cfg``.
        report_type: one of the canonical report types (see ``classify.query``). It
                  selects a type-specific prompt add-on + JSON block; ``general``
                  (default / unknown) keeps the original prompt and shape.

    Returns:
        A fully-populated ``TopicReport``. On an LLM error it returns a deterministic
        skeleton (no overview synthesis, but real per_video / sources / watch_list) so
        the pipeline never crashes.
    """
    if report_type not in REPORT_TYPES:
        report_type = "general"
    pack = build_evidence(topic, summaries)
    cost: Dict[str, Any] = {"model": getattr(cfg, "synth_model", ""), "calls": 0}

    if not pack["videos"]:
        return TopicReport(
            topic=topic,
            generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            report_type=report_type,
            overview="（无可用的视频摘要，无法生成报告。）",
            cost=cost,
        )

    prompt = build_synth_prompt(topic, pack, report_type)
    call = llm_call or None
    raw = ""
    try:
        if call is None:
            call = _default_llm_call(cfg)
        raw = call(prompt["system"], prompt["user"]) or ""
        cost["calls"] = 1
    except Exception as e:  # transport / SDK / network failure -> deterministic skeleton
        cost["error"] = f"{type(e).__name__}: {e}"
        return _coerce_report(topic, {}, pack, "", cost, report_type)

    data = _extract_json(raw)
    if not isinstance(data, dict):
        data = {}
    cost["chars_in"] = len(prompt["user"]) + len(prompt["system"])
    cost["chars_out"] = len(raw)
    return _coerce_report(topic, data, pack, raw, cost, report_type)
