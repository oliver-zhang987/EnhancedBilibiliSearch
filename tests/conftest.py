"""Shared test fixtures/helpers. All tests are OFFLINE — no network, no LLM, no backend.

We never construct a real LLM/transport: anything that would do I/O is injected as a
plain callable or simply left as ``None`` (the core degrades to its no-network path).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Make the repo root importable when running ``pytest`` from anywhere.
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from ebsearch.config import Config  # noqa: E402
from ebsearch.models import VideoHit  # noqa: E402


@pytest.fixture
def cfg() -> Config:
    """A default Config built from class defaults (env is irrelevant offline)."""
    return Config()


def make_hit(**kw) -> VideoHit:
    """VideoHit with sensible, on-topic defaults; override per-test via kwargs."""
    base = dict(
        bvid="BV1aaaaaaaaa",
        aid=1,
        title="测试视频",
        author="up主",
        play=20000,
        pubdate=1_700_000_000,
        duration_sec=1200,
        hit_columns=["title"],
        url="https://www.bilibili.com/video/BV1aaaaaaaaa",
    )
    base.update(kw)
    return VideoHit(**base)
