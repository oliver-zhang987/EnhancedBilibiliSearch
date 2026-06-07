"""Query classification: decide which *report shape* a topic deserves.

A topic like "RTX 5090 值不值得买" wants a review layout (verdict + pros/cons), while
"什么是扩散模型" wants a concept/glossary layout. :func:`classify_query` returns one of
the six canonical report types, cheaply and — crucially — offline-safe.
"""
from .query import REPORT_TYPES, classify_query  # noqa: F401

__all__ = ["classify_query", "REPORT_TYPES"]
