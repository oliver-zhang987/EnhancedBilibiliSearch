"""End-to-end auth + credits test chain for the EBSearch backend.

Access-code-only auth (no phone/SMS — minimal PIPL footprint). Offline only — the
`research` pipeline is monkeypatched so NO live search/LLM/ASR calls happen.
Exercises: access-code activation, the credit gate on /api/research (401 unauth,
402 under-funded), per-user history isolation, a charged run, and the empty-result
refund.

Run:  python -m pytest tests/test_auth_credits.py -q   (needs fastapi installed)
"""
from __future__ import annotations

import importlib
import time
import types

import pytest

pytest.importorskip("fastapi.testclient")
from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTH_DB_PATH", str(tmp_path / "users.db"))
    monkeypatch.setenv("AUTH_JWT_SECRET", "test-secret")
    monkeypatch.setenv("EBS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("EBS_MAX_VIDEOS", "4")
    monkeypatch.setenv("EBS_SYNTH_MODEL", "deepseek-v4-pro")
    monkeypatch.setenv("COST_REPORT_BASE", "10")
    monkeypatch.setenv("COST_REPORT_PER_VIDEO", "8")
    monkeypatch.setenv("COST_SYNTH_PRO_EXTRA", "10")  # report cost = 10 + 8*4 + 10 = 52
    from ebsearch import account
    account.reload_settings()
    account.init_db()
    account.create_invite_code("WELCOME", credits=200, max_uses=100)
    account.create_invite_code("BROKE", credits=5, max_uses=100)
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


def _fake_research(n_summarized):
    report = types.SimpleNamespace(to_dict=lambda: {"topic": "t", "generated_at": "now",
                                                     "report_type": "comparison"})
    return types.SimpleNamespace(markdown="# report", report=report,
                                 n_candidates=10, n_selected=4, n_summarized=n_summarized,
                                 stages={})


def _wait(client, job_id, headers, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        st = client.get(f"/api/research/{job_id}", headers=headers).json()
        if st["status"] in ("done", "error"):
            return st
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def test_unauth_401(client):
    assert client.post("/api/research", json={"topic": "x"}).status_code == 401


def test_bad_access_code_400(client):
    assert client.post("/api/auth/activate", json={"code": "NOPE"}).status_code == 400


def test_under_funded_402(client):
    token = _activate(client, "BROKE")
    r = client.post("/api/research", json={"topic": "对比 A vs B"}, headers=_auth(token))
    assert r.status_code == 402
    assert r.json()["detail"]["required"] == 52


def test_success_charges_and_saves_history(client, app_mod, monkeypatch):
    token = _activate(client)
    monkeypatch.setattr(app_mod, "research", lambda *a, **k: _fake_research(4))
    jid = client.post("/api/research", json={"topic": "对比 A vs B"},
                      headers=_auth(token)).json()["job_id"]
    assert _wait(client, jid, _auth(token))["status"] == "done"
    assert client.get("/api/auth/me", headers=_auth(token)).json()["user"]["credits"] == 148  # 200-52
    hist = client.get("/api/history", headers=_auth(token)).json()
    assert len(hist) == 1 and hist[0]["topic"] == "对比 A vs B"


def test_empty_result_is_refunded(client, app_mod, monkeypatch):
    token = _activate(client)
    monkeypatch.setattr(app_mod, "research", lambda *a, **k: _fake_research(0))
    jid = client.post("/api/research", json={"topic": "no results"},
                      headers=_auth(token)).json()["job_id"]
    _wait(client, jid, _auth(token))
    assert client.get("/api/auth/me", headers=_auth(token)).json()["user"]["credits"] == 200


def test_history_is_per_user(client, app_mod, monkeypatch):
    monkeypatch.setattr(app_mod, "research", lambda *a, **k: _fake_research(4))
    a = _activate(client)
    jid = client.post("/api/research", json={"topic": "A's topic"}, headers=_auth(a)).json()["job_id"]
    _wait(client, jid, _auth(a))
    b = _activate(client)
    assert client.get("/api/history", headers=_auth(b)).json() == []
    assert client.get(f"/api/research/{jid}", headers=_auth(b)).status_code == 404
