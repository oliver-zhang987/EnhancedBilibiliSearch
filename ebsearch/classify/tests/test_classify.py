"""classify.query.classify_query — OFFLINE only. Heuristic fallback + guarded LLM.

No network, no real LLM: the LLM path is exercised via injected fake callables and the
heuristic path needs nothing at all.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Make the repo root importable when running pytest from anywhere.
_ROOT = Path(__file__).resolve().parents[3]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from ebsearch.classify.query import (  # noqa: E402
    REPORT_TYPES,
    classify_heuristic,
    classify_query,
)


def test_label_space_is_the_six_canonical_types():
    assert set(REPORT_TYPES) == {"how_to", "news", "comparison", "concept", "review", "general"}


# --- heuristic buckets (the deterministic, offline path) --------------------- #
import pytest  # noqa: E402


@pytest.mark.parametrize("topic,expected", [
    ("Stable Diffusion 怎么本地部署", "how_to"),
    ("Docker 入门教程 手把手搭建", "how_to"),
    ("how to fine-tune an LLM", "how_to"),
    ("2026 苹果发布会最新爆料", "news"),
    ("OpenAI 最新事件回顾 时间线", "news"),
    ("RTX 5090 vs 4090 哪个更值得买", "comparison"),
    ("Vue 和 React 的区别与选择", "comparison"),
    ("什么是扩散模型 原理科普", "concept"),
    ("Transformer 注意力机制是什么", "concept"),
    ("iPhone 17 上手体验测评 值不值得买", "review"),
    ("某某耳机开箱评测 优缺点", "review"),
    ("人工智能", "general"),
    ("", "general"),
])
def test_heuristic_buckets(topic, expected):
    assert classify_heuristic(topic) == expected


def test_classify_query_offline_uses_heuristic_when_no_llm():
    assert classify_query("Kubernetes 怎么部署到生产环境") == "how_to"
    assert classify_query("随便一个没有信号词的题目") == "general"


def test_llm_label_is_used_when_valid():
    def fake(system, user):
        return "comparison"  # model returns a clean label

    # topic alone would be 'general'; the LLM override should win.
    assert classify_query("一个中性题目", llm_call=fake) == "comparison"


def test_llm_label_parsed_from_chatty_reply():
    def fake(system, user):
        return "这个主题属于 review 类型。"

    assert classify_query("中性题目", llm_call=fake) == "review"


def test_llm_offlabel_reply_falls_back_to_heuristic():
    def fake(system, user):
        return "banana"  # not a valid label

    # heuristic still classifies the how-to cue
    assert classify_query("Nginx 怎么配置反向代理", llm_call=fake) == "how_to"


def test_llm_exception_falls_back_to_heuristic():
    def boom(system, user):
        raise RuntimeError("transport down")

    assert classify_query("什么是向量数据库 原理", llm_call=boom) == "concept"


def test_llm_empty_reply_falls_back_to_heuristic():
    assert classify_query("RTX vs Radeon 对比", llm_call=lambda s, u: "") == "comparison"


def test_return_value_is_always_a_known_label():
    for t in ["", "教程", "新闻", "对比", "科普", "评测", "无关词", "abc 123"]:
        assert classify_query(t) in REPORT_TYPES
