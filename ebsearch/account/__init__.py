"""Self-contained, stdlib-only account subsystem.

Phone + invite-code registration, passwordless JWT auth (phone + 6-digit OTP),
and a credits/billing ledger. Designed to be vendored into two FastAPI services
that share ONE sqlite file and ONE JWT secret.

Quick start for the integrator::

    from videosummary.account import init_db, build_auth_router, require_user
    init_db()
    app.include_router(build_auth_router())

    @app.post("/summarize")
    def summarize(user_id: int = Depends(require_user)):
        if not deduct(user_id, cost_summary(False, model), "summary", job_id):
            raise HTTPException(402, "Insufficient credits")
        ...
"""
from __future__ import annotations

from .config import Settings, load_settings, reload_settings  # noqa: F401
from .credits import (  # noqa: F401
    balance,
    can_afford,
    cost_report,
    cost_summary,
    deduct,
    grant,
    refund,
)
from .db import init_db  # noqa: F401
from .router import (  # noqa: F401
    build_auth_router,
    create_invite_code,
    optional_user,
    require_user,
)
from .tokens import issue_token, verify_token  # noqa: F401

__all__ = [
    # bootstrap / config
    "init_db",
    "load_settings",
    "reload_settings",
    "Settings",
    # auth dependencies + router
    "require_user",
    "optional_user",
    "build_auth_router",
    "create_invite_code",
    # tokens
    "verify_token",
    "issue_token",
    # credits
    "balance",
    "can_afford",
    "deduct",
    "refund",
    "grant",
    "cost_summary",
    "cost_report",
]
