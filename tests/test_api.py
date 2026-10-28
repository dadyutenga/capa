import asyncio

from fastapi.testclient import TestClient

import backend.app as appmod
from backend.runner import RunnerManager


def client_for(tmp_path, monkeypatch):
    monkeypatch.setattr(appmod, "DB_PATH", tmp_path / "capacity.sqlite3")
    appmod.runner = RunnerManager(appmod._on_run_update)
    return TestClient(appmod.app)


def test_external_target_requires_attestation_and_dns_proof_before_run(tmp_path, monkeypatch):
    with client_for(tmp_path, monkeypatch) as client:
        workspace = client.get("/api/workspace").json()
        project = client.post("/api/projects", json={"name": "Staging service", "environment": "staging"}).json()
        denied = client.post(f"/api/projects/{project['id']}/targets", json={
            "name": "Unapproved target", "kind": "http", "base_url": "https://staging.example.com", "authorization_confirmed": False,
        })
        assert denied.status_code == 403
        created = client.post(f"/api/projects/{project['id']}/targets", json={
            "name": "Authorized staging", "kind": "http", "base_url": "https://staging.example.com:8443", "authorization_confirmed": True,
        })
        assert created.status_code == 200
        target = created.json()
        assert target["origin_key"] == "https://staging.example.com:8443"
        assert target["verified"] == 0
        journey = client.post(f"/api/projects/{project['id']}/journeys", json={"name": "Landing", "steps": [{"method": "GET", "path": "/"}]}).json()
        denied_start = client.post("/api/tests/start", json={
            "target_id": target["id"], "journey_id": journey["id"],
            "authorization_confirmed": True, "concurrency": 1, "ramp_seconds": 0,
            "duration_seconds": 2, "max_requests": 10, "rate_limit_rps": 1,
            "threshold_p95_ms": 1000, "threshold_error_percent": 2,
        })
        assert denied_start.status_code == 403  # unverified and from a different project
        assert not appmod.runner.active


def test_journey_rejects_user_supplied_code(tmp_path, monkeypatch):
    with client_for(tmp_path, monkeypatch) as client:
        project = client.post("/api/projects", json={"name": "Stage A", "environment": "staging"}).json()
        response = client.post(f"/api/projects/{project['id']}/journeys", json={
            "name": "bad journey", "steps": [{"method": "GET", "path": "/", "script": "fetch('http://internal')"}],
        })
        assert response.status_code == 422


def test_server_caps_are_enforced_by_api(tmp_path, monkeypatch):
    with client_for(tmp_path, monkeypatch) as client:
        workspace = client.get("/api/workspace").json()
        target = workspace["targets"][0]
        journey = workspace["journeys"][0]
        response = client.post("/api/tests/start", json={
            "target_id": target["id"], "journey_id": journey["id"], "concurrency": 51,
            "ramp_seconds": 0, "duration_seconds": 2, "max_requests": 10,
            "rate_limit_rps": 1, "threshold_p95_ms": 1000, "threshold_error_percent": 2,
        })
        assert response.status_code == 422
        assert not appmod.runner.active


def test_kill_switch_prevents_simulator_run(tmp_path, monkeypatch):
    with client_for(tmp_path, monkeypatch) as client:
        workspace = client.get("/api/workspace").json()
        client.post("/api/kill-switch", json={"active": True})
        target = workspace["targets"][0]
        journey = workspace["journeys"][0]
        response = client.post("/api/tests/start", json={
            "target_id": target["id"], "journey_id": journey["id"], "concurrency": 1,
            "ramp_seconds": 0, "duration_seconds": 2, "max_requests": 10,
            "rate_limit_rps": 1, "threshold_p95_ms": 1000, "threshold_error_percent": 2,
        })
        assert response.status_code == 423
        assert not appmod.runner.active
        client.post("/api/kill-switch", json={"active": False})


def test_simulator_run_persists_metrics_and_exports_csv(tmp_path, monkeypatch):
    import time
    with client_for(tmp_path, monkeypatch) as client:
        workspace = client.get("/api/workspace").json()
        target = workspace["targets"][0]
        journey = workspace["journeys"][0]
        started = client.post("/api/tests/start", json={
            "target_id": target["id"], "journey_id": journey["id"], "concurrency": 2,
            "ramp_seconds": 0, "duration_seconds": 3, "max_requests": 4,
            "rate_limit_rps": 20, "threshold_p95_ms": 1000, "threshold_error_percent": 2,
        })
        assert started.status_code == 200, started.text
        test_id = started.json()["id"]
        deadline = time.monotonic() + 3
        result = None
        while time.monotonic() < deadline:
            result = client.get(f"/api/tests/{test_id}").json()
            if result["status"] in {"completed", "stopped", "failed"}:
                break
            time.sleep(0.03)
        assert result is not None
        assert result["status"] == "completed"
        assert result["stats"]["requests_started"] <= 4
        assert result["stats"]["completed"] == result["stats"]["requests_started"]
        assert result["stats"]["p95_ms"] >= 0
        exported = client.get(f"/api/tests/{test_id}/export.csv")
        assert exported.status_code == 200
        assert "p95_ms" in exported.text
        assert f"{test_id}" in exported.text
