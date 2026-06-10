"""FastAPI surface — OFFLINE. The pipeline's research() is monkeypatched to a stub,
so no network/LLM/backend call ever happens. Skipped entirely if fastapi is absent
(it's an optional 'server' dep); the core suite stays hermetic either way.

Auth model under test: access-code-only (anonymous account + Bearer JWT). The old
X-API-Key gate was replaced by the account subsystem; these tests activate a user
via the mock /api/auth/activate flow and exercise the API as that user.
"""
from __future__ import annotations

import time

import pytest

pytest.importorskip("fastapi", exc_type=ImportError)  # server extra absent -> skip

from fastapi.testclient import TestClient  # noqa: E402

from ebsearch.models import TopicReport  # noqa: E402


class _FakeResult:
    def __init__(self, topic):
        self.markdown = f"# {topic} — 报告\n\n## 概览\n\n测试。\n"
        self.report = TopicReport(topic=topic, overview="测试。")
        self.n_candidates = 10
        self.n_selected = 3
        self.n_summarized = 2
        self.stages = {"selected": [], "failed": []}


def _fake_research(topic, cfg=None, *, logger=None):
    if logger:
        logger("search: 10 candidates")
        logger("select: 3 chosen")
        logger("summarize: 2/2 ok")
    return _FakeResult(topic)


@pytest.fixture
def appmod(tmp_path, monkeypatch):
    """Server module with an isolated account DB + the pipeline stubbed out."""
    monkeypatch.setenv("AUTH_DB_PATH", str(tmp_path / "users.db"))
    monkeypatch.setenv("AUTH_JWT_SECRET", "test-secret")
    monkeypatch.setenv("EBS_DATA_DIR", str(tmp_path / "data"))
    from ebsearch import account
    account.reload_settings()
    account.init_db()
    account.create_invite_code("WELCOME", credits=1000, max_uses=100)

    import ebsearch.server.app as appmod
    monkeypatch.setattr(appmod, "research", _fake_research)
    return appmod


@pytest.fixture
def client(appmod):
    return TestClient(appmod.create_app())


def _auth(client):
    """Activate an anonymous account; return the Bearer headers."""
    r = client.post("/api/auth/activate", json={"code": "WELCOME"})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["token"]}


def _wait_done(client, job_id, headers, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = client.get(f"/api/research/{job_id}", headers=headers)
        data = r.json()
        if data["status"] in ("done", "error"):
            return data
        time.sleep(0.02)
    raise AssertionError("job did not finish in time")


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200 and r.json() == {"ok": True}


def test_index_served(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "B站主题" in r.text  # the standalone web page


def test_research_lifecycle(client):
    h = _auth(client)
    r = client.post("/api/research", json={"topic": "RAG 检索增强", "max_videos": 3}, headers=h)
    assert r.status_code == 202
    job_id = r.json()["job_id"]
    assert job_id

    data = _wait_done(client, job_id, h)
    assert data["status"] == "done"
    assert "## 概览" in data["markdown"]
    assert data["report"]["topic"] == "RAG 检索增强"
    assert data["n_candidates"] == 10
    assert data["n_selected"] == 3
    assert data["n_summarized"] == 2
    # logger lines captured into progress
    assert any("candidates" in line for line in data["progress"])


def test_empty_topic_rejected(client):
    r = client.post("/api/research", json={"topic": "   "}, headers=_auth(client))
    assert r.status_code == 400


def test_unknown_job_404(client):
    r = client.get("/api/research/does-not-exist", headers=_auth(client))
    assert r.status_code == 404


def test_overrides_passed_to_config(appmod, monkeypatch):
    captured = {}

    def _capture_research(topic, cfg=None, *, logger=None):
        captured["max_videos"] = cfg.max_videos
        captured["duration_filter"] = cfg.duration_filter
        captured["allow_asr"] = cfg.allow_asr
        captured["query_expand"] = cfg.query_expand
        captured["allow_llm_rerank"] = cfg.allow_llm_rerank
        return _FakeResult(topic)

    monkeypatch.setattr(appmod, "research", _capture_research)
    c = TestClient(appmod.create_app())
    h = _auth(c)
    r = c.post("/api/research", json={
        "topic": "X", "max_videos": 9, "duration_filter": 4,
        "allow_asr": True, "query_expand": True, "allow_llm_rerank": True,
    }, headers=h)
    job_id = r.json()["job_id"]
    _wait_done(c, job_id, h)
    assert captured == {
        "max_videos": 9, "duration_filter": 4, "allow_asr": True,
        "query_expand": True, "allow_llm_rerank": True,
    }


def test_token_required(client):
    """No/invalid Bearer token -> 401; the page + health stay public."""
    assert client.post("/api/research", json={"topic": "X"}).status_code == 401
    assert client.post("/api/research", json={"topic": "X"},
                       headers={"Authorization": "Bearer not-a-token"}).status_code == 401
    h = _auth(client)
    r = client.post("/api/research", json={"topic": "X"}, headers=h)
    assert r.status_code == 202
    # the job is private to its creator: polling without the token is rejected
    job_id = r.json()["job_id"]
    assert client.get(f"/api/research/{job_id}").status_code == 401
    assert _wait_done(client, job_id, h)["status"] == "done"
    assert client.get("/health").status_code == 200
