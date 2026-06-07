"""One-time-passcode lifecycle: hashing, generation, send, verify.

Security / privacy
------------------
* Phone numbers are hashed (sha256(phone + salt)) before they touch the DB —
  PIPL compliance: no cleartext phone at rest.
* OTP codes are stored hashed (sha256) too; verification is constant-time.
* Rate limits: a per-phone resend cooldown and an hourly send cap, both raising
  the typed :class:`OTPError` on violation.
* Production paths never log the full phone or the code. (The mock SMS provider
  may log last4 + code — that is dev-only.)
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from typing import Dict, Optional

from . import config, db, sms


class OTPError(Exception):
    """Typed error for OTP policy violations (cooldown, hourly cap, send fail).

    ``code`` is a short machine-readable tag the router maps to an HTTP status.
    """

    def __init__(self, message: str, code: str = "otp_error"):
        super().__init__(message)
        self.code = code


def phone_hash(phone: str) -> str:
    """sha256(phone + salt) hex. Salt comes from settings (shared by both
    services so the same phone hashes identically everywhere)."""
    salt = config.SETTINGS.phone_salt
    return hashlib.sha256((phone + salt).encode("utf-8")).hexdigest()


def _code_hash(code: str) -> str:
    """Hash an OTP code. Salted with the phone salt for defense in depth; the
    code itself is short-lived and single-use."""
    salt = config.SETTINGS.phone_salt
    return hashlib.sha256((code + salt).encode("utf-8")).hexdigest()


def gen_code() -> str:
    """Cryptographically-random numeric code of OTP_LENGTH digits (zero-padded)."""
    n = config.SETTINGS.otp_length
    upper = 10 ** n
    return str(secrets.randbelow(upper)).zfill(n)


def send_code(phone: str) -> Dict[str, Optional[object]]:
    """Generate + store + dispatch an OTP, enforcing rate limits.

    Returns ``{"sent": True, "debug_code": code}`` under the mock provider and
    ``{"sent": True, "debug_code": None}`` for real providers.

    Raises :class:`OTPError` on cooldown / hourly-cap violations or send failure.
    """
    s = config.SETTINGS
    ph = phone_hash(phone)
    now = time.time()

    existing = db.get_otp(ph)

    # --- hourly window bookkeeping ---
    if existing is not None:
        hour_window = existing["hour_window"] or now
        sent_count = existing["sent_count_hour"] or 0
        last_sent = existing["last_sent"] or 0.0

        # Resend cooldown.
        if now - last_sent < s.otp_resend_cooldown:
            wait = int(s.otp_resend_cooldown - (now - last_sent))
            raise OTPError(
                f"Please wait {wait}s before requesting another code.",
                code="cooldown",
            )

        # Roll the hour window if it has elapsed.
        if now - hour_window >= 3600:
            hour_window = now
            sent_count = 0

        # Hourly cap.
        if sent_count >= s.otp_max_per_phone_hour:
            raise OTPError(
                "Too many codes requested this hour. Try again later.",
                code="hourly_cap",
            )
    else:
        hour_window = now
        sent_count = 0

    code = gen_code()
    expires_at = now + s.otp_ttl_seconds

    # Persist BEFORE attempting to send so a send failure can't leave a usable
    # code unrecorded; on send failure we delete it again.
    db.upsert_otp(
        phone_hash=ph,
        code_hash=_code_hash(code),
        expires_at=expires_at,
        last_sent=now,
        sent_count_hour=sent_count + 1,
        hour_window=hour_window,
    )

    provider = sms.get_provider()
    try:
        provider.send(phone, code)
    except Exception as exc:
        # Roll back the OTP row so the user can retry immediately.
        db.delete_otp(ph)
        raise OTPError(f"Failed to send code: {exc}", code="send_failed") from exc

    is_mock = (config.SETTINGS.sms_provider or "mock").strip().lower() == "mock"
    return {"sent": True, "debug_code": code if is_mock else None}


def verify_code(phone: str, code: str) -> bool:
    """Validate an OTP: not expired, attempts under cap, constant-time match.

    Increments the attempt counter on a wrong code; deletes the row on success
    (single-use). Returns ``True`` only on a valid, unexpired match.
    """
    s = config.SETTINGS
    ph = phone_hash(phone)
    row = db.get_otp(ph)
    if row is None:
        return False

    now = time.time()
    if now >= (row["expires_at"] or 0):
        # Expired — clean up so it can't be brute-forced after TTL.
        db.delete_otp(ph)
        return False

    if (row["attempts"] or 0) >= s.otp_max_attempts:
        return False

    expected = row["code_hash"] or ""
    candidate = _code_hash(code or "")
    if hmac.compare_digest(expected, candidate):
        db.delete_otp(ph)  # consume on success
        return True

    db.increment_otp_attempts(ph)
    return False
