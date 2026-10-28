import asyncio

import pytest

import backend.runner as runner_module
from backend.runner import RunnerError, RunnerManager, _checked_redirect, _one_http_hop, validate_run_config
from backend.safety import SafetyError, Origin, parse_origin


def config(**changes):
    value = {
        "concurrency": 3,
        "ramp_seconds": 0,
        "duration_seconds": 2,
        "max_requests": 40,
        "rate_limit_rps": 25,
        "threshold_p95_ms": 1000,
        "threshold_error_percent": 2,
    }
    value.update(changes)
    return value


def test_run_config_hard_caps_are_rejected_not_clamped():
    for unsafe in [
        config(concurrency=51), config(duration_seconds=301), config(max_requests=5001),
        config(rate_limit_rps=26), config(ramp_seconds=3, duration_seconds=2),
        config(max_requests=True), config(unexpected="code"),
    ]:
        with pytest.raises(RunnerError):
            validate_run_config(unsafe)


def test_runner_refuses_external_target_without_every_authorization_gate():
    async def scenario():
        async def on_update(*args):
            return None
        manager = RunnerManager(on_update)
        target = {
            "kind": "http",
            "base_url": "https://staging.example.com:8443",
            "origin_key": "https://staging.example.com:8443",
            "verified": True,
            "authorization_confirmed": True,
            "challenge": "proof",
            # Missing fresh per-run authorization.
        }
        with pytest.raises(RunnerError, match="explicit authorization"):
            await manager.start(1, target, [{"method": "GET", "path": "/"}], config())
        assert not manager.active
    asyncio.run(scenario())


def test_runner_refuses_changed_exact_origin_before_resolving_dns():
    async def scenario():
        async def on_update(*args):
            return None
        manager = RunnerManager(on_update)
        target = {
            "kind": "http",
            "base_url": "https://staging.example.com:8443",
            "origin_key": "https://staging.example.com:443",
            "verified": True,
            "authorization_confirmed": True,
            "run_authorization_confirmed": True,
            "challenge": "proof",
        }
        with pytest.raises(RunnerError, match="scope does not match"):
            await manager.start(1, target, [{"method": "GET", "path": "/"}], config())
        assert not manager.active
    asyncio.run(scenario())


def test_redirects_are_restricted_to_the_exact_authorized_origin():
    origin = parse_origin("https://staging.example.com:8443")
    assert _checked_redirect(origin, origin.key + "/a", "/b") == origin.key + "/b"
    assert _checked_redirect(origin, origin.key + "/a", "https://staging.example.com:8443/b") == origin.key + "/b"
    for location in [
        "http://staging.example.com:8443/", "https://staging.example.com/",
        "https://other.example.com:8443/", "http://127.0.0.1/", "//169.254.169.254/latest/meta-data",
    ]:
        with pytest.raises(SafetyError):
            _checked_redirect(origin, origin.key + "/start", location)


