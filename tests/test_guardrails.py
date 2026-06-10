"""Free-tier guardrails for the EBSearch backend — fully OFFLINE.

Covers:
  * per-user report quota — EBS_USER_REPORTS_PER_HOUR sliding-hour cap on
    POST /api/research, 429 past the cap, checked BEFORE any credit charge
  * fan-out 451 handling  — when the AIVideoSummary backend rejects a video
    with HTTP 451 (creator opt-out blocklist), that video is skipped
    gracefully (ok=False, error="已按创作者要求跳过") and the other videos /
    the overall report are unaffected

The research pipeline and all HTTP transport are monkeypatched: NO live
search/LLM/ASR/backend calls happen.

Run:  PYTHONPATH=. python -m pytest tests/test_guardrails.py -q
"""
from __future__ import annotations

import importlib
import types
import urllib.error

import pytest

pytest.importorskip("fastapi.testclient")
from fastapi.testclient import TestClient  # noqa: E402

from ebsearch.models import ScoredHit, VideoHit  # noqa: E402
from ebsearch.summarize import fanout  # noqa: E402


# --------------------------------------------------------------------------- #
# Server fixtures (same offline harness as tests/test_auth_credits.py)
# --------------------------------------------------------------------------- #
@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTH_DB_PATH", str(tmp_path / "users.db"))
    monkeypatch.setenv("AUTH_JWT_SECRET", "test-secret")
    monkeypatch.setenv("EBS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("EBS_MAX_VIDEOS", "4")
    monkeypatch.setenv("EBS_SYNTH_MODEL", "deepseek-v4-pro")
    monkeypatch.setenv("EBS_USER_REPORTS_PER_HOUR", "2")  # small cap for tests
    monkeypatch.setenv("COST_REPORT_BASE", "10")
    monkeypatch.setenv("COST_REPORT_PER_VIDEO", "8")
    monkeypatch.setenv("COST_SYNTH_PRO_EXTRA", "10")  # report cost = 10 + 8*4 + 10 = 52
    from ebsearch import account
    account.reload_settings()
    account.init_db()
    account.create_invite_code("WELCOME", credits=200, max_uses=100)
    return account


@pytest.fixture()
def app_mod(env):
    from ebsearch.server import app as app_mod
    importlib.reload(app_mod)
    return app_mod


@pytest.fixture()
def client(app_mod):
    return TestClient(app_mod.create_app())


def _activate(client, code="WELCOME"):
    r = client.post("/api/auth/activate", json={"code": code})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def _auth(t):
    return {"Authorization": "Bearer " + t}


def _fake_research(topic, cfg=None, *, logger=None):
    report = types.SimpleNamespace(to_dict=lambda: {"topic": topic, "generated_at": "now",
                                                    "report_type": "general"})
    return types.SimpleNamespace(markdown="# report", report=report,
                                 n_candidates=10, n_selected=4, n_summarized=4,
                                 stages={})


# --------------------------------------------------------------------------- #
# Per-user report quota — 429 past the cap, no charge for throttled requests
# --------------------------------------------------------------------------- #
def test_user_report_quota_429_after_cap(client, app_mod, monkeypatch):
    monkeypatch.setattr(app_mod, "research", _fake_research)
    token = _activate(client)
    for _ in range(2):  # cap is 2/hour in this harness
        assert client.post("/api/research", json={"topic": "主题"},
                           headers=_auth(token)).status_code == 202
    r = client.post("/api/research", json={"topic": "主题"}, headers=_auth(token))
    assert r.status_code == 429
    assert "请求过于频繁" in r.json()["detail"]
    assert "2" in r.json()["detail"]  # the cap is named in the message
    # Throttled BEFORE the credit gate: only the two accepted runs were charged.
    assert client.get("/api/auth/me", headers=_auth(token)).json()["user"]["credits"] == 96


def test_quota_is_per_user(client, app_mod, monkeypatch):
    monkeypatch.setattr(app_mod, "research", _fake_research)
    a = _activate(client)
    b = _activate(client)
    for _ in range(2):
        assert client.post("/api/research", json={"topic": "A"},
                           headers=_auth(a)).status_code == 202
    assert client.post("/api/research", json={"topic": "A"},
                       headers=_auth(a)).status_code == 429
    # A different user still has a full window.
    assert client.post("/api/research", json={"topic": "B"},
                       headers=_auth(b)).status_code == 202


def test_quota_disabled_when_zero(env, monkeypatch):
    monkeypatch.setenv("EBS_USER_REPORTS_PER_HOUR", "0")
    from ebsearch.server import app as app_mod
    importlib.reload(app_mod)
    monkeypatch.setattr(app_mod, "research", _fake_research)
    client = TestClient(app_mod.create_app())
    token = _activate(client)
    for _ in range(3):  # 3 * 52 = 156 <= 200 credits, and no quota in the way
        assert client.post("/api/research", json={"topic": "x"},
                           headers=_auth(token)).status_code == 202


# --------------------------------------------------------------------------- #
# Fan-out: HTTP 451 from the backend (creator opt-out) -> graceful skip
# --------------------------------------------------------------------------- #
def _sh(bvid):
    return ScoredHit(hit=VideoHit(bvid=bvid, title="视频 " + bvid,
                                  url="https://www.bilibili.com/video/" + bvid))


def _http_451(url):
    return urllib.error.HTTPError(url, 451, "Unavailable For Legal Reasons", None, None)


def test_fanout_451_marks_video_skipped(monkeypatch):
    # The raw urllib transport raises HTTPError 451 (what a real backend
    # rejection looks like); _submit_one must catch it and skip gracefully.
    def _urlopen(req, timeout=None):
        raise _http_451(req.full_url)

    monkeypatch.setattr(fanout.urllib.request, "urlopen", _urlopen)
    cfg = types.SimpleNamespace(backend_url="http://backend", backend_api_key="k",
                                allow_asr=True)
    out = fanout.summarize_selected([_sh("BV1A2b3C4d5E")], cfg, client=None,
                                    poll_interval=0.01, per_video_timeout=0.2)
    assert len(out) == 1
    assert out[0].ok is False
    assert out[0].error == "已按创作者要求跳过"


def test_fanout_451_does_not_affect_other_videos(monkeypatch):
    blocked, fine = "BV1A2b3C4d5E", "BV9z8Y7x6W5v"

    def _post(url, body, key, timeout=30.0):
        if blocked in (body.get("url") or ""):
            raise _http_451(url)
        return {"job_id": "job-ok"}

    def _get(url, key, timeout=30.0):
        return {"status": "done",
                "result": {"summary": {"tldr": "好"},
                           "transcript": {"origin": "subtitle"}}}

    monkeypatch.setattr(fanout, "_post", _post)
    monkeypatch.setattr(fanout, "_get", _get)
    cfg = types.SimpleNamespace(backend_url="http://backend", backend_api_key="k",
                                allow_asr=True)
    out = fanout.summarize_selected([_sh(blocked), _sh(fine)], cfg, client=None,
                                    poll_interval=0.01, per_video_timeout=10.0)
    by_bvid = {v.bvid: v for v in out}
    assert by_bvid[blocked].ok is False
    assert by_bvid[blocked].error == "已按创作者要求跳过"
    assert by_bvid[fine].ok is True and by_bvid[fine].summary == {"tldr": "好"}
