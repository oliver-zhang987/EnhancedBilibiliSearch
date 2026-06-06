"""build_plans / expand_queries — offline. No suggest fetch, no real LLM."""
from __future__ import annotations

import json

from ebsearch.config import Config
from ebsearch.search.query import build_plans, expand_queries


def test_default_single_raw_plan_carries_duration_filter():
    cfg = Config(duration_filter=3, search_order="totalrank", candidate_pages=2)
    plans = build_plans("大语言模型 RAG", cfg)  # no llm_complete -> no network

    assert len(plans) == 1
    p = plans[0]
    assert p.origin == "raw"
    assert p.keyword == "大语言模型 RAG"      # normalized (collapsed whitespace)
    assert p.order == "totalrank"
    assert p.duration == 3                    # cfg.duration_filter propagated
    assert p.pages == 2


def test_normalize_collapses_whitespace():
    cfg = Config()
    plans = build_plans("  大语言模型    RAG  ", cfg)
    assert plans[0].keyword == "大语言模型 RAG"


def test_multi_order_adds_a_click_plan():
    cfg = Config(query_multi_order=True, search_order="totalrank")
    plans = build_plans("向量数据库", cfg)

    origins = [p.origin for p in plans]
    orders = [p.order for p in plans]
    assert origins == ["raw", "click"]
    assert "click" in orders
    # both plans still target the same keyword / duration filter
    assert {p.keyword for p in plans} == {"向量数据库"}


def test_expansion_adds_variant_plans_with_fake_llm():
    calls = {"n": 0}

    def fake_llm_complete(system: str, user: str) -> str:
        calls["n"] += 1
        # query.expand_queries extracts the first [...] JSON array from the text.
        return '这是变体：["RAG 检索增强", "向量检索", "知识库问答"]'

    cfg = Config(query_expand=True, query_expand_pages=1)
    plans = build_plans("RAG", cfg, llm_complete=fake_llm_complete)

    assert calls["n"] == 1
    origins = [p.origin for p in plans]
    assert origins[0] == "raw"
    expand_plans = [p for p in plans if p.origin == "expand"]
    assert len(expand_plans) == 3
    assert {p.keyword for p in expand_plans} == {"RAG 检索增强", "向量检索", "知识库问答"}
    # expansion variants use the small per-variant page budget
    assert all(p.pages == 1 for p in expand_plans)


def test_expansion_requires_llm_complete():
    cfg = Config(query_expand=True)
    # query_expand on but no llm_complete provided -> still just the raw plan, no I/O.
    plans = build_plans("RAG", cfg, llm_complete=None)
    assert [p.origin for p in plans] == ["raw"]


def test_expand_queries_dedups_and_caps():
    def fake(system, user):
        return json.dumps(["A", "A", "B", "C", "D"])  # dup + over n

    out = expand_queries("topic", fake, n=3)
    assert out == ["A", "B", "C"]


def test_expand_queries_failsafe_on_garbage():
    assert expand_queries("topic", lambda s, u: "not json at all") == []
    assert expand_queries("topic", lambda s, u: (_ for _ in ()).throw(RuntimeError())) == []
