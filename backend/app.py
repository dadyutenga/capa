from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import csv
import io
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from .runner import (
    MAX_CONCURRENCY,
    MAX_DURATION_SECONDS,
    MAX_REQUESTS,
    MAX_RESPONSE_BYTES_PER_TEST,
    MAX_RPS,
    RunnerError,
    RunnerManager,
    validate_run_config,
)
from .safety import (
    SafetyError,
    challenge_record_name,
    new_challenge,
    parse_origin,
    resolve_public_addresses,
    validate_steps,
    verify_dns_challenge,
)

APP_DIR = Path(__file__).resolve().parent.parent
DB_PATH = Path(os.environ.get("CAPACITY_LAB_DB", APP_DIR / "data" / "capacity_lab.sqlite3"))

@asynccontextmanager
async def lifespan(_app: FastAPI):
    _init_db()
    runner.kill_switch_active = _load_setting("kill_switch_active", "0") == "1"
    yield


app = FastAPI(title="Capacity Lab API", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:5173", "http://localhost:5173"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type"],
)


class ProjectCreate(BaseModel):
    name: str = Field(min_length=2, max_length=80)
    environment: Literal["staging", "simulation"] = "staging"


class TargetCreate(BaseModel):
    name: str = Field(min_length=2, max_length=80)
    kind: Literal["http", "simulator"] = "http"
    base_url: str | None = Field(default=None, max_length=512)
    authorization_confirmed: bool = False


class JourneyCreate(BaseModel):
    name: str = Field(min_length=2, max_length=80)
    steps: list[dict[str, Any]]


class RunStart(BaseModel):
    target_id: int
    journey_id: int
    authorization_confirmed: bool = False
    concurrency: int = 5
    ramp_seconds: int = 10
    duration_seconds: int = 60
    max_requests: int = 1_000
    rate_limit_rps: int = 5
    threshold_p95_ms: int = 1_000
    threshold_error_percent: float = 2.0


class KillSwitchUpdate(BaseModel):
    active: bool


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DB_PATH, timeout=5.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row(row: sqlite3.Row | None) -> dict | None:
    return dict(row) if row else None


