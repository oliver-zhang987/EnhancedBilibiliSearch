"""History persistence + endpoints (offline; redirects storage to a tmp dir)."""
import pytest

pytest.importorskip("fastapi", exc_type=ImportError)
from fastapi.testclient import TestClient  # noqa: E402


def _appmod(tmp_path, monkeypatch):
    monkeypatch.delenv("EBS_SERVER_API_KEY", raising=False)  # auth off for the test
    from ebsearch.server import app as appmod
    # redirect history storage to tmp (endpoints + _hist_save read these at call time)
    monkeypatch.setattr(appmod, "_HIST_DIR", tmp_path / "history")
    monkeypatch.setattr(appmod, "_HIST_INDEX", tmp_path / "index.json")
    return appmod


def test_history_roundtrip(tmp_path, monkeypatch):
    appmod = _appmod(tmp_path, monkeypatch)
    job = appmod._Job("abc123", "测试主题")
    appmod._hist_save(job, {
        "markdown": "# 报告\n内容〔BVxxxx〕",
        "report": {"generated_at": "2026-01-01T00:00:00+00:00"},
        "n_candidates": 10, "n_selected": 3, "n_summarized": 3,
    })
    client = TestClient(appmod.app)

    lst = client.get("/api/history").json()
    assert any(it["id"] == "abc123" and it["topic"] == "测试主题"
               and it["n_summarized"] == 3 for it in lst)

    rec = client.get("/api/history/abc123")
    assert rec.status_code == 200
    assert rec.json()["markdown"].startswith("# 报告")

    assert client.delete("/api/history/abc123").json()["ok"] is True
    assert client.get("/api/history/abc123").status_code == 404


def test_history_id_guard(tmp_path, monkeypatch):
    appmod = _appmod(tmp_path, monkeypatch)
    client = TestClient(appmod.app)
    # path-traversal-ish ids must not escape; non-existent -> 404, never 500/leak
    assert client.get("/api/history/..%2f..%2fetc%2fpasswd").status_code in (400, 404)
    assert client.get("/api/history/not_there").status_code == 404
