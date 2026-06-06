"""render.markdown.render_markdown — section headers + clickable timestamp deep links."""
from __future__ import annotations

from ebsearch.models import TopicReport
from ebsearch.render.markdown import render_markdown

BV = "BV1JLN2z4EZQ"


def _report() -> TopicReport:
    return TopicReport(
        topic="大语言模型 RAG 检索增强",
        generated_at="2026-06-06T00:00:00+00:00",
        overview=f"概览：RAG 把检索与生成结合〔{BV}〕。",
        themes=[{"title": "原理", "summary": f"两阶段〔{BV}〕", "video_bvids": [BV]}],
        consensus=[f"检索+生成是核心〔{BV}〕"],
        disagreements=[f"分块策略存在分歧〔{BV}〕"],
        per_video=[{
            "bvid": BV, "title": "RAG 原理详解",
            "url": f"https://www.bilibili.com/video/{BV}",
            "highlights": ["两阶段流程"],
            "timestamps": [{"t": "05:00", "start_sec": 300, "label": "检索阶段"}],
        }],
        watch_list=[{"bvid": BV, "title": "RAG 原理详解", "rank": 1, "reason": "先看原理"}],
        gaps=["评测未覆盖"],
        sources=[{"bvid": BV, "title": "RAG 原理详解",
                  "url": f"https://www.bilibili.com/video/{BV}", "author": "讲解up"}],
        cost={"model": "deepseek-v4-pro", "calls": 1},
    )


def test_contains_expected_section_headers():
    md = render_markdown(_report())
    for header in ["# 大语言模型 RAG 检索增强 — 多视频综合报告",
                   "## 概览", "## 主题脉络", "## 共识", "## 分歧与异见",
                   "## 各视频要点", "## 推荐观看顺序", "## 尚未覆盖 / 值得补充",
                   "## 来源"]:
        assert header in md, f"missing header: {header}"


def test_clickable_timestamp_deep_link():
    md = render_markdown(_report())
    # _ts_link renders [`mm:ss label`](https://.../video/BV?t=sec)
    assert "?t=300" in md
    assert f"https://www.bilibili.com/video/{BV}?t=300" in md
    assert "`05:00 检索阶段`" in md
    # markdown link syntax around the timestamp
    assert f"[`05:00 检索阶段`](https://www.bilibili.com/video/{BV}?t=300)" in md


def test_provenance_tags_preserved():
    md = render_markdown(_report())
    assert f"〔{BV}〕" in md  # consensus/theme provenance tags survive rendering


def test_coverage_matrix_present_when_themes_exist():
    md = render_markdown(_report())
    assert "## 子主题 × 视频 覆盖矩阵" in md
    # can be suppressed
    md_no_matrix = render_markdown(_report(), include_matrix=False)
    assert "覆盖矩阵" not in md_no_matrix


def test_cost_footer_rendered():
    md = render_markdown(_report())
    assert "合成成本" in md and "deepseek-v4-pro" in md
