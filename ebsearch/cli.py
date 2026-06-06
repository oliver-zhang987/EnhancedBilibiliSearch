"""CLI: ebsearch "<topic>" [options] -> Markdown report on stdout (or --out file)."""
from __future__ import annotations

import argparse
import json
import sys

from .config import Config
from .pipeline import research


def _load_dotenv(path: str = ".env") -> None:
    """Minimal .env loader (does not override existing env)."""
    import os
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())
    except OSError:
        pass


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="ebsearch",
                                description="Search a Bilibili topic, summarize the best "
                                            "videos, and synthesize one report.")
    p.add_argument("topic", help="topic to research")
    p.add_argument("--max", type=int, help="max videos to summarize (EBS_MAX_VIDEOS)")
    p.add_argument("--pages", type=int, help="candidate search pages (EBS_CANDIDATE_PAGES)")
    p.add_argument("--duration", type=int, choices=[0, 1, 2, 3, 4],
                   help="duration filter: 0 all,1 <10m,2 10-30m,3 30-60m,4 >60m")
    p.add_argument("--asr", action="store_true", help="allow server ASR for subtitle-less videos")
    p.add_argument("--expand", action="store_true", help="LLM query expansion")
    p.add_argument("--suggest", action="store_true", help="use B站 suggest to normalize the query")
    p.add_argument("--rerank", action="store_true", help="cheap-LLM rerank of candidates")
    p.add_argument("--out", help="write Markdown to this file instead of stdout")
    p.add_argument("--json", dest="as_json", action="store_true",
                   help="also print the TopicReport JSON to stderr")
    p.add_argument("--quiet", action="store_true", help="suppress progress logging")
    args = p.parse_args(argv)

    _load_dotenv()
    cfg = Config.from_env()
    if args.max is not None:
        cfg.max_videos = args.max
    if args.pages is not None:
        cfg.candidate_pages = args.pages
    if args.duration is not None:
        cfg.duration_filter = args.duration
    if args.asr:
        cfg.allow_asr = True
    if args.expand:
        cfg.query_expand = True
    if args.suggest:
        cfg.query_suggest = True
    if args.rerank:
        cfg.allow_llm_rerank = True

    log = (lambda *a, **k: None) if args.quiet else (lambda m: print("· %s" % m, file=sys.stderr))
    res = research(args.topic, cfg, logger=log)

    if args.out:
        with open(args.out, "w") as f:
            f.write(res.markdown)
        print("wrote %s (%d candidates -> %d selected -> %d summarized)"
              % (args.out, res.n_candidates, res.n_selected, res.n_summarized), file=sys.stderr)
    else:
        sys.stdout.write(res.markdown)

    if args.as_json:
        json.dump(res.report.to_dict(), sys.stderr, ensure_ascii=False, indent=2)
        sys.stderr.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
