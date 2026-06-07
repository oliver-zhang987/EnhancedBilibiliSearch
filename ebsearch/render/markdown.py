"""Render a :class:`TopicReport` to a readable Markdown report.

Section order is the reader's journey: overview → themes (with which videos cover
each) → consensus vs. disagreements → a sub-topic × video coverage matrix → per-video
highlights with clickable timestamps → a ranked watch list → gaps → sources.

Timestamps render as clickable deep links: ``https://www.bilibili.com/video/{bvid}?t={sec}``.
The 〔BVxxxx〕 provenance tags produced by synthesis are preserved verbatim, so every
claim stays attributable in the rendered output.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List

from ..models import TopicReport


# Human-readable labels for the report type, shown in the report meta line.
_TYPE_LABEL = {
    "how_to": "操作教程",
    "news": "事件资讯",
    "comparison": "横向对比",
    "concept": "概念科普",
    "review": "测评体验",
    "general": "综合报告",
}


def _video_url(bvid: str) -> str:
    return f"https://www.bilibili.com/video/{bvid}"


def _cite(text: str, sources: List[Dict[str, Any]]) -> str:
    """Turn [n] citation markers into markdown links to source n's video."""
    def repl(m):
        n = int(m.group(1))
        if 1 <= n <= len(sources):
            s = sources[n - 1]
            return f"[[{n}]]({s.get('url') or _video_url(s.get('bvid', ''))})"
        return m.group(0)
    return re.sub(r"\[(\d+)\]", repl, text or "")


def _ts_link(bvid: str, start_sec: int, label: str = "") -> str:
    """A clickable [mm:ss] (or [mm:ss] label) deep link into the video."""
    sec = max(0, int(start_sec or 0))
    mmss = f"{sec // 60:02d}:{sec % 60:02d}"
    url = f"{_video_url(bvid)}?t={sec}"
    shown = f"{mmss} {label}".strip() if label else mmss
    return f"[`{shown}`]({url})"


def _src_index(report: TopicReport) -> Dict[str, Dict[str, Any]]:
    return {s.get("bvid"): s for s in (report.sources or []) if s.get("bvid")}


def _short_label(bvid: str, src: Dict[str, Dict[str, Any]]) -> str:
    """A compact column header for the matrix: author + short bvid."""
    info = src.get(bvid, {})
    author = info.get("author") or ""
    return f"{author}".strip() or bvid


def _coverage_matrix(report: TopicReport, src: Dict[str, Dict[str, Any]]) -> List[str]:
    """Build a sub-topic × video coverage matrix from ``themes`` (kept as a render
    affordance — the at-a-glance view that the matrix-centric design optimized for,
    without sacrificing the structured prompt's nuance)."""
    themes = report.themes or []
    if not themes:
        return []
    # column order = sources order (stable, all videos)
    bvids = [s["bvid"] for s in (report.sources or []) if s.get("bvid")]
    if not bvids:
        return []
    header = "| 子主题 | " + " | ".join(_short_label(b, src) for b in bvids) + " |"
    sep = "| --- | " + " | ".join("---" for _ in bvids) + " |"
    rows = [header, sep]
    for th in themes:
        title = (th.get("title") or "").replace("|", "/")
        covers = set(th.get("video_bvids") or [])
        cells = ["✓" if b in covers else "—" for b in bvids]
        rows.append(f"| {title} | " + " | ".join(cells) + " |")
    return ["## 子主题 × 视频 覆盖矩阵", "", *rows, ""]


# --------------------------------------------------------------------------- #
# Type-specific block renderers. Each returns a list of markdown lines (empty if
# its block is absent), so render_markdown can drop them in wherever a given
# report_type wants them. The 〔n〕 citations inside text are resolved via _cite.
# --------------------------------------------------------------------------- #
def _render_steps(report: TopicReport, sources: List[Dict[str, Any]]) -> List[str]:
    """how_to → an ordered, numbered procedure; each step optionally deep-links into
    the video at the timestamp that demonstrates it ([mm:ss])."""
    steps = report.steps or []
    if not steps:
        return []
    out = ["## 操作步骤", ""]
    for i, st in enumerate(steps):
        n = st.get("n") or (i + 1)
        title = st.get("title") or ""
        detail = _cite(st.get("detail", ""), sources)
        line = f"{n}. **{title}**"
        if detail:
            line += f" — {detail}"
        bvid = st.get("bvid") or ""
        if bvid:
            line += "  \n   ▶ " + _ts_link(bvid, st.get("start_sec", 0))
        out.append(line)
    out.append("")
    return out


