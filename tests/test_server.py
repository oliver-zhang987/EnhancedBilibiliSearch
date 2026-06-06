"""FastAPI surface — OFFLINE. The pipeline's research() is monkeypatched to a stub,
so no network/LLM/backend call ever happens. Skipped entirely if fastapi is absent
(it's an optional 'server' dep); the core suite stays hermetic either way."""
from __future__ import annotations

import time

import pytest

pytest.importorskip("fastapi", exc_type=ImportError)  # server extra absent -> skip

from fastapi.testclient import TestClient  # noqa: E402

from ebsearch.models import TopicReport  # noqa: E402
import ebsearch.server.app as appmod  # noqa: E402


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
def client(monkeypatch):
    # Patch the symbol the app actually calls (imported into the server module).
    monkeypatch.setattr(appmod, "research", _fake_research)
    monkeypatch.delenv("EBS_SERVER_API_KEY", raising=False)
    return TestClient(appmod.create_app())


def _wait_done(client, job_id, headers=None, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = client.get(f"/api/research/{job_id}", headers=headers or {})
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
    assert "B站主题综合报告" in r.text  # the standalone web page


def test_research_lifecycle(client):
    r = client.post("/api/research", json={"topic": "RAG 检索增强", "max_videos": 3})
    assert r.status_code == 202
    job_id = r.json()["job_id"]
    assert job_id

    data = _wait_done(client, job_id)
    assert data["status"] == "done"
    assert "## 概览" in data["markdown"]
    assert data["report"]["topic"] == "RAG 检索增强"
    assert data["n_candidates"] == 10
    assert data["n_selected"] == 3
    assert data["n_summarized"] == 2
    # logger lines captured into progress
    assert any("candidates" in line for line in data["progress"])


def test_empty_topic_rejected(client):
    r = client.post("/api/research", json={"topic": "   "})
    assert r.status_code == 400


def test_unknown_job_404(client):
    r = client.get("/api/research/does-not-exist")
    assert r.status_code == 404


def test_overrides_passed_to_config(monkeypatch):
    captured = {}

    def _capture_research(topic, cfg=None, *, logger=None):
        captured["max_videos"] = cfg.max_videos
        captured["duration_filter"] = cfg.duration_filter
        captured["allow_asr"] = cfg.allow_asr
        captured["query_expand"] = cfg.query_expand
        captured["allow_llm_rerank"] = cfg.allow_llm_rerank
        return _FakeResult(topic)

    monkeypatch.setattr(appmod, "research", _capture_research)
    monkeypatch.delenv("EBS_SERVER_API_KEY", raising=False)
    c = TestClient(appmod.create_app())
    r = c.post("/api/research", json={
        "topic": "X", "max_videos": 9, "duration_filter": 4,
        "allow_asr": True, "query_expand": True, "allow_llm_rerank": True,
    })
    job_id = r.json()["job_id"]
    _wait_done(c, job_id)
    assert captured == {
        "max_videos": 9, "duration_filter": 4, "allow_asr": True,
        "query_expand": True, "allow_llm_rerank": True,
    }


def test_api_key_required_when_env_set(monkeypatch):
    monkeypatch.setattr(appmod, "research", _fake_research)
    monkeypatch.setenv("EBS_SERVER_API_KEY", "secret123")
    c = TestClient(appmod.create_app())

    # missing key -> 401
    assert c.post("/api/research", json={"topic": "X"}).status_code == 401
    # wrong key -> 401
    assert c.post("/api/research", json={"topic": "X"},
                  headers={"X-API-Key": "nope"}).status_code == 401
    # correct key -> 202, and the same key required to poll
    r = c.post("/api/research", json={"topic": "X"},
               headers={"X-API-Key": "secret123"})
    assert r.status_code == 202
    job_id = r.json()["job_id"]
    assert c.get(f"/api/research/{job_id}").status_code == 401
    data = _wait_done(c, job_id, headers={"X-API-Key": "secret123"})
    assert data["status"] == "done"
    # health stays open (no /api prefix)
    assert c.get("/health").status_code == 200
