"""End-to-end orchestrator: topic -> report.

    search (multi-plan, WBI)  ->  rank/select (heuristic [+ optional rerank])
      ->  fan-out summarize (reuse AIVideoSummary backend)
      ->  synthesize (deepseek-v4-pro)  ->  render Markdown

Each stage is cost-gated: the duration filter + selection cap how many videos get
summarized; exactly one strong-LLM call does the synthesis.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from . import llm
from .classify.query import classify_query
from .config import Config
from .models import ScoredHit, TopicReport, VideoHit, VideoSummary
from .rank.select import select
from .render.markdown import render_markdown
from .search.client import BilibiliSearchClient
from .summarize.fanout import summarize_selected
from .synthesize.report import synthesize


@dataclass
class ResearchResult:
    topic: str
    report: TopicReport
    markdown: str
    n_candidates: int = 0
    n_selected: int = 0
    n_summarized: int = 0
    stages: Dict[str, Any] = field(default_factory=dict)


def research(topic: str, cfg: Optional[Config] = None, *, logger=None) -> ResearchResult:
    cfg = cfg or Config.from_env()
    log = logger or (lambda *a, **k: None)

    # 1) search (optional LLM query expansion)
    expand = llm.expand_caller(cfg) if cfg.query_expand else None
    client = BilibiliSearchClient(cfg)
    hits: List[VideoHit] = client.search_topic(topic, llm_complete=expand)
    log("search: %d candidates" % len(hits))

    # 2) rank + select (heuristic always; optional cheap rerank)
    rerank = llm.rerank_caller(cfg) if cfg.allow_llm_rerank else None
    selected: List[ScoredHit] = select(hits, cfg, topic,
                                       use_llm=cfg.allow_llm_rerank, llm_call=rerank)
    log("select: %d chosen" % len(selected))

    # 3) fan-out summarize (reuse backend)
    summaries: List[VideoSummary] = summarize_selected(selected, cfg, client=client, logger=logger)
    ok = [s for s in summaries if s.ok]
    log("summarize: %d/%d ok" % (len(ok), len(summaries)))

    # 4) classify the topic to pick an adaptive report shape (cheap/offline-safe),
    #    then synthesize (one strong-model call) + render in that shape.
    report_type = classify_query(topic, llm_call=llm.expand_caller(cfg))
    log("classify: report_type=%s" % report_type)
    report = synthesize(topic, [s.to_dict() for s in ok], cfg,
                        llm_call=llm.synth_caller(cfg), report_type=report_type)
    md = render_markdown(report)

    return ResearchResult(
        topic=topic, report=report, markdown=md,
        n_candidates=len(hits), n_selected=len(selected), n_summarized=len(ok),
        stages={
            "selected": [{"bvid": s.hit.bvid, "title": s.hit.title,
                          "score": s.score} for s in selected],
            "failed": [{"bvid": s.bvid, "error": s.error}
                       for s in summaries if not s.ok],
        },
    )
