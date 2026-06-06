"""rank.select — pure heuristic, no LLM, no network. Hand-built VideoHit pool."""
from __future__ import annotations

from ebsearch.config import Config
from ebsearch.models import ScoredHit, VideoHit
from ebsearch.rank.select import select

TOPIC = "大语言模型 RAG 检索增强"


def _hit(bvid, title, *, play=50000, duration_sec=1200,
         hit_columns=("title",), tags=(), pubdate=1_700_000_000) -> VideoHit:
    return VideoHit(
        bvid=bvid,
        title=title,
        play=play,
        duration_sec=duration_sec,
        hit_columns=list(hit_columns),
        tags=list(tags),
        pubdate=pubdate,
        url=f"https://www.bilibili.com/video/{bvid}",
    )


def _pool():
    return [
        # 0: strong on-topic explainer, ideal length -> should win
        _hit("BV1good00001", "RAG 检索增强生成原理详解", play=120000, duration_sec=1200),
        # 1: another on-topic explainer
        _hit("BV1good00002", "大语言模型与向量检索实战", play=80000, duration_sec=1500,
             hit_columns=("title", "tag")),
        # 2: on-topic but tag-only match, fewer plays
        _hit("BV1good00003", "知识库问答 RAG 入门", play=30000, duration_sec=900),
        # 3: OFF-TOPIC high-play decoy: no topic tokens, no title/tag hit
        _hit("BV1decoy0001", "猫咪搞笑合集 太可爱了", play=5_000_000,
             duration_sec=600, hit_columns=("description",)),
        # 4: >max_duration course compilation (must be dropped by hard filter)
        _hit("BV1course001", "大语言模型 RAG 系统课程全集", play=200000,
             duration_sec=5400, hit_columns=("title",)),
    ]


def test_drops_offtopic_decoy_and_overlong_course_no_llm():
    cfg = Config(max_videos=6, max_duration_sec=3600, min_play=1000)
    out = select(_pool(), cfg, TOPIC)  # use_llm defaults to cfg.allow_llm_rerank=False

    assert all(isinstance(s, ScoredHit) for s in out)
    bvids = [s.hit.bvid for s in out]
    # off-topic high-play decoy is dropped despite 5M views
    assert "BV1decoy0001" not in bvids
    # >60min course is dropped by the hard duration filter
    assert "BV1course001" not in bvids
    # the three on-topic explainers survive
    assert set(bvids) == {"BV1good00001", "BV1good00002", "BV1good00003"}


def test_results_sorted_desc_by_score():
    cfg = Config(max_videos=6)
    out = select(_pool(), cfg, TOPIC)
    scores = [s.score for s in out]
    assert scores == sorted(scores, reverse=True)
    assert len(scores) >= 2


def test_respects_max_videos():
    cfg = Config(max_videos=2)
    out = select(_pool(), cfg, TOPIC)
    assert len(out) == 2
    # the two highest-scoring on-topic explainers, best first
    assert out[0].score >= out[1].score


def test_empty_pool_returns_empty():
    assert select([], Config(), TOPIC) == []


def test_low_play_is_filtered():
    cfg = Config(max_videos=6, min_play=1000)
    pool = [
        _hit("BV1good00001", "RAG 检索增强生成原理", play=120000),
        _hit("BV1lowplay01", "RAG 检索增强生成原理", play=10),  # below min_play
    ]
    bvids = [s.hit.bvid for s in select(pool, cfg, TOPIC)]
    assert bvids == ["BV1good00001"]


def test_works_without_llm_even_if_flag_on():
    # use_llm forced True but llm_call=None -> heuristic order kept, no crash/I-O.
    cfg = Config(max_videos=3)
    out = select(_pool(), cfg, TOPIC, use_llm=True, llm_call=None)
    assert [s.hit.bvid for s in out] == [s.hit.bvid for s in select(_pool(), cfg, TOPIC)]
