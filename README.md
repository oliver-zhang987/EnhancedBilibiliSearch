# EnhancedBilibiliSearch

Give a **topic**, get an **organized report** synthesized from the most relevant
Bilibili videos. It ranks topic-relevant videos, summarizes the best ones (reusing the
[AIVideoSummary](https://github.com/oliver-zhang987/AIVideoSummary) backend), and
synthesizes one report with a stronger LLM.

> Status: **works end-to-end (CLI / API / web).** Per-video summaries are produced via
> the AIVideoSummary backend; ranking, synthesis, and query strategies are tuned and the
> contracts are stable. See **Run** below.

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

## Run

Copy `.env.example` → `.env` and fill in the keys you need. At minimum set
`EBS_LLM_API_KEY` (synthesis) and point `EBS_BACKEND_URL` at a running AIVideoSummary
backend (default `http://127.0.0.1:8010`); set `EBS_BACKEND_API_KEY` if that backend
requires one. The core has **no pip dependencies** (stdlib only); the server/tests need
the extras below.

```bash
pip install -e .            # core only (CLI)
pip install -e ".[server]"  # + FastAPI/uvicorn for the API + web UI
pip install -e ".[dev]"     # + pytest/httpx to run the tests
```

**CLI** — Markdown to stdout (`--out FILE` to write a file, `--json` to also dump the
report JSON to stderr; `--max/--duration/--asr/--expand/--suggest/--rerank` override
config):

```bash
ebsearch "大语言模型 RAG 检索增强"
ebsearch "向量数据库" --max 4 --duration 2 --rerank --out report.md
```

**Server (REST API)** — background jobs; `research()` blocks ~1–2 min per topic:

```bash
python -m ebsearch.server.app      # or: ebsearch-server
# binds EBS_HOST:EBS_PORT (default 0.0.0.0:8020)
```

```bash
# start a job -> {"job_id": "..."}  (HTTP 202)
curl -sX POST localhost:8020/api/research \
  -H 'Content-Type: application/json' \
  -d '{"topic":"大语言模型 RAG 检索增强","max_videos":4,"allow_llm_rerank":true}'
# poll -> {"status":"pending|running|done|error","progress":[...],"markdown":"...","report":{...}}
curl -s localhost:8020/api/research/<job_id>
curl -s localhost:8020/health   # {"ok":true}
```

Server env: `EBS_HOST`/`EBS_PORT` (bind), `EBS_SERVER_API_KEY` (if set, `/api/*`
requires an `X-API-Key` header), `EBS_CORS_ORIGINS` (comma-separated; default `*`).

**Web UI** — open `http://localhost:8020/` (served at `/` by the server). Enter a topic,
tweak options (摘要视频数 / 时长筛选 / ASR / 扩展 / 重排), click **生成报告**; the page
polls every ~3 s, streams the stage progress (候选→筛选→摘要→合成), and renders the
Markdown report. If `EBS_SERVER_API_KEY` is set, a key field appears and is stored in
`localStorage`.

**Tests** (offline, hermetic — no network/LLM/backend):

```bash
python -m pytest -q
```

## Config

See `.env.example`. Key knobs: `EBS_MAX_VIDEOS`, `EBS_ALLOW_ASR`, `EBS_DURATION_FILTER`,
`EBS_MIN_PLAY`, `EBS_SEARCH_ORDER`, `EBS_SYNTH_MODEL`, `EBS_BACKEND_URL/_API_KEY`.

## License

MIT
