"""Hand-rolled HS256 JWT (stdlib only — no PyJWT).

A JWT is ``base64url(header).base64url(payload).base64url(signature)`` where the
signature is ``HMAC-SHA256(secret, header.payload)``. We implement exactly that
so the package has zero third-party dependencies.

Both FastAPI services must share the SAME ``AUTH_JWT_SECRET`` for tokens minted
by one to validate in the other.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Any, Dict, Optional

from . import config

_HEADER = {"alg": "HS256", "typ": "JWT"}


# --------------------------------------------------------------------------- #
# base64url helpers (no padding, per RFC 7515)
# --------------------------------------------------------------------------- #
def b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64url_decode(data: str) -> bytes:
    # Restore stripped padding before decoding.
    pad = (-len(data)) % 4
    return base64.urlsafe_b64decode(data + ("=" * pad))


def _secret_bytes() -> bytes:
    return config.SETTINGS.jwt_secret.encode("utf-8")


def _sign(signing_input: bytes) -> str:
    sig = hmac.new(_secret_bytes(), signing_input, hashlib.sha256).digest()
    return b64url_encode(sig)


def _encode_segment(obj: Dict[str, Any]) -> str:
    # Compact JSON (no spaces) keeps tokens small and deterministic.
    raw = json.dumps(obj, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return b64url_encode(raw)


def issue_token(user_id: int) -> str:
    """Mint a signed JWT for ``user_id`` with sub/iat/exp claims."""
    now = int(time.time())
    exp = now + config.SETTINGS.jwt_days * 86400
    payload = {"sub": int(user_id), "iat": now, "exp": exp}
    header_seg = _encode_segment(_HEADER)
    payload_seg = _encode_segment(payload)
    signing_input = f"{header_seg}.{payload_seg}".encode("ascii")
    sig_seg = _sign(signing_input)
    return f"{header_seg}.{payload_seg}.{sig_seg}"


def verify_token(token: str) -> Optional[int]:
    """Validate signature + expiry and return the user_id, or ``None``.

    Uses ``hmac.compare_digest`` for a constant-time signature check. Any
    malformed token, bad signature, wrong alg, or expired token yields ``None``.
    """
    if not token or not isinstance(token, str):
        return None
    parts = token.split(".")
    if len(parts) != 3:
        return None
    header_seg, payload_seg, sig_seg = parts

    signing_input = f"{header_seg}.{payload_seg}".encode("ascii")
    expected_sig = _sign(signing_input)
    # Constant-time comparison resists timing attacks.
    if not hmac.compare_digest(expected_sig, sig_seg):
        return None

    try:
        header = json.loads(b64url_decode(header_seg))
        payload = json.loads(b64url_decode(payload_seg))
    except (ValueError, json.JSONDecodeError):
        return None

    # Reject unexpected algorithms (defends against alg-confusion / "none").
    if not isinstance(header, dict) or header.get("alg") != "HS256":
        return None

    if not isinstance(payload, dict):
        return None

    exp = payload.get("exp")
    if not isinstance(exp, (int, float)) or time.time() >= exp:
        return None

    sub = payload.get("sub")
    if not isinstance(sub, int):
        return None
    return sub