def _init_db() -> None:
    with _connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS projects (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              name TEXT NOT NULL,
              environment TEXT NOT NULL CHECK(environment IN ('staging','simulation')),
              created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS targets (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
              name TEXT NOT NULL,
              kind TEXT NOT NULL CHECK(kind IN ('http','simulator')),
              base_url TEXT NOT NULL,
              origin_key TEXT,
              challenge TEXT,
              verified INTEGER NOT NULL DEFAULT 0,
              authorization_confirmed INTEGER NOT NULL DEFAULT 0,
              verified_at TEXT,
              created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS journeys (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
              name TEXT NOT NULL,
              steps_json TEXT NOT NULL,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS tests (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
              target_id INTEGER NOT NULL REFERENCES targets(id),
              journey_id INTEGER NOT NULL REFERENCES journeys(id),
              status TEXT NOT NULL,
              config_json TEXT NOT NULL,
              stats_json TEXT NOT NULL,
              created_at TEXT NOT NULL,
              started_at TEXT,
              finished_at TEXT,
              stop_reason TEXT
            );
            CREATE TABLE IF NOT EXISTS settings (
              key TEXT PRIMARY KEY,
              value TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS tests_project_created ON tests(project_id, created_at DESC);
            """
        )
        if conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 0:
            now = _now()
            project_id = conn.execute(
                "INSERT INTO projects(name,environment,created_at) VALUES(?,?,?)",
                ("Demo workspace", "simulation", now),
            ).lastrowid
            target_id = conn.execute(
                "INSERT INTO targets(project_id,name,kind,base_url,verified,authorization_confirmed,verified_at,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (project_id, "Local simulator", "simulator", "simulator://local", 1, 1, now, now),
            ).lastrowid
            conn.execute(
                "INSERT INTO journeys(project_id,name,steps_json,created_at,updated_at) VALUES(?,?,?,?,?)",
                (project_id, "Browse catalog", json.dumps([
                    {"method": "GET", "path": "/"},
                    {"method": "GET", "path": "/catalog"},
                    {"method": "POST", "path": "/api/cart", "headers": {"Content-Type": "application/json"}, "body": "{\"sku\":\"demo\",\"quantity\":1}"},
                ]), now, now),
            )
        conn.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('kill_switch_active','0')")


def _load_setting(key: str, default: str = "") -> str:
    with _connect() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default


def _set_setting(key: str, value: str) -> None:
    with _connect() as conn:
        conn.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


async def _on_run_update(test_id: int, status: str, stats: dict) -> None:
    with _connect() as conn:
        row = conn.execute("SELECT id FROM tests WHERE id=?", (test_id,)).fetchone()
        if not row:
            return
        conn.execute(
            "UPDATE tests SET status=?, stats_json=?, started_at=COALESCE(started_at,?), finished_at=?, stop_reason=? WHERE id=?",
            (status, json.dumps(stats), stats.get("started_at"), stats.get("finished_at"), stats.get("stop_reason"), test_id),
        )


runner = RunnerManager(_on_run_update)


def _safe_http_target(target: dict) -> None:
    if target["kind"] != "http":
        return
    origin = parse_origin(target["base_url"])
    if origin.key != target["origin_key"]:
        raise HTTPException(status_code=409, detail="Stored target origin changed; re-create and re-verify it.")
    if not target["verified"] or not target["authorization_confirmed"]:
        raise HTTPException(status_code=403, detail="Target ownership and explicit authorization are required before traffic.")


def _target_public(row: sqlite3.Row | dict) -> dict:
    target = dict(row)
    # The challenge must be shown once in the local UI so the operator can publish the TXT record.
    if target.get("kind") == "http" and target.get("challenge") and not target.get("verified"):
        origin = parse_origin(target["base_url"])
        target["challenge_record_name"] = challenge_record_name(origin.host)
        target["challenge_record_value"] = f"capacity-lab-verification={target['challenge']}"
    else:
        target["challenge_record_name"] = None
        target["challenge_record_value"] = None
    return target


@app.get("/api/health")
async def health() -> dict:
    return {"ok": True, "service": "capacity-lab", "safety_tests_required": True}


@app.get("/api/safety")
async def safety_status() -> dict:
    return {
        "kill_switch_active": runner.kill_switch_active,
        "single_operator_local_mode": True,
        "caps": {
            "concurrency": MAX_CONCURRENCY,
            "duration_seconds": MAX_DURATION_SECONDS,
            "requests_per_test": MAX_REQUESTS,
            "requests_per_second": MAX_RPS,
            "response_bytes_per_test": MAX_RESPONSE_BYTES_PER_TEST,
        },
        "allowed_external_environment": "staging only",
    }


@app.get("/api/workspace")
async def workspace(project_id: int | None = None) -> dict:
    with _connect() as conn:
        projects = [dict(row) for row in conn.execute("SELECT * FROM projects ORDER BY id")]
        if not projects:
            return {"projects": [], "selected_project_id": None, "targets": [], "journeys": [], "tests": [], "safety": await safety_status()}
        valid_ids = {item["id"] for item in projects}
        selected = project_id if project_id in valid_ids else projects[0]["id"]
        targets = [_target_public(row) for row in conn.execute("SELECT * FROM targets WHERE project_id=? ORDER BY id DESC", (selected,))]
        journeys = []
        for row in conn.execute("SELECT * FROM journeys WHERE project_id=? ORDER BY updated_at DESC", (selected,)):
            item = dict(row)
            item["steps"] = json.loads(item.pop("steps_json"))
            journeys.append(item)
        tests = []
        for row in conn.execute(
            "SELECT t.*, a.name AS target_name, j.name AS journey_name FROM tests t JOIN targets a ON a.id=t.target_id JOIN journeys j ON j.id=t.journey_id WHERE t.project_id=? ORDER BY t.id DESC LIMIT 100",
            (selected,),
        ):
            item = dict(row)
            item["config"] = json.loads(item.pop("config_json"))
            item["stats"] = json.loads(item.pop("stats_json"))
            tests.append(item)
    return {
        "projects": projects,
        "selected_project_id": selected,
        "targets": targets,
        "journeys": journeys,
        "tests": tests,
        "safety": await safety_status(),
    }


@app.post("/api/projects")
async def create_project(body: ProjectCreate) -> dict:
    with _connect() as conn:
        cursor = conn.execute("INSERT INTO projects(name,environment,created_at) VALUES(?,?,?)", (body.name.strip(), body.environment, _now()))
        project_id = cursor.lastrowid
        return dict(conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone())


@app.delete("/api/projects/{project_id}")
async def delete_project(project_id: int) -> dict:
    with _connect() as conn:
        history = conn.execute("SELECT COUNT(*) FROM tests WHERE project_id=?", (project_id,)).fetchone()[0]
        if history:
            raise HTTPException(status_code=409, detail="Projects with test history cannot be deleted; export the history first.")
        cursor = conn.execute("DELETE FROM projects WHERE id=?", (project_id,))
        if not cursor.rowcount:
            raise HTTPException(status_code=404, detail="Project not found.")
        return {"deleted": True}


@app.post("/api/projects/{project_id}/targets")
async def create_target(project_id: int, body: TargetCreate) -> dict:
    with _connect() as conn:
        project = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
        if not project:
            raise HTTPException(status_code=404, detail="Project not found.")
        if body.kind == "simulator":
            base_url, origin_key, token, verified, attested = "simulator://local", None, None, 1, 1
        else:
            if project["environment"] != "staging":
                raise HTTPException(status_code=400, detail="External targets can be added only to staging projects.")
            if not body.authorization_confirmed:
                raise HTTPException(status_code=403, detail="Confirm that you own or are explicitly authorized for this exact staging origin.")
            try:
                origin = parse_origin(body.base_url or "")
            except SafetyError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            base_url, origin_key, token, verified, attested = origin.key, origin.key, new_challenge(origin.key), 0, 1
        cursor = conn.execute(
            "INSERT INTO targets(project_id,name,kind,base_url,origin_key,challenge,verified,authorization_confirmed,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (project_id, body.name.strip(), body.kind, base_url, origin_key, token, verified, attested, _now()),
        )
        row = conn.execute("SELECT * FROM targets WHERE id=?", (cursor.lastrowid,)).fetchone()
        return _target_public(row)


@app.delete("/api/targets/{target_id}")
async def delete_target(target_id: int) -> dict:
    with _connect() as conn:
        count = conn.execute("SELECT COUNT(*) FROM tests WHERE target_id=?", (target_id,)).fetchone()[0]
        if count:
            raise HTTPException(status_code=409, detail="Keep targets referenced by test history; disable them in a future version.")
        cursor = conn.execute("DELETE FROM targets WHERE id=?", (target_id,))
        if not cursor.rowcount:
            raise HTTPException(status_code=404, detail="Target not found.")
        return {"deleted": True}


@app.post("/api/targets/{target_id}/verify")
async def verify_target(target_id: int) -> dict:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM targets WHERE id=?", (target_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Target not found.")
        target = dict(row)
        if target["kind"] == "simulator":
            return {"verified": True, "message": "The built-in simulator is local and sends no network traffic."}
        try:
            origin = parse_origin(target["base_url"])
            public_addresses = await asyncio.to_thread(resolve_public_addresses, origin.host, origin.port)
            valid = await asyncio.to_thread(verify_dns_challenge, origin.host, target["challenge"])
            if not valid:
                raise HTTPException(status_code=400, detail="The DNS TXT value does not match the challenge.")
        except SafetyError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        with _connect() as conn2:
            conn2.execute("UPDATE targets SET verified=1,verified_at=? WHERE id=?", (_now(), target_id))
        return {"verified": True, "message": "DNS ownership confirmed; every test still requires a fresh authorization check.", "resolved_addresses_checked": len(public_addresses)}


@app.post("/api/projects/{project_id}/journeys")
async def create_journey(project_id: int, body: JourneyCreate) -> dict:
    try:
        steps = validate_steps(body.steps)
    except SafetyError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    with _connect() as conn:
        if not conn.execute("SELECT 1 FROM projects WHERE id=?", (project_id,)).fetchone():
            raise HTTPException(status_code=404, detail="Project not found.")
        now = _now()
        cursor = conn.execute(
            "INSERT INTO journeys(project_id,name,steps_json,created_at,updated_at) VALUES(?,?,?,?,?)",
            (project_id, body.name.strip(), json.dumps(steps), now, now),
        )
        return {"id": cursor.lastrowid, "project_id": project_id, "name": body.name.strip(), "steps": steps, "created_at": now, "updated_at": now}


@app.put("/api/journeys/{journey_id}")
async def update_journey(journey_id: int, body: JourneyCreate) -> dict:
    try:
        steps = validate_steps(body.steps)
    except SafetyError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    with _connect() as conn:
        cursor = conn.execute("UPDATE journeys SET name=?,steps_json=?,updated_at=? WHERE id=?", (body.name.strip(), json.dumps(steps), _now(), journey_id))
        if not cursor.rowcount:
            raise HTTPException(status_code=404, detail="Journey not found.")
        row = dict(conn.execute("SELECT * FROM journeys WHERE id=?", (journey_id,)).fetchone())
        row["steps"] = json.loads(row.pop("steps_json"))
        return row


@app.delete("/api/journeys/{journey_id}")
async def delete_journey(journey_id: int) -> dict:
    with _connect() as conn:
        count = conn.execute("SELECT COUNT(*) FROM tests WHERE journey_id=?", (journey_id,)).fetchone()[0]
        if count:
            raise HTTPException(status_code=409, detail="Keep journeys referenced by test history.")
        cursor = conn.execute("DELETE FROM journeys WHERE id=?", (journey_id,))
        if not cursor.rowcount:
            raise HTTPException(status_code=404, detail="Journey not found.")
        return {"deleted": True}


@app.post("/api/tests/start")
async def start_test(body: RunStart) -> dict:
    raw_config = {
        "concurrency": body.concurrency,
        "ramp_seconds": body.ramp_seconds,
        "duration_seconds": body.duration_seconds,
        "max_requests": body.max_requests,
        "rate_limit_rps": body.rate_limit_rps,
        "threshold_p95_ms": body.threshold_p95_ms,
        "threshold_error_percent": body.threshold_error_percent,
    }
    try:
        config = validate_run_config(raw_config)
    except RunnerError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if runner.kill_switch_active:
        raise HTTPException(status_code=423, detail="Global kill switch is active.")
    if runner.has_active_run():
        raise HTTPException(status_code=409, detail="A test is already running in this installation.")
    with _connect() as conn:
        target_row = conn.execute("SELECT * FROM targets WHERE id=?", (body.target_id,)).fetchone()
        journey_row = conn.execute("SELECT * FROM journeys WHERE id=?", (body.journey_id,)).fetchone()
        if not target_row or not journey_row:
            raise HTTPException(status_code=404, detail="Target or journey not found.")
        target, journey = dict(target_row), dict(journey_row)
        if target["project_id"] != journey["project_id"]:
            raise HTTPException(status_code=403, detail="Target and journey must belong to the same project.")
        try:
            steps = validate_steps(json.loads(journey["steps_json"]))
        except (SafetyError, json.JSONDecodeError) as exc:
            raise HTTPException(status_code=422, detail="Stored journey failed safety validation.") from exc
        if target["kind"] == "http":
            if not body.authorization_confirmed:
                raise HTTPException(status_code=403, detail="Explicit authorization for this exact origin is required before every external run.")
            if not target["verified"] or not target["authorization_confirmed"]:
                raise HTTPException(status_code=403, detail="Complete target ownership verification and authorization first.")
            try:
                origin = parse_origin(target["base_url"])
                if origin.key != target["origin_key"]:
                    raise SafetyError("Stored authorized origin does not match target.")
                # Re-check ownership and all current A/AAAA answers before enqueueing traffic.
                addresses = await asyncio.to_thread(resolve_public_addresses, origin.host, origin.port)
                challenge_valid = await asyncio.to_thread(verify_dns_challenge, origin.host, target["challenge"])
                if not challenge_valid:
                    raise SafetyError("Ownership TXT challenge has expired or changed.")
            except SafetyError as exc:
                raise HTTPException(status_code=403, detail=str(exc)) from exc
        else:
            addresses = ()
        target["run_authorization_confirmed"] = bool(body.authorization_confirmed)
        now = _now()
        cursor = conn.execute(
            "INSERT INTO tests(project_id,target_id,journey_id,status,config_json,stats_json,created_at) VALUES(?,?,?,?,?,?,?)",
            (target["project_id"], target["id"], journey["id"], "queued", json.dumps(config), "{}", now),
        )
        test_id = cursor.lastrowid
    try:
        await runner.start(test_id, target, steps, config)
    except RunnerError as exc:
        with _connect() as conn:
            conn.execute("UPDATE tests SET status='rejected',stop_reason=? WHERE id=?", (str(exc), test_id))
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"id": test_id, "status": "running", "message": "Test started within server-enforced safety limits.", "resolved_addresses_checked": len(addresses)}


@app.post("/api/tests/{test_id}/stop")
async def stop_test(test_id: int) -> dict:
    stopped = await runner.stop_one(test_id)
    if not stopped:
        with _connect() as conn:
            if not conn.execute("SELECT 1 FROM tests WHERE id=?", (test_id,)).fetchone():
                raise HTTPException(status_code=404, detail="Test not found.")
        return {"stopped": False, "status": "already finished"}
    return {"stopped": True, "status": "stopped"}


@app.post("/api/kill-switch")
async def update_kill_switch(body: KillSwitchUpdate) -> dict:
    if body.active:
        await runner.kill_all()
        _set_setting("kill_switch_active", "1")
    else:
        runner.resume()
        _set_setting("kill_switch_active", "0")
    return {"kill_switch_active": runner.kill_switch_active, "message": "All active workers were cancelled." if body.active else "Runner re-enabled; all other safety gates remain active."}


@app.get("/api/tests/{test_id}")
async def get_test(test_id: int) -> dict:
    live = runner.snapshot(test_id)
    with _connect() as conn:
        row = conn.execute(
            "SELECT t.*, a.name AS target_name, j.name AS journey_name FROM tests t JOIN targets a ON a.id=t.target_id JOIN journeys j ON j.id=t.journey_id WHERE t.id=?",
            (test_id,),
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Test not found.")
    item = dict(row)
    item["config"] = json.loads(item.pop("config_json"))
    saved_stats = json.loads(item.pop("stats_json"))
    item["stats"] = live or saved_stats
    return item


@app.get("/api/tests/{test_id}/events")
async def test_events(test_id: int, request: Request):
    queue = runner.subscribe(test_id)
    if queue is None:
        snapshot = await get_test(test_id)
        async def final_once():
            yield f"data: {json.dumps(snapshot['stats'])}\n\n"
        return StreamingResponse(final_once(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    async def stream():
        try:
            current = runner.snapshot(test_id)
            if current:
                yield f"data: {json.dumps(current)}\n\n"
            while not await request.is_disconnected():
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=10)
                    yield f"data: {json.dumps(event)}\n\n"
                    if event["status"] in {"completed", "stopped", "failed"}:
                        break
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
        finally:
            runner.unsubscribe(test_id, queue)
    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/tests/{test_id}/export.csv")
async def export_test_csv(test_id: int):
    test = await get_test(test_id)
    stats = test["stats"]
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["test_id", "status", "target", "journey", "duration_seconds", "concurrency", "requests_started", "completed", "failed", "throughput_rps", "p50_ms", "p95_ms", "p99_ms", "error_rate_percent", "bytes_received", "alerts"])
    writer.writerow([
        test["id"], test["status"], test["target_name"], test["journey_name"],
        test["config"].get("duration_seconds"), test["config"].get("concurrency"),
        stats.get("requests_started", 0), stats.get("completed", 0), stats.get("failed", 0),
        stats.get("throughput_rps", 0), stats.get("p50_ms", 0), stats.get("p95_ms", 0),
        stats.get("p99_ms", 0), stats.get("error_rate_percent", 0), stats.get("bytes_received", 0),
        json.dumps(stats.get("alerts", []), ensure_ascii=False),
    ])
    buffer.seek(0)
    return StreamingResponse(iter([buffer.getvalue()]), media_type="text/csv", headers={"Content-Disposition": f"attachment; filename=capacity-lab-test-{test_id}.csv"})
