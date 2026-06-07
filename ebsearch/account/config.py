"""Account-package settings, read from the environment with sensible defaults.

Stdlib only. The .env loader mirrors the pattern in
``videosummary/pipeline/config.py`` so a single .env can configure both the
pipeline and the account subsystem.

This module is shared by TWO FastAPI services that point at ONE sqlite file and
ONE JWT secret. Make sure both deployments export the SAME values for
``AUTH_PHONE_SALT`` and ``AUTH_JWT_SECRET`` (otherwise hashes / tokens minted by
one service won't validate in the other).
"""
from __future__ import annotations

import os
from dataclasses import dataclass


def _load_dotenv(path: str = ".env") -> None:
    """Minimal .env loader (no dependency). Does not override existing env vars.

    Copied from ``videosummary/pipeline/config.py`` so the account package has
    no import-time dependency on the pipeline.
    """
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            key, val = key.strip(), val.strip().strip('"').strip("'")
            os.environ.setdefault(key, val)


def _env_int(name: str, default: int) -> int:
    v = os.environ.get(name)
    if v is None or v.strip() == "":
        return default
    try:
        return int(v.strip())
    except ValueError:
        return default


@dataclass
class Settings:
    # --- storage / crypto ---
    db_path: str = "/app/db/users.db"
    # PIPL: phone numbers are hashed with this salt; never stored in cleartext.
    phone_salt: str = "dev-phone-salt-change-me"
    # HS256 secret shared by both services. CHANGE in production.
    jwt_secret: str = "dev-jwt-secret-change-me"
    jwt_days: int = 30

    # --- SMS provider selection ---
    sms_provider: str = "mock"  # mock | aliyun | tencent

    # --- credit grants ---
    grant_default: int = 200

    # --- cost table (credits per operation) ---
    cost_summary_subtitle: int = 5
    cost_summary_asr: int = 20
    cost_report_base: int = 10
    cost_report_per_video: int = 8
    cost_synth_pro_extra: int = 10

    # --- OTP policy ---
    otp_ttl_seconds: int = 300
    otp_resend_cooldown: int = 60
    otp_max_per_phone_hour: int = 5
    otp_max_attempts: int = 3
    otp_length: int = 6


def load_settings(dotenv: str = ".env") -> Settings:
    """Build a :class:`Settings` from the environment (loading .env first)."""
    _load_dotenv(dotenv)
    e = os.environ.get
    return Settings(
        db_path=e("AUTH_DB_PATH", "/app/db/users.db"),
        phone_salt=e("AUTH_PHONE_SALT", "dev-phone-salt-change-me"),
        jwt_secret=e("AUTH_JWT_SECRET", "dev-jwt-secret-change-me"),
        jwt_days=_env_int("AUTH_JWT_DAYS", 30),
        sms_provider=e("AUTH_SMS_PROVIDER", "mock").strip().lower(),
        grant_default=_env_int("AUTH_GRANT_DEFAULT", 200),
        cost_summary_subtitle=_env_int("COST_SUMMARY_SUBTITLE", 5),
        cost_summary_asr=_env_int("COST_SUMMARY_ASR", 20),
        cost_report_base=_env_int("COST_REPORT_BASE", 10),
        cost_report_per_video=_env_int("COST_REPORT_PER_VIDEO", 8),
        cost_synth_pro_extra=_env_int("COST_SYNTH_PRO_EXTRA", 10),
        otp_ttl_seconds=_env_int("OTP_TTL_SECONDS", 300),
        otp_resend_cooldown=_env_int("OTP_RESEND_COOLDOWN", 60),
        otp_max_per_phone_hour=_env_int("OTP_MAX_PER_PHONE_HOUR", 5),
        otp_max_attempts=_env_int("OTP_MAX_ATTEMPTS", 3),
        otp_length=_env_int("OTP_LENGTH", 6),
    )


# Module-level singleton. Re-read with ``reload_settings`` (tests do this after
# monkeypatching AUTH_DB_PATH etc.).
SETTINGS = load_settings()


def reload_settings(dotenv: str = ".env") -> Settings:
    """Re-read settings from the environment and update the module singleton.

    Returns the fresh :class:`Settings`. Used by tests that monkeypatch env
    vars (e.g. AUTH_DB_PATH) after import.
    """
    global SETTINGS
    SETTINGS = load_settings(dotenv)
    return SETTINGS
