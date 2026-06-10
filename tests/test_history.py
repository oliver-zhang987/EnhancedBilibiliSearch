"""Per-user history persistence + endpoints (offline; storage redirected to tmp).

History is partitioned by the authenticated user's id (access-code accounts),
not by the old X-Client-Id header. Unique coverage kept from the original file:
cross-user delete protection and the path-traversal id guard.
"""
import pytest

pytest.importorskip("fastapi", exc_type=ImportError)
from fastapi.testclient import TestClient  # noqa: E402


def _setup(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTH_DB_PATH", str(tmp_path / "users.db"))
    monkeypatch.setenv("AUTH_JWT_SECRET", "test-secret")
    from ebsearch import account
    account.reload_settings()
    account.init_db()
    account.create_invite_code("WELCOME", credits=1000, max_uses=100)

    from ebsearch.server import app as appmod
    monkeypatch.setattr(appmod, "_HIST_ROOT", tmp_path / "history")
    client = TestClient(appmod.create_app())

    def auth():
        r = client.post("/api/auth/activate", json={"code": "WELCOME"})
        assert r.status_code == 200, r.text
        d = r.json()
        return {"Authorization": "Bearer " + d["token"]}, d["user"]["id"]

    return appmod, client, auth


def test_history_roundtrip_and_isolation(tmp_path, monkeypatch):
    appmod, client, auth = _setup(tmp_path, monkeypatch)
    ha, uid_a = auth()
    hb, _ = auth()

    # save a report owned by user A (owner = str(user_id), as _run_job does)
    job = appmod._Job("abc123", "测试主题", owner=str(uid_a), user_id=uid_a)
    appmod._hist_save(job, {
        "markdown": "# 报告\n内容",
        "report": {"generated_at": "2026-01-01T00:00:00+00:00"},
        "n_candidates": 10, "n_selected": 3, "n_summarized": 3,
    })

    # user A sees it
    lst = client.get("/api/history", headers=ha).json()
    assert any(it["id"] == "abc123" and it["topic"] == "测试主题" for it in lst)
    assert client.get("/api/history/abc123", headers=ha).status_code == 200

    # user B must NOT see or fetch user A's report (the whole point)
    assert client.get("/api/history", headers=hb).json() == []
    assert client.get("/api/history/abc123", headers=hb).status_code == 404
    # ...and can't delete it either
    client.delete("/api/history/abc123", headers=hb)
    assert client.get("/api/history/abc123", headers=ha).status_code == 200

    # user A can delete their own
    assert client.delete("/api/history/abc123", headers=ha).json()["ok"] is True
    assert client.get("/api/history/abc123", headers=ha).status_code == 404


def test_history_id_guard(tmp_path, monkeypatch):
    _, client, auth = _setup(tmp_path, monkeypatch)
    h, _ = auth()
    assert client.get("/api/history/..%2f..%2fetc%2fpasswd", headers=h).status_code in (400, 404)
    assert client.get("/api/history/not_there", headers=h).status_code == 404
    # unauthenticated history access is rejected outright
    assert client.get("/api/history").status_code == 401
