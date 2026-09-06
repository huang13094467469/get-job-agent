"""health 端点测试：不依赖真实基础设施，monkeypatch 连通检查。

（Ollama/Qdrant 已随「只写不读」的简历向量化管线移除，现仅探 PostgreSQL。）
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from get_job_agent.main import create_app


def _patch_checks(monkeypatch, ok: bool) -> None:
    from get_job_agent.infra import postgres

    async def fake(settings) -> bool:
        return ok

    monkeypatch.setattr(postgres, "check_postgres", fake)


def test_health_ok(monkeypatch) -> None:
    _patch_checks(monkeypatch, ok=True)
    client = TestClient(create_app())
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["components"]["postgres"]["ok"] is True


def test_health_degraded(monkeypatch) -> None:
    _patch_checks(monkeypatch, ok=False)
    client = TestClient(create_app())
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "degraded"
    assert body["components"]["postgres"]["ok"] is False