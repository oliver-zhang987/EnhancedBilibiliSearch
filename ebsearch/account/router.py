"""FastAPI wiring for the account subsystem.

fastapi is imported lazily INSIDE the functions so the rest of the package
(and the test suite) imports fine without fastapi installed.

Public surface for the integrator:
    build_auth_router()    -> APIRouter mounted at /auth/...
    require_user           -> FastAPI dependency, returns user_id (401 if bad)
    optional_user          -> FastAPI dependency, returns user_id or None
    create_invite_code(...) -> admin helper (NOT an HTTP endpoint)
"""
from __future__ import annotations

import datetime as _dt
from typing import Optional

from . import db, service, tokens


# --------------------------------------------------------------------------- #
# Auth header parsing (no fastapi import needed)
# --------------------------------------------------------------------------- #
def _user_id_from_header(authorization: Optional[str]) -> Optional[int]:
    """Extract + verify a Bearer token from an Authorization header value."""
    if not authorization:
        return None
    parts = authorization.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return tokens.verify_token(parts[1].strip())


# --------------------------------------------------------------------------- #
# FastAPI dependencies
# --------------------------------------------------------------------------- #
# These are real Header-bound dependencies when fastapi is installed and plain
# callables otherwise. They are defined in a builder so the module imports
# cleanly without fastapi (the test suite never touches the HTTP layer).
def _build_dependencies():
    """Return ``(require_user, optional_user)`` fastapi dependencies."""
    from fastapi import Header, HTTPException

    def require_user(authorization: Optional[str] = Header(default=None)) -> int:
        """Dependency: require a valid Bearer token; return user_id or 401."""
        uid = _user_id_from_header(authorization)
        if uid is None:
            raise HTTPException(status_code=401, detail="Invalid or missing token.")
        return uid

    def optional_user(authorization: Optional[str] = Header(default=None)) -> Optional[int]:
        """Dependency: return user_id if a valid token is present, else None."""
        return _user_id_from_header(authorization)

    return require_user, optional_user


try:  # pragma: no cover - exercised only when fastapi is installed
    require_user, optional_user = _build_dependencies()
except Exception:  # fastapi not installed (e.g. test env)
    def require_user(authorization: Optional[str] = None) -> int:  # type: ignore[misc]
        """Fallback used only when fastapi is absent. As a real dependency this
        is replaced by a Header-bound version above; here it just verifies a
        raw header value and raises on failure."""
        uid = _user_id_from_header(authorization)
        if uid is None:
            raise PermissionError("Invalid or missing token.")
        return uid

    def optional_user(authorization: Optional[str] = None) -> Optional[int]:  # type: ignore[misc]
        return _user_id_from_header(authorization)


# --------------------------------------------------------------------------- #
# Request models (built lazily, cached in module globals)
# --------------------------------------------------------------------------- #
# Placeholder so static references resolve; populated by ``_ensure_models``.
ActivateBody = None  # type: ignore[assignment]


def _ensure_models():
    """Define the pydantic request model once and stash it in module globals.

    Returns ``ActivateBody``. pydantic is imported lazily here so the package
    imports without it.
    """
    global ActivateBody
    if ActivateBody is not None:
        return ActivateBody

    from pydantic import BaseModel

    class _ActivateBody(BaseModel):
        code: str

    ActivateBody = _ActivateBody
    return ActivateBody


# --------------------------------------------------------------------------- #
# Router builder
# --------------------------------------------------------------------------- #
def build_auth_router():
    """Build and return a fastapi ``APIRouter`` with the /auth/* endpoints.

    Imports fastapi/pydantic lazily so importing this package never requires
    fastapi to be installed.
    """
    from fastapi import APIRouter, Header, HTTPException

    router = APIRouter(prefix="/auth", tags=["auth"])

    # The request model must live in this module's GLOBALS, not this function's
    # locals: with ``from __future__ import annotations`` the endpoint argument
    # annotations are stored as strings and FastAPI resolves them via
    # ``typing.get_type_hints`` against the function's ``__globals__``. A class
    # defined in local scope would be invisible there and FastAPI would mistake
    # the body for a query parameter. ``_ensure_models`` injects it globally.
    ActivateBody = _ensure_models()

    @router.post("/activate")
    def activate(body: ActivateBody):
        """Access-code-only activation: no phone / SMS, no PII collected."""
        try:
            user, token = service.activate(body.code)
        except service.AuthError as exc:
            raise HTTPException(status_code=_auth_status(exc), detail=str(exc))
        return {"token": token, "user": user}

    @router.get("/me")
    def me(authorization: Optional[str] = Header(default=None)):
        uid = _user_id_from_header(authorization)
        if uid is None:
            raise HTTPException(status_code=401, detail="Invalid or missing token.")
        try:
            user = service.me(uid)
        except service.AuthError:
            raise HTTPException(status_code=401, detail="User not found.")
        return {"user": user}

    return router


def _auth_status(exc: "service.AuthError") -> int:
    """Map an AuthError code to an HTTP status.

    bad_otp / not_found -> 401 (auth)
    invalid_invite / no_consent / exists -> 400 (bad request)
    (insufficient credits is surfaced by the gated endpoints as 402, not here)
    """
    if exc.code in ("bad_otp", "not_found"):
        return 401
    return 400


# --------------------------------------------------------------------------- #
# Admin helper (callable from a script, NOT an HTTP endpoint)
# --------------------------------------------------------------------------- #
def create_invite_code(
    code: str, credits: int, max_uses: int, note: Optional[str] = None
) -> dict:
    """Create an invite code. Intended for an admin CLI / one-off script.

    Returns the stored row as a dict. Idempotency is the caller's concern; a
    duplicate code raises sqlite3.IntegrityError.
    """
    db.init_db()
    created_at = _dt.datetime.now(_dt.timezone.utc).isoformat()
    db.insert_invite_code(
        code=code,
        credits=int(credits),
        max_uses=int(max_uses),
        note=note,
        created_at=created_at,
    )
    return {
        "code": code,
        "credits": int(credits),
        "max_uses": int(max_uses),
        "used_count": 0,
        "note": note,
        "active": 1,
        "created_at": created_at,
    }
