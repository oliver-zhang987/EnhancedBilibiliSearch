"""EnhancedBilibiliSearch — topic in, organized multi-video report out.

Flow: search (Bilibili) → rank/select relevant videos → summarize each (reusing the
AIVideoSummary backend) → synthesize one report with a stronger LLM → render.
"""
__version__ = "0.1.0"
