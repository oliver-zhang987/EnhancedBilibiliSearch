# EnhancedBilibiliSearch

Give a **topic**, get an **organized report** synthesized from the most relevant
Bilibili videos. It ranks topic-relevant videos, summarizes the best ones (reusing the
[AIVideoSummary](https://github.com/oliver-zhang987/AIVideoSummary) backend), and
synthesizes one report with a stronger LLM.

> Status: **early / in active exploration.** Foundation + contracts are in place; the
> ranking, synthesis, and query strategies are being explored and compared before the
> design is frozen. Not yet usable end-to-end.

## Pipeline

```
topic
  │  (optional) query expansion / suggest canonicalization
  ▼
search        Bilibili wbi/search/type (WBI-signed, relevance order)   ebsearch/search/
  ▼
rank/select   re-rank by rank_score + hit_columns + play + recency;     ebsearch/rank/
              cost gates (min play/duration, top-N)
  ▼
summarize     fan-out: per video → AIVideoSummary backend /api/jobs     ebsearch/summarize/
              (subtitle-first; ASR opt-in; cache reuse)
  ▼
synthesize    deepseek-v4-pro: overview, themes, consensus vs.          ebsearch/synthesize/
              disagreement, per-video highlights, ranked watch list
  ▼
render        Markdown (+ JSON)                                          ebsearch/render/
```

Surfaces: **CLI + REST API**, and a **standalone web page** (topic in → report out).
It does *not* modify the existing browser extension.

## Design decisions

- **Reuse, don't re-implement.** Per-video summaries call the deployed AIVideoSummary
  backend over HTTP (co-located → `127.0.0.1:8010`), inheriting its cookies, ASR relay,
  and content-addressed cache.
- **Cost discipline (不浪费).** Hard relevance/duration/play prefilter *before* any
  summary; small configurable `max_videos`; subtitle-first (ASR opt-in); cache reuse;
  exactly **one** strong-LLM call (synthesis), not per video.
- **Synthesis model:** `deepseek-v4-pro` (configurable). Strong on Chinese, cheap,
  avoids the precious OpenAI key.
- **Don't wing the Bilibili side.** Search behavior (WBI signing, risk control, result
  shape) is validated against the live API before being built on. Note: the canonical
  `bilibili-API-collect` doc was taken down, so behavior is verified empirically.

## Config

See `.env.example`. Key knobs: `EBS_MAX_VIDEOS`, `EBS_ALLOW_ASR`, `EBS_DURATION_FILTER`,
`EBS_MIN_PLAY`, `EBS_SEARCH_ORDER`, `EBS_SYNTH_MODEL`, `EBS_BACKEND_URL/_API_KEY`.

## License

MIT
