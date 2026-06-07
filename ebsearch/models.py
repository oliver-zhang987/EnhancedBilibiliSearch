"""Canonical data contracts for the search → rank → summarize → synthesize flow.

These are the stable seams the rest of the codebase (and the exploration agents)
build against. Kept deliberately small; the synthesis report shape may evolve as
we compare report designs.
"""
from __future__ import annotations

from dataclasses import dataclass, field, fields, asdict
from typing import Any, Dict, List, Optional


@dataclass
class VideoHit:
    """One video result from Bilibili search/type (cleaned)."""
    bvid: str
    aid: int = 0
    title: str = ""              # <em> highlight tags already stripped
    author: str = ""
    mid: int = 0
    play: int = 0
    danmaku: int = 0             # from `video_review`
    favorites: int = 0
    review: int = 0              # comment count
    pubdate: int = 0             # unix seconds
    duration_sec: int = 0        # parsed from "MM:SS"/"HH:MM:SS"
    description: str = ""
    tags: List[str] = field(default_factory=list)
    typename: str = ""           # sub-partition, e.g. "科学科普"
    rank_score: float = 0.0      # Bilibili's opaque relevance score
    hit_columns: List[str] = field(default_factory=list)  # title/description/tag/author
    pic: str = ""
    url: str = ""                # https://www.bilibili.com/video/{bvid}

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ScoredHit:
    """A VideoHit plus our own relevance/quality score and the reasons for it."""
    hit: VideoHit
    score: float = 0.0
    reasons: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {"hit": self.hit.to_dict(), "score": self.score, "reasons": self.reasons}


@dataclass
class VideoSummary:
    """Result of summarizing one selected video (via the AIVideoSummary backend)."""
    bvid: str
    title: str = ""
    url: str = ""
    summary: Optional[Dict[str, Any]] = None   # {tldr, key_points, chapters[], keywords}
    source: str = ""                           # "subtitle" | "asr"
    ok: bool = False
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class TopicReport:
    """The synthesized, organized output for a topic.

    ``report_type`` (one of how_to / news / comparison / concept / review / general)
    drives *adaptive presentation*: synthesis fills the matching type-specific block
    below and rendering reorders/re-styles sections for it. All type-specific blocks
    are optional and default empty, so any report still renders if a block is absent
    and the ``general`` shape is unaffected when they stay empty.
    """
    topic: str
    query_used: str = ""
    generated_at: str = ""
    report_type: str = "general"
    overview: str = ""
    themes: List[Dict[str, Any]] = field(default_factory=list)   # [{title, summary, video_bvids[]}]
    consensus: List[str] = field(default_factory=list)
    disagreements: List[str] = field(default_factory=list)
    per_video: List[Dict[str, Any]] = field(default_factory=list)  # [{bvid,title,url,highlights[],timestamps[]}]
    watch_list: List[Dict[str, Any]] = field(default_factory=list)  # ranked recommendations w/ reason
    gaps: List[str] = field(default_factory=list)
    sources: List[Dict[str, Any]] = field(default_factory=list)     # provenance: bvid,title,url,author
    cost: Dict[str, Any] = field(default_factory=dict)

    # ---- optional, type-specific blocks (rendered conditionally) ----
    # how_to: ordered steps with an optional deep-link into the video that shows it.
    steps: List[Dict[str, Any]] = field(default_factory=list)       # [{n,title,detail,bvid,start_sec}]
    # news: chronological events, each backed by one or more videos.
    timeline: List[Dict[str, Any]] = field(default_factory=list)    # [{date,event,bvids[]}]
    # comparison: a dimensions × options matrix.
    comparison: Dict[str, Any] = field(default_factory=dict)        # {dimensions[], options:[{name,bvid,cells:{dim:val}}]}
    # concept: term → definition glossary.
    glossary: List[Dict[str, Any]] = field(default_factory=list)    # [{term,definition}]
    # review: per-subject verdict cards.
    verdicts: List[Dict[str, Any]] = field(default_factory=list)    # [{subject,rating,pros[],cons[],bvid}]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "TopicReport":
        """Rebuild a report from a (possibly partial) dict, defaulting any missing or
        new field — so older payloads round-trip and the type-specific blocks default
        empty when absent."""
        d = d or {}
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in known})
