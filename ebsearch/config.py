"""Configuration knobs (env-driven). Defaults chosen to be cost-conservative.

Per the design: subtitles are preferred (cheap), ASR is opt-in, and only the final
synthesis uses the stronger model (deepseek-v4-pro). N and the ASR policy are tunable
so we can compare tradeoffs during development.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


def _bool(key: str, default: bool) -> bool:
    v = os.environ.get(key)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, str(default)))
    except ValueError:
        return default


@dataclass
class Config:
    # --- AIVideoSummary backend (reused over HTTP for per-video summaries) ---
    backend_url: str = "http://127.0.0.1:8010"   # co-located with the China backend
    backend_api_key: Optional[str] = None         # matches backend VS_API_KEY

    # --- Synthesis LLM (the one "stronger" call; OpenAI-compatible) ---
    synth_model: str = "deepseek-v4-pro"
    llm_api_key: Optional[str] = None
    llm_base_url: str = "https://api.deepseek.com"

    # --- Bilibili search ---
    cookies_file: Optional[str] = None            # Netscape cookies (buvid3/SESSDATA)
    search_order: str = "totalrank"               # relevance by default
    duration_filter: int = 2                       # 0 all,1 <10m,2 10-30m,3 30-60m,4 >60m
    tids: int = 0                                  # 0 = all partitions

    # --- Selection / cost gates (tunable; compared during dev) ---
    max_videos: int = 6                            # how many videos to summarize per topic
    candidate_pages: int = 2                       # search pages to pull before re-ranking
    allow_asr: bool = False                        # subtitle-only by default (cheapest)
    min_play: int = 1000                           # drop near-zero-view results
    min_duration_sec: int = 120                    # drop clips/shorts

    # --- IO ---
    cache_dir: str = ".cache"
    request_delay_sec: float = 1.0                 # throttle search to avoid risk control

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            backend_url=_env("EBS_BACKEND_URL", cls.backend_url),
            backend_api_key=_env("EBS_BACKEND_API_KEY") or None,
            synth_model=_env("EBS_SYNTH_MODEL", cls.synth_model),
            llm_api_key=_env("EBS_LLM_API_KEY") or None,
            llm_base_url=_env("EBS_LLM_BASE_URL", cls.llm_base_url),
            cookies_file=_env("EBS_COOKIES_FILE") or None,
            search_order=_env("EBS_SEARCH_ORDER", cls.search_order),
            duration_filter=_int("EBS_DURATION_FILTER", cls.duration_filter),
            tids=_int("EBS_TIDS", cls.tids),
            max_videos=_int("EBS_MAX_VIDEOS", cls.max_videos),
            candidate_pages=_int("EBS_CANDIDATE_PAGES", cls.candidate_pages),
            allow_asr=_bool("EBS_ALLOW_ASR", cls.allow_asr),
            min_play=_int("EBS_MIN_PLAY", cls.min_play),
            min_duration_sec=_int("EBS_MIN_DURATION_SEC", cls.min_duration_sec),
            cache_dir=_env("EBS_CACHE_DIR", cls.cache_dir),
        )
