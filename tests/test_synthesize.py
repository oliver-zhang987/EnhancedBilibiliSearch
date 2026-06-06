"""synthesize.report.synthesize — mock the one LLM call; never touch the network."""
from __future__ import annotations

import json

from ebsearch.config import Config
from ebsearch.models import TopicReport
from ebsearch.synthesize.report import synthesize

TOPIC = "大语言模型 RAG 检索增强"
BV1 = "BV1JLN2z4EZQ"
BV2 = "BV1pSpWz2ES5"


def _summaries():
    """Two tiny per-video summaries in the list-of-records shape synthesize accepts."""
    return [
        {
            "bvid": BV1,
            "title": "RAG 原理详解",
            "url": f"https://www.bilibili.com/video/{BV1}",
            "ok": True,
            "summary": {
                "media": {"title": "RAG 原理详解", "uploader": "讲解up",
                          "duration": 1200, "url": f"https://www.bilibili.com/video/{BV1}"},
                "tldr": "讲清楚检索增强生成的基本流程。",
                "key_points": ["检索+生成两阶段", "向量库召回"],
                "keywords": ["RAG", "向量检索"],
                "chapters": [
                    {"title": "什么是RAG", "start": 30, "bullets": ["定义"]},
                    {"title": "检索阶段", "start": 300, "bullets": ["embedding"]},
                ],
            },
        },
        {
            "bvid": BV2,
            "title": "RAG 实战",
            "url": f"https://www.bilibili.com/video/{BV2}",
            "ok": True,
            "summary": {
                "media": {"title": "RAG 实战", "uploader": "工程师up",
                          "duration": 1500, "url": f"https://www.bilibili.com/video/{BV2}"},
                "tldr": "动手搭一个RAG问答系统。",
                "key_points": ["分块策略", "重排序"],
                "keywords": ["分块", "rerank"],
                "chapters": [{"title": "环境准备", "start": 60, "bullets": ["依赖"]}],
            },
        },
    ]


def test_synthesize_populates_report_and_preserves_provenance():
    captured = {}

    def fake_llm(system: str, user: str) -> str:
        captured["system"] = system
        captured["user"] = user
        return json.dumps({
            "overview": f"该批视频系统介绍了RAG〔{BV1}；{BV2}〕。",
            "themes": [
                {"title": "原理", "summary": f"两阶段流程〔{BV1}〕",
                 "video_bvids": [BV1]},
            ],
            "consensus": [f"检索+生成是核心〔{BV1}；{BV2}〕"],
            "disagreements": [f"分块策略上{BV2}更强调重排〔{BV2}〕"],
            "per_video": [
                {"bvid": BV1, "title": "RAG 原理详解",
                 "url": f"https://www.bilibili.com/video/{BV1}",
                 "highlights": ["两阶段"],
                 "timestamps": [{"t": "00:30", "start_sec": 30, "label": "定义"}]},
                {"bvid": BV2, "title": "RAG 实战",
                 "url": f"https://www.bilibili.com/video/{BV2}",
                 "highlights": ["分块"],
                 "timestamps": [{"t": "01:00", "start_sec": 60, "label": "准备"}]},
            ],
            "watch_list": [
                {"bvid": BV1, "title": "RAG 原理详解", "rank": 1, "reason": "先看原理"},
                {"bvid": BV2, "title": "RAG 实战", "rank": 2, "reason": "再上手"},
            ],
            "gaps": ["评测指标未覆盖"],
        }, ensure_ascii=False)

    report = synthesize(TOPIC, _summaries(), Config(), llm_call=fake_llm)

    assert isinstance(report, TopicReport)
    assert report.topic == TOPIC
    assert "RAG" in report.overview
    # provenance tags preserved verbatim
    assert f"〔{BV1}；{BV2}〕" in report.consensus[0]
    assert report.themes and report.themes[0]["title"] == "原理"
    assert report.disagreements
    assert len(report.per_video) == 2
    assert {s["bvid"] for s in report.sources} == {BV1, BV2}
    assert report.cost.get("calls") == 1
    # the evidence pack the model saw contains both bvids and forbids extra facts
    assert BV1 in captured["user"] and BV2 in captured["user"]


def test_synthesize_garbage_response_degrades_to_skeleton():
    def fake_llm(system: str, user: str) -> str:
        return "对不起，我无法完成。"  # no JSON at all

    report = synthesize(TOPIC, _summaries(), Config(), llm_call=fake_llm)

    assert isinstance(report, TopicReport)
    # deterministic per_video + sources built straight from the summaries (no crash)
    assert {pv["bvid"] for pv in report.per_video} == {BV1, BV2}
    assert {s["bvid"] for s in report.sources} == {BV1, BV2}
    # skeleton highlights/timestamps come from the summaries' key_points/chapters
    pv1 = next(pv for pv in report.per_video if pv["bvid"] == BV1)
    assert pv1["highlights"] == ["检索+生成两阶段", "向量库召回"]
    assert pv1["timestamps"][0]["start_sec"] == 30
    # watch_list backfilled
    assert len(report.watch_list) == 2
    # the model was called once even though it returned garbage
    assert report.cost.get("calls") == 1


def test_synthesize_llm_exception_degrades_to_skeleton():
    def boom(system, user):
        raise RuntimeError("transport down")

    report = synthesize(TOPIC, _summaries(), Config(), llm_call=boom)
    assert {pv["bvid"] for pv in report.per_video} == {BV1, BV2}
    assert "error" in report.cost  # recorded, not raised
    assert report.cost.get("calls") == 0


def test_synthesize_no_videos_returns_placeholder():
    report = synthesize(TOPIC, [], Config(), llm_call=lambda s, u: "{}")
    assert report.per_video == []
    assert "无" in report.overview