def _render_timeline(report: TopicReport, src: Dict[str, Dict[str, Any]],
                     sources: List[Dict[str, Any]]) -> List[str]:
    """news → a chronological event list, earliest cue first as the model ordered it."""
    timeline = report.timeline or []
    if not timeline:
        return []
    out = ["## 事件时间线", ""]
    for ev in timeline:
        date = ev.get("date") or ""
        event = _cite(ev.get("event", ""), sources)
        links = ""
        bvids = ev.get("bvids") or []
        if bvids:
            links = "  \n  <sub>来源：" + ", ".join(
                f"[{src.get(b, {}).get('author') or b}]({_video_url(b)})" for b in bvids
            ) + "</sub>"
        out.append(f"- **{date}** — {event}{links}" if date else f"- {event}{links}")
    out.append("")
    return out


def _render_comparison(report: TopicReport, sources: List[Dict[str, Any]]) -> List[str]:
    """comparison → a markdown table: options as rows, dimensions as columns."""
    comp = report.comparison or {}
    dims = comp.get("dimensions") or []
    options = comp.get("options") or []
    if not dims or not options:
        return []
    header = "| 对象 | " + " | ".join(str(d).replace("|", "/") for d in dims) + " |"
    sep = "| --- | " + " | ".join("---" for _ in dims) + " |"
    rows = [header, sep]
    for opt in options:
        name = (opt.get("name") or "").replace("|", "/")
        bvid = opt.get("bvid") or ""
        if bvid:
            name = f"[{name}]({_video_url(bvid)})"
        cells = opt.get("cells") or {}
        vals = [str(cells.get(d, "—")).replace("|", "/").replace("\n", " ") or "—" for d in dims]
        rows.append(f"| **{name}** | " + " | ".join(vals) + " |")
    return ["## 横向对比", "", *rows, ""]


def _render_glossary(report: TopicReport, sources: List[Dict[str, Any]]) -> List[str]:
    """concept → a term/definition glossary."""
    glossary = report.glossary or []
    if not glossary:
        return []
    out = ["## 关键术语", ""]
    for g in glossary:
        term = g.get("term") or ""
        definition = _cite(g.get("definition", ""), sources)
        if term:
            out.append(f"- **{term}**：{definition}")
    out.append("")
    return out


def _render_verdicts(report: TopicReport, src: Dict[str, Dict[str, Any]],
                     sources: List[Dict[str, Any]]) -> List[str]:
    """review → one verdict card per subject (conclusion/rating + pros/cons)."""
    verdicts = report.verdicts or []
    if not verdicts:
        return []
    out = ["## 测评结论", ""]
    for v in verdicts:
        subject = v.get("subject") or ""
        rating = v.get("rating") or ""
        bvid = v.get("bvid") or ""
        head = f"### {subject}"
        if rating:
            head += f" — {rating}"
        out += [head, ""]
        pros = v.get("pros") or []
        cons = v.get("cons") or []
        if pros:
            out.append("**优点**")
            out += [f"- {_cite(p, sources)}" for p in pros]
        if cons:
            out.append("")
            out.append("**缺点**")
            out += [f"- {_cite(c, sources)}" for c in cons]
        if bvid:
            out.append("")
            out.append(f"<sub>主要依据：[{src.get(bvid, {}).get('author') or bvid}]({_video_url(bvid)})</sub>")
        out.append("")
    return out


