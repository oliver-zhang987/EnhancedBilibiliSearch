"""Render a :class:`TopicReport` to a readable Markdown report.

Section order is the reader's journey: overview → themes (with which videos cover
each) → consensus vs. disagreements → a sub-topic × video coverage matrix → per-video
highlights with clickable timestamps → a ranked watch list → gaps → sources.

Timestamps render as clickable deep links: ``https://www.bilibili.com/video/{bvid}?t={sec}``.
The 〔BVxxxx〕 provenance tags produced by synthesis are preserved verbatim, so every
claim stays attributable in the rendered output.
"""
from __future__ import annotations

from typing import Any, Dict, List

from ..models import TopicReport


def _video_url(bvid: str) -> str:
    return f"https://www.bilibili.com/video/{bvid}"


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


def render_markdown(report: TopicReport, *, include_matrix: bool = True) -> str:
    src = _src_index(report)
    out: List[str] = []

    out.append(f"# {report.topic} — 多视频综合报告")
    if report.generated_at:
        out.append(f"*生成时间：{report.generated_at} · 来源视频 {len(report.sources or [])} 个*")
    out.append("")

    # ---- Overview ----
    if report.overview:
        out += ["## 概览", "", report.overview, ""]

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
            out.append(f"- **{title}** — {th.get('summary', '')}{tag}")
        out.append("")

    # ---- Consensus vs disagreements ----
    if report.consensus:
        out += ["## 共识", ""]
        out += [f"- {c}" for c in report.consensus]
        out.append("")
    if report.disagreements:
        out += ["## 分歧与异见", ""]
        out += [f"- {d}" for d in report.disagreements]
        out.append("")

    # ---- Coverage matrix ----
    if include_matrix:
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
        out += [f"- {g}" for g in report.gaps]
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
