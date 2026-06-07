"""Classify a research topic into one of six report types.

The report type drives presentation downstream: synthesis asks for type-specific
blocks (steps / timeline / comparison / glossary / verdicts) and rendering reorders
and re-styles sections accordingly.

Design mirrors the rest of the codebase's transport-light, degrade-gracefully stance
(cf. ``rank.select`` / ``synthesize.report``):

  * ``classify_query(topic, llm_call=None)`` does **at most one** cheap structured call
    when an ``llm_call`` is injected, and **always** falls back to a pure-python keyword
    heuristic. No LLM dependency, no network — the heuristic alone is a complete answer,
    so the module works fully offline and is what the tests exercise.
  * The label space is closed: anything we can't confidently bucket is ``general`` (the
    existing, unchanged report shape).
"""
from __future__ import annotations

import re
from typing import Callable, List, Optional, Tuple

# The six canonical report types. ``general`` is the default / catch-all and keeps the
# original report shape; the other five each get a tailored synthesis + render path.
REPORT_TYPES = ("how_to", "news", "comparison", "concept", "review", "general")

# --------------------------------------------------------------------------- #
# Keyword heuristic. Ordered by specificity: the most discriminating signals
# (comparison "vs", review verdict-words) are scored highest so they win ties
# against weaker cues. Returns the best-scoring label, else "general".
# --------------------------------------------------------------------------- #
# Each entry: (report_type, [(pattern, weight), ...]). Patterns are matched
# case-insensitively against the topic; CJK terms match literally.
_RULES: List[Tuple[str, List[Tuple[str, int]]]] = [
    ("comparison", [
        (r"\bvs\.?\b", 4), (r"对比", 4), (r"区别", 3), (r"哪个好", 4),
        (r"哪个更", 3), (r"哪款", 3), (r"选择", 2), (r"选购", 2), (r"还是", 2),
        (r"差异", 3), (r"优劣", 3), (r"对决", 3), (r"比较", 3),
        (r"\bvs\b", 4),
    ]),
    ("review", [
        (r"测评", 5), (r"评测", 5), (r"值不值", 5), (r"值得(买|入手|购买)", 4),
        (r"上手体验", 4), (r"体验", 2), (r"开箱", 4), (r"使用感受", 4),
        (r"\breview\b", 4), (r"踩坑", 3), (r"优缺点", 3), (r"好不好用", 4),
        (r"翻车", 2),
    ]),
    ("how_to", [
        (r"怎么", 4), (r"如何", 4), (r"教程", 5), (r"步骤", 4), (r"教学", 4),
        (r"how\s+to", 5), (r"入门", 3), (r"上手", 3), (r"搭建", 3), (r"配置", 2),
        (r"安装", 3), (r"部署", 3), (r"实操", 3), (r"手把手", 5), (r"从零", 3),
        (r"做法", 3), (r"指南", 3),
    ]),
    ("news", [
        (r"最新", 4), (r"事件", 4), (r"发布会?", 3), (r"新闻", 5), (r"资讯", 4),
        (r"进展", 3), (r"动态", 3), (r"爆料", 4), (r"官宣", 4), (r"回顾", 2),
        (r"20\d{2}", 2), (r"近期", 3), (r"刚刚", 3), (r"突发", 4), (r"时间线", 4),
    ]),
    ("concept", [
        (r"是什么", 5), (r"什么是", 5), (r"原理", 4), (r"科普", 5), (r"解释", 3),
        (r"概念", 4), (r"通俗(易懂|讲解)?", 4), (r"讲解", 2), (r"详解", 2),
        (r"为什么", 3), (r"理解", 2), (r"基础知识", 4), (r"是怎么(回事|工作)", 4),
    ]),
]


def _score_topic(topic: str) -> dict:
    """Sum keyword weights per report type for ``topic`` (lowercased for ASCII)."""
    t = (topic or "").lower()
    scores = {rt: 0 for rt, _ in _RULES}
    for rt, patterns in _RULES:
        for pat, w in patterns:
            if re.search(pat, t):
                scores[rt] += w
    return scores


def classify_heuristic(topic: str) -> str:
    """Pure-python, offline label for ``topic``. Never raises; defaults to ``general``.

    The highest-scoring report type wins. On a tie we prefer the more *specific*
    intent (the rule list is ordered comparison > review > how_to > news > concept),
    which matches how a human reads a mixed-signal title.
    """
    scores = _score_topic(topic)
    best_rt, best_score = "general", 0
    for rt, _ in _RULES:  # iterate in specificity order so ties keep the earlier rule
        if scores[rt] > best_score:
            best_rt, best_score = rt, scores[rt]
    return best_rt


# --------------------------------------------------------------------------- #
# Optional one-shot LLM classification (cheap model). Always guarded; the
# heuristic is the safety net, so an empty / malformed / failing call is fine.
# --------------------------------------------------------------------------- #
_LABELS_STR = ", ".join(t for t in REPORT_TYPES)

_SYSTEM = (
    "你是一个意图分类器。只输出一个英文标签，不要解释、不要标点、不要其它文字。"
)


def _build_prompt(topic: str) -> str:
    return (
        f"把下面的研究主题归类到这几种报告类型之一：{_LABELS_STR}。\n"
        "判断标准：\n"
        "- how_to：教程/步骤/如何做某事（怎么、如何、教程、搭建、入门）。\n"
        "- news：最新事件/发布/新闻/时间线（最新、发布、新闻、20XX 年的进展）。\n"
        "- comparison：两个及以上对象的对比/选择（对比、vs、哪个好、区别）。\n"
        "- concept：概念/原理/科普解释（是什么、原理、科普、为什么）。\n"
        "- review：测评/体验/值不值得（测评、评测、值不值、开箱、体验）。\n"
        "- general：以上都不明显时的默认。\n"
        f"主题：《{topic}》\n"
        "只回复一个标签："
    )


def _parse_label(text: str) -> Optional[str]:
    """Pull the first valid report-type token out of a model reply, else None."""
    if not text:
        return None
    low = text.strip().lower()
    for rt in REPORT_TYPES:
        # word-ish boundary so "general" inside a sentence still matches, but we
        # require the token to appear as written.
        if re.search(r"(?<![a-z_])" + re.escape(rt) + r"(?![a-z])", low):
            return rt
    return None


def classify_query(
    topic: str,
    llm_call: Optional[Callable[[str, str], str]] = None,
) -> str:
    """Return one of :data:`REPORT_TYPES` for ``topic``.

    Args:
        topic: the user's research topic.
        llm_call: optional ``(system, user) -> text`` cheap caller. If given, we make
            **one** structured call and use it only when it yields a valid label;
            any error / empty / off-label reply silently falls back to the keyword
            heuristic. If ``None`` (the offline / test path) we use the heuristic only.

    The function never raises and never requires the LLM.
    """
    if llm_call is not None:
        try:
            raw = llm_call(_SYSTEM, _build_prompt(topic)) or ""
            label = _parse_label(raw)
            if label in REPORT_TYPES:
                return label
        except Exception:
            pass  # fall through to the deterministic heuristic
    return classify_heuristic(topic)
