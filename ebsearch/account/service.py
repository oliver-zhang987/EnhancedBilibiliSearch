"""High-level orchestration the API layer calls.

Combines OTP verification, invite-code consumption, user creation, credit
grants, and token issuance into the register/login/me flows. All multi-step
writes happen inside a single transaction for atomicity.
"""
from __future__ import annotations

import datetime as _dt
import sqlite3
from typing import Any, Dict, Optional, Tuple

from . import config, db, otp, tokens


class AuthError(Exception):
    """Typed auth/validation error mapped to an HTTP status by the router.

    ``code`` tags the failure: 'bad_otp', 'invalid_invite', 'exists',
    'not_found', etc.
    """

    def __init__(self, message: str, code: str = "auth_error"):
        super().__init__(message)
        self.code = code


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def user_public(row: sqlite3.Row) -> Dict[str, Any]:
    """Project a users row to the public shape (never exposes phone_hash)."""
    return {
        "id": int(row["id"]),
        "phone_last4": row["phone_last4"],
        "credits": int(row["credits"] or 0),
        "created_at": row["created_at"],
    }


def register(
    phone: str, code: str, invite_code: str, consented: bool
) -> Tuple[Dict[str, Any], str]:
    """Register a new user via phone + OTP + invite code.

    Steps (OTP check first, then everything else atomically):
      1. verify the OTP (consumes it on success)
      2. require explicit PIPL consent
      3. require the phone is not already registered
      4. validate + consume the invite code (used_count++ under max_uses, active)
      5. create the user with the granted credits
      6. write a 'register grant' ledger row
      7. issue a JWT

    Returns ``(user_public_dict, token)``. Raises :class:`AuthError`.
    """
    if not consented:
        raise AuthError("Consent is required to register (PIPL).", code="no_consent")

    # OTP verification is single-use and lives in its own short transaction; do
    # it before opening the registration transaction.
    if not otp.verify_code(phone, code):
        raise AuthError("Invalid or expired verification code.", code="bad_otp")

    ph = otp.phone_hash(phone)
    last4 = phone[-4:] if phone else ""
    inv = (invite_code or "").strip()

    with db.transaction() as conn:
        # Phone must be new (the OTP is already consumed; that's acceptable —
        # the user can request a fresh code and log in instead).
        if db.get_user_by_phone_hash(ph, conn=conn) is not None:
            raise AuthError("Phone already registered; please log in.", code="exists")

        # Validate + consume the invite code atomically.
        granted = _consume_invite_code(inv, conn)

        user_id = db.create_user(
            phone_hash=ph,
            phone_last4=last4,
            credits=granted,
            invite_code=inv,
            consented=True,
            created_at=_now_iso(),
            conn=conn,
        )

        # Ledger: registration grant (balance_after == granted since brand new).
        db.insert_ledger(
            user_id=user_id,
            delta=granted,
            balance_after=granted,
            reason="register grant",
            job_id=None,
            ts=_now_iso(),
            conn=conn,
        )

        row = db.get_user_by_id(user_id, conn=conn)

    token = tokens.issue_token(user_id)
    return user_public(row), token


def _consume_invite_code(code: str, conn: sqlite3.Connection) -> int:
    """Validate the invite code and bump used_count, inside ``conn``'s tx.

    Returns the number of credits the code grants. Raises :class:`AuthError`
    with code 'invalid_invite' if missing / inactive / exhausted.
    """
    if not code:
        raise AuthError("An invite code is required.", code="invalid_invite")

    row = db.get_invite_code(code, conn=conn)
    if row is None:
        raise AuthError("Unknown invite code.", code="invalid_invite")
    if not row["active"]:
        raise AuthError("Invite code is no longer active.", code="invalid_invite")

    used = int(row["used_count"] or 0)
    max_uses = row["max_uses"]
    if max_uses is not None and used >= int(max_uses):
        raise AuthError("Invite code has been fully used.", code="invalid_invite")

    # Conditional UPDATE guards against a concurrent registration consuming the
    # last slot between our read and write (defense in depth on top of the tx).
    cur = conn.execute(
        "UPDATE invite_codes SET used_count = used_count + 1 "
        "WHERE code = ? AND active = 1 "
        "AND (max_uses IS NULL OR used_count < max_uses)",
        (code,),
    )
    if cur.rowcount != 1:
        raise AuthError("Invite code has been fully used.", code="invalid_invite")

    return int(row["credits"] or 0)


def login(phone: str, code: str) -> Tuple[Dict[str, Any], str]:
    """Log in an existing user via phone + OTP. Raises :class:`AuthError`."""
    if not otp.verify_code(phone, code):
        raise AuthError("Invalid or expired verification code.", code="bad_otp")

    ph = otp.phone_hash(phone)
    row = db.get_user_by_phone_hash(ph)
    if row is None:
        raise AuthError("No account for this phone; please register.", code="not_found")

    token = tokens.issue_token(int(row["id"]))
    return user_public(row), token


def me(user_id: int) -> Dict[str, Any]:
    """Return the public profile for ``user_id``. Raises if unknown."""
    row = db.get_user_by_id(user_id)
    if row is None:
        raise AuthError("User not found.", code="not_found")
    return user_public(row)
