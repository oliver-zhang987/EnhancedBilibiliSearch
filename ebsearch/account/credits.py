"""Credits / billing ledger.

Every balance change is atomic (one IMMEDIATE transaction) and double-entry:
the ``users.credits`` column is the source of truth and ``credits_ledger`` is an
append-only audit log. ``deduct`` does a check-and-decrement in a single
transaction so two concurrent jobs (possibly in the two different services)
cannot overspend.

Cost calculators read the price table from :mod:`config`.
"""
from __future__ import annotations

import datetime as _dt
from typing import Optional

from . import config, db


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _is_pro_model(model: Optional[str]) -> bool:
    """Heuristic: does ``model`` denote a premium/strong tier?

    Matches names containing pro/strong/max/ultra/opus or the high-end OpenAI /
    Anthropic / DeepSeek families (e.g. 'deepseek-v4-pro', 'gpt-4o', 'gpt-4.1',
    'claude-...-opus'). Cheap tiers like '-flash' / '-mini' / '-haiku' are not.
    """
    if not model:
        return False
    m = model.lower()
    cheap_markers = ("flash", "mini", "nano", "lite", "haiku", "small")
    if any(c in m for c in cheap_markers):
        return False
    pro_markers = ("pro", "strong", "max", "ultra", "opus", "sonnet")
    if any(p in m for p in pro_markers):
        return True
    # Treat full GPT-4 class models as pro.
    if "gpt-4" in m:
        return True
    return False


# --------------------------------------------------------------------------- #
# Balance queries
# --------------------------------------------------------------------------- #
def balance(user_id: int) -> int:
    """Current credit balance, or 0 if the user does not exist."""
    row = db.get_user_by_id(user_id)
    if row is None:
        return 0
    return int(row["credits"] or 0)


def can_afford(user_id: int, cost: int) -> bool:
    return balance(user_id) >= int(cost)


# --------------------------------------------------------------------------- #
# Mutations (atomic)
# --------------------------------------------------------------------------- #
def deduct(user_id: int, cost: int, reason: str, job_id: Optional[str] = None) -> bool:
    """Atomically check-and-decrement. Writes a ledger row on success.

    Returns ``False`` (and writes nothing) if the balance is insufficient — the
    caller maps this to HTTP 402. A non-positive ``cost`` is a no-op success.
    """
    cost = int(cost)
    if cost <= 0:
        return True

    with db.transaction() as conn:
        row = db.get_user_by_id(user_id, conn=conn)
        if row is None:
            return False
        current = int(row["credits"] or 0)
        if current < cost:
            return False  # insufficient — transaction commits no changes
        new_balance = current - cost
        db.set_user_credits(user_id, new_balance, conn=conn)
        db.insert_ledger(
            user_id=user_id,
            delta=-cost,
            balance_after=new_balance,
            reason=reason,
            job_id=job_id,
            ts=_now_iso(),
            conn=conn,
        )
    return True


def refund(user_id: int, amount: int, reason: str, job_id: Optional[str] = None) -> int:
    """Credit ``amount`` back to a user (e.g. a failed job). Returns new balance."""
    return _credit(user_id, int(amount), reason, job_id)


def grant(user_id: int, amount: int, reason: str, job_id: Optional[str] = None) -> int:
    """Add ``amount`` credits (registration grant, invite bonus, admin top-up).
    Returns the new balance."""
    return _credit(user_id, int(amount), reason, job_id)


def _credit(user_id: int, amount: int, reason: str, job_id: Optional[str]) -> int:
    """Shared positive-delta path for grant/refund."""
    amount = int(amount)
    with db.transaction() as conn:
        row = db.get_user_by_id(user_id, conn=conn)
        if row is None:
            return 0
        current = int(row["credits"] or 0)
        new_balance = current + amount
        db.set_user_credits(user_id, new_balance, conn=conn)
        db.insert_ledger(
            user_id=user_id,
            delta=amount,
            balance_after=new_balance,
            reason=reason,
            job_id=job_id,
            ts=_now_iso(),
            conn=conn,
        )
    return new_balance


# --------------------------------------------------------------------------- #
# Cost calculators (read prices from config)
# --------------------------------------------------------------------------- #
def cost_summary(origin_or_force_asr: bool, model: Optional[str] = None) -> int:
    """Cost of summarizing one video.

    ``origin_or_force_asr`` True => the transcript came from ASR (expensive);
    False => subtitles were available (cheap). A pro/strong model adds the synth
    pro extra on top.
    """
    s = config.SETTINGS
    base = s.cost_summary_asr if origin_or_force_asr else s.cost_summary_subtitle
    if _is_pro_model(model):
        base += s.cost_synth_pro_extra
    return base


def cost_report(n_videos: int, model: Optional[str] = None) -> int:
    """Cost of a multi-video report: base + per_video * n + pro extra."""
    s = config.SETTINGS
    n = max(0, int(n_videos))
    total = s.cost_report_base + s.cost_report_per_video * n
    if _is_pro_model(model):
        total += s.cost_synth_pro_extra
    return total