def test_http_socket_is_pinned_to_validated_address_and_keeps_host_header(monkeypatch):
    async def scenario():
        observed = {}

        async def handler(reader, writer):
            observed["peer"] = writer.get_extra_info("peername")[0]
            observed["request"] = (await reader.readline()).decode().strip()
            while await reader.readline() not in {b"\r\n", b"\n", b""}:
                pass
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        server = await asyncio.start_server(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        monkeypatch.setattr(runner_module, "resolve_public_addresses", lambda host, port: ("127.0.0.1",))
        origin = Origin("http", "staging.example.com", port)
        try:
            result = await _one_http_hop(origin, origin.key + "/check?x=1", "GET", {}, None)
            assert result == (200, {"content-length": "2", "connection": "close"}, 2)
            assert observed["peer"] == "127.0.0.1"
            assert observed["request"] == "GET /check?x=1 HTTP/1.1"
        finally:
            server.close()
            await server.wait_closed()
    asyncio.run(scenario())


def test_global_kill_cancels_simulator_workers_and_prevents_new_requests():
    async def scenario():
        updates = []
        async def on_update(test_id, status, snapshot):
            updates.append((status, snapshot.get("requests_started", 0)))
        manager = RunnerManager(on_update)
        await manager.start(
            7,
            {"kind": "simulator", "base_url": "simulator://local"},
            [{"method": "GET", "path": "/"}],
            config(duration_seconds=30, concurrency=10, max_requests=4000),
        )
        await asyncio.sleep(0.03)
        await manager.kill_all()
        ctx = manager.active[7]
        stopped_count = ctx.stats["requests_started"]
        assert manager.kill_switch_active is True
        assert ctx.status == "stopped"
        assert ctx.stats["active_users"] == 0
        assert ctx.stats["in_flight"] == 0
        assert all(task.done() for task in ctx.worker_tasks)
        await asyncio.sleep(0.08)
        assert ctx.stats["requests_started"] == stopped_count
        assert updates[-1][0] == "stopped"
    asyncio.run(scenario())


def test_kill_switch_blocks_new_runs():
    async def scenario():
        async def on_update(*args):
            return None
        manager = RunnerManager(on_update)
        manager.kill_switch_active = True
        with pytest.raises(RunnerError, match="kill switch"):
            await manager.start(9, {"kind": "simulator"}, [{"method": "GET", "path": "/"}], config())
    asyncio.run(scenario())


def test_duration_budget_includes_rate_limiter_waiters():
    async def scenario():
        async def on_update(*args):
            return None
        manager = RunnerManager(on_update)
        began = asyncio.get_running_loop().time()
        await manager.start(
            11,
            {"kind": "simulator", "base_url": "simulator://local"},
            [{"method": "GET", "path": "/"}],
            config(duration_seconds=1, concurrency=50, ramp_seconds=0, rate_limit_rps=1, max_requests=5000),
        )
        ctx = manager.active[11]
        await asyncio.wait_for(ctx.root_task, timeout=0.75)
        elapsed = asyncio.get_running_loop().time() - began
        assert elapsed < 0.75
        assert ctx.status == "completed"
        assert ctx.stop_reason == "duration limit reached"
        assert ctx.stats["requests_started"] <= 1
    asyncio.run(scenario())


def test_cancelled_external_request_releases_inflight_count(monkeypatch):
    async def scenario():
        request_seen = asyncio.Event()
        release_handler = asyncio.Event()

        async def handler(reader, writer):
            try:
                await reader.readline()
                while await reader.readline() not in {b"\r\n", b"\n", b""}:
                    pass
                request_seen.set()
                await release_handler.wait()
            finally:
                writer.close()
                try:
                    await writer.wait_closed()
                except OSError:
                    pass

        server = await asyncio.start_server(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        monkeypatch.setattr(runner_module, "resolve_public_addresses", lambda host, port: ("127.0.0.1",))
        monkeypatch.setattr(runner_module, "verify_dns_challenge", lambda host, token: True)
        manager = RunnerManager(lambda *args: asyncio.sleep(0))
        target = {
            "kind": "http", "base_url": f"http://staging.example.com:{port}",
            "origin_key": f"http://staging.example.com:{port}", "verified": True,
            "authorization_confirmed": True, "run_authorization_confirmed": True,
            "challenge": "test-proof",
        }
        try:
            await manager.start(12, target, [{"method": "GET", "path": "/slow"}], config(concurrency=1, duration_seconds=30))
            ctx = manager.active[12]
            await asyncio.wait_for(request_seen.wait(), timeout=1)
            assert ctx.stats["in_flight"] == 1
            await manager.kill_all()
            assert ctx.status == "stopped"
            assert ctx.stats["in_flight"] == 0
            assert ctx.stats["bytes_reserved"] == 0
            assert ctx.stats["active_users"] == 0
            assert all(task.done() for task in ctx.worker_tasks)
        finally:
            release_handler.set()
            server.close()
            await server.wait_closed()
    asyncio.run(scenario())


def test_total_response_byte_budget_is_strict_across_concurrency(monkeypatch):
    async def scenario():
        async def on_update(*args):
            return None
        monkeypatch.setattr(runner_module, "MAX_RESPONSE_BYTES_PER_TEST", 100)
        manager = RunnerManager(on_update)
        await manager.start(
            13,
            {"kind": "simulator", "base_url": "simulator://local"},
            [{"method": "GET", "path": "/"}],
            config(concurrency=20, duration_seconds=3, max_requests=5000),
        )
        ctx = manager.active[13]
        await asyncio.wait_for(ctx.root_task, timeout=1)
        assert ctx.stats["bytes_received"] <= 100
        assert ctx.stats["bytes_received"] == 100
        assert ctx.stats["bytes_reserved"] == 0
        assert ctx.stop_reason == "response-byte safety limit reached"
    asyncio.run(scenario())
