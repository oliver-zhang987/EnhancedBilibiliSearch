"""Per-client history persistence + endpoints (offline; storage redirected to tmp)."""
import pytest

pytest.importorskip("fastapi", exc_type=ImportError)
from fastapi.testclient import TestClient  # noqa: E402

A = {"X-Client-Id": "clientA"}
B = {"X-Client-Id": "clientB"}


def _appmod(tmp_path, monkeypatch):
    monkeypatch.delenv("EBS_SERVER_API_KEY", raising=False)  # auth off for the test
    from ebsearch.server import app as appmod
    monkeypatch.setattr(appmod, "_HIST_ROOT", tmp_path / "history")
    return appmod


def test_history_roundtrip_and_isolation(tmp_path, monkeypatch):
    appmod = _appmod(tmp_path, monkeypatch)
    # save a report owned by clientA
    job = appmod._Job("abc123", "测试主题", appmod._owner_id("clientA"))
    appmod._hist_save(job, {
        "markdown": "# 报告\n内容",
        "report": {"generated_at": "2026-01-01T00:00:00+00:00"},
        "n_candidates": 10, "n_selected": 3, "n_summarized": 3,
    })
    client = TestClient(appmod.app)

    # clientA sees it
    lst = client.get("/api/history", headers=A).json()
    assert any(it["id"] == "abc123" and it["topic"] == "测试主题" for it in lst)
    assert client.get("/api/history/abc123", headers=A).status_code == 200

    # clientB must NOT see or fetch clientA's report (the whole point)
    assert client.get("/api/history", headers=B).json() == []
    assert client.get("/api/history/abc123", headers=B).status_code == 404
    # ...and can't delete it either
    client.delete("/api/history/abc123", headers=B)
    assert client.get("/api/history/abc123", headers=A).status_code == 200

    # clientA can delete its own
    assert client.delete("/api/history/abc123", headers=A).json()["ok"] is True
    assert client.get("/api/history/abc123", headers=A).status_code == 404


def test_history_id_guard(tmp_path, monkeypatch):
    appmod = _appmod(tmp_path, monkeypatch)
    client = TestClient(appmod.app)
    assert client.get("/api/history/..%2f..%2fetc%2fpasswd", headers=A).status_code in (400, 404)
    assert client.get("/api/history/not_there", headers=A).status_code == 404