def render_markdown(report: TopicReport, *, include_matrix: bool = True) -> str:
    src = _src_index(report)
    sources = report.sources or []
    rt = getattr(report, "report_type", "general") or "general"
    out: List[str] = []

    out.append(f"# {report.topic} — 多视频综合报告")
    if report.generated_at:
        meta = f"*生成时间：{report.generated_at} · 来源视频 {len(report.sources or [])} 个"
        if rt != "general":
            meta += f" · 报告类型：{_TYPE_LABEL.get(rt, rt)}"
        out.append(meta + "*")
    out.append("")

    # ---- Overview ----
    if report.overview:
        out += ["## 概览", "", _cite(report.overview, sources), ""]

    # ---- Type-specific lead block: for non-general reports the tailored block comes
    #      right after the overview so the report *reads* like its kind (a how-to leads
    #      with steps, news with the timeline, etc.). general is untouched.
    if rt == "how_to":
        out += _render_steps(report, sources)
    elif rt == "news":
        out += _render_timeline(report, src, sources)
    elif rt == "comparison":
        out += _render_comparison(report, sources)
    elif rt == "concept":
        out += _render_glossary(report, sources)
    elif rt == "review":
        out += _render_verdicts(report, src, sources)

    # ---- Themes ----
    if report.themes:
        out += ["## 主题脉络", ""]
        for th in report.themes:
            title = th.get("title", "")
            covers = th.get("video_bvids") or []
            tag = ""
            if covers:
                links = ", ".join(f"[{src.get(b, {}).get('author') or b}]({_video_url(b)})"
                                  for b in covers)
                tag = f"  \n  <sub>覆盖：{links}</sub>"
            out.append(f"- **{title}** — {_cite(th.get('summary', ''), sources)}{tag}")
        out.append("")

    # ---- Consensus vs disagreements ----
    if report.consensus:
        out += ["## 共识", ""]
        out += [f"- {_cite(c, sources)}" for c in report.consensus]
        out.append("")
    if report.disagreements:
        out += ["## 分歧与异见", ""]
        out += [f"- {_cite(d, sources)}" for d in report.disagreements]
        out.append("")

    # ---- Coverage matrix ----
    # Suppress it for comparison reports: the dimensions×options table above is the
    # at-a-glance view, so a second theme×video grid would be redundant noise.
    if include_matrix and not (rt == "comparison" and report.comparison):
        out += _coverage_matrix(report, src)

    # ---- Per-video highlights ----
    if report.per_video:
        out += ["## 各视频要点", ""]
        for pv in report.per_video:
            bvid = pv.get("bvid", "")
            title = pv.get("title") or src.get(bvid, {}).get("title") or bvid
            url = pv.get("url") or _video_url(bvid)
            author = src.get(bvid, {}).get("author") or ""
            head = f"### [{title}]({url})"
            if author:
                head += f" — {author}"
            out += [head, ""]
            for h in pv.get("highlights") or []:
                out.append(f"- {h}")
            ts = pv.get("timestamps") or []
            if ts:
                links = " · ".join(
                    _ts_link(bvid, t.get("start_sec", 0), t.get("label", "")) for t in ts
                )
                out += ["", f"  关键时间点：{links}"]
            out.append("")

    # ---- Watch list ----
    if report.watch_list:
        out += ["## 推荐观看顺序", ""]
        for w in sorted(report.watch_list, key=lambda x: x.get("rank", 999)):
            bvid = w.get("bvid", "")
            title = w.get("title") or src.get(bvid, {}).get("title") or bvid
            url = _video_url(bvid)
            rank = w.get("rank", "")
            reason = w.get("reason", "")
            out.append(f"{rank}. **[{title}]({url})** — {reason}")
        out.append("")

    # ---- Gaps ----
    if report.gaps:
        out += ["## 尚未覆盖 / 值得补充", ""]
        out += [f"- {_cite(g, sources)}" for g in report.gaps]
        out.append("")

    # ---- Sources ----
    if report.sources:
        out += ["## 来源", ""]
        for s in report.sources:
            bvid = s.get("bvid", "")
            out.append(
                f"- 〔{bvid}〕[{s.get('title', '')}]({s.get('url') or _video_url(bvid)})"
                f" — {s.get('author', '')}"
            )
        out.append("")

    # ---- Cost footer ----
    if report.cost:
        c = report.cost
        bits = [f"model={c.get('model', '')}", f"calls={c.get('calls', 0)}"]
        if c.get("error"):
            bits.append(f"error={c['error']}")
        out += ["---", f"<sub>合成成本：{' · '.join(bits)}</sub>"]

    return "\n".join(out).rstrip() + "\n"
