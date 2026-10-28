"""Bounded, cancellable load-test runner. External requests are pinned to a freshly
validated public DNS answer and every redirect is checked against the exact origin.
"""
from __future__ import annotations

import asyncio
import json
import random
import ssl
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable
from urllib.parse import urljoin, urlsplit

from .safety import (
    Origin,
    SafetyError,
    origin_from_url,
    parse_origin,
    resolve_public_addresses,
    validate_steps,
    verify_dns_challenge,
)

MAX_CONCURRENCY = 50
MAX_DURATION_SECONDS = 300
MAX_REQUESTS = 5_000
MAX_RPS = 25
MAX_REDIRECTS = 3
MAX_RESPONSE_BYTES_PER_REQUEST = 65_536
MAX_RESPONSE_BYTES_PER_TEST = 20 * 1024 * 1024
CONNECT_TIMEOUT_SECONDS = 3.0
REQUEST_TIMEOUT_SECONDS = 5.0


class RunnerError(ValueError):
    pass


@dataclass
class RunContext:
    test_id: int
    config: dict[str, Any]
    status: str = "queued"
    started_monotonic: float = 0.0
    started_at: str | None = None
    finished_at: str | None = None
    stop_reason: str | None = None
    stop_event: asyncio.Event = field(default_factory=asyncio.Event)
    start_gate: asyncio.Event = field(default_factory=asyncio.Event)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    rate_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    next_request_at: float = 0.0
    root_task: asyncio.Task | None = None
    worker_tasks: set[asyncio.Task] = field(default_factory=set)
    subscribers: set[asyncio.Queue] = field(default_factory=set)
    stats: dict[str, Any] = field(default_factory=lambda: {
        "requests_started": 0,
        "completed": 0,
        "failed": 0,
        "in_flight": 0,
        "active_users": 0,
        "status_counts": {},
        "latencies_ms": [],
        "bytes_received": 0,
        "bytes_reserved": 0,
        "throughput_rps": 0.0,
        "error_rate_percent": 0.0,
        "p50_ms": 0.0,
        "p95_ms": 0.0,
        "p99_ms": 0.0,
        "alerts": [],
    })


def _percentile(samples: list[float], percentile: float) -> float:
    if not samples:
        return 0.0
    values = sorted(samples)
    index = max(0, min(len(values) - 1, int((len(values) - 1) * percentile + 0.999999)))
    return round(values[index], 2)


def validate_run_config(raw: dict[str, Any]) -> dict[str, Any]:
    """Reject, do not silently clamp, any client request exceeding server policy."""
    fields = {"concurrency", "ramp_seconds", "duration_seconds", "max_requests", "rate_limit_rps",
              "threshold_p95_ms", "threshold_error_percent"}
    if set(raw) - fields:
        raise RunnerError("Unsupported run configuration fields.")
    integer_ranges = {
        "concurrency": (1, MAX_CONCURRENCY),
        "ramp_seconds": (0, MAX_DURATION_SECONDS),
        "duration_seconds": (1, MAX_DURATION_SECONDS),
        "max_requests": (1, MAX_REQUESTS),
        "rate_limit_rps": (1, MAX_RPS),
        "threshold_p95_ms": (50, 30_000),
    }
    result: dict[str, Any] = {}
    for key, (minimum, maximum) in integer_ranges.items():
        value = raw.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
            raise RunnerError(f"{key} must be an integer from {minimum} to {maximum} (server-enforced).")
        result[key] = value
    if result["ramp_seconds"] > result["duration_seconds"]:
        raise RunnerError("Ramp time cannot exceed the test duration.")
    error_threshold = raw.get("threshold_error_percent")
    if isinstance(error_threshold, bool) or not isinstance(error_threshold, (int, float)) or not 0 <= error_threshold <= 100:
        raise RunnerError("threshold_error_percent must be between 0 and 100.")
    result["threshold_error_percent"] = float(error_threshold)
    return result


async def _read_line(reader: asyncio.StreamReader, limit: int = 8_192) -> bytes:
    line = await reader.readline()
    if len(line) > limit:
        raise OSError("response_header_too_large")
    if not line:
        raise OSError("incomplete_response")
    return line


async def _read_response(reader: asyncio.StreamReader, method: str, max_body_bytes: int = MAX_RESPONSE_BYTES_PER_REQUEST) -> tuple[int, dict[str, str], int]:
    status_line = (await _read_line(reader)).decode("iso-8859-1", "replace").strip()
    parts = status_line.split(" ", 2)
    if len(parts) < 2 or not parts[0].startswith("HTTP/"):
        raise OSError("invalid_response")
    try:
        status = int(parts[1])
    except ValueError as exc:
        raise OSError("invalid_status") from exc
    headers: dict[str, str] = {}
    header_bytes = 0
    while True:
        line = await _read_line(reader)
        header_bytes += len(line)
        if header_bytes > 16_384:
            raise OSError("response_header_too_large")
        if line in {b"\r\n", b"\n"}:
            break
        if b":" not in line:
            raise OSError("invalid_header")
        key, value = line.decode("iso-8859-1", "replace").split(":", 1)
        headers[key.strip().lower()] = value.strip()
    body_bytes = 0
    body_limit = max(0, min(max_body_bytes, MAX_RESPONSE_BYTES_PER_REQUEST))
    if method != "HEAD" and status not in {204, 304} and not 100 <= status < 200:
        length = headers.get("content-length")
        if length is not None:
            try:
                size = max(0, int(length))
            except ValueError:
                raise OSError("invalid_content_length")
            take = min(size, body_limit)
            if take:
                body_bytes = len(await reader.readexactly(take))
        elif "chunked" in headers.get("transfer-encoding", "").lower():
            while body_bytes < body_limit:
                line = (await _read_line(reader, 128)).split(b";", 1)[0].strip()
                try:
                    chunk_len = int(line, 16)
                except ValueError as exc:
                    raise OSError("invalid_chunk") from exc
                if chunk_len == 0:
                    await _read_line(reader, 8_192)
                    break
                take = min(chunk_len, body_limit - body_bytes)
                if take:
                    body_bytes += len(await reader.readexactly(take))
                if take < chunk_len:
                    break
                ending = await reader.readexactly(2)
                if ending != b"\r\n":
                    raise OSError("invalid_chunk_ending")
        else:
            # Do not wait on unbounded/streaming bodies; sample at most one bounded read.
            try:
                body = await asyncio.wait_for(reader.read(body_limit), timeout=0.25)
                body_bytes = len(body)
            except asyncio.TimeoutError:
                body_bytes = 0
    return status, headers, body_bytes


async def _one_http_hop(
    origin: Origin,
    url: str,
    method: str,
    headers: dict[str, str],
    body: str | None,
    timeout_seconds: float = REQUEST_TIMEOUT_SECONDS,
    max_body_bytes: int = MAX_RESPONSE_BYTES_PER_REQUEST,
) -> tuple[int, dict[str, str], int]:
    """Make one request to a validated IP; preserve Host/SNI and never auto-follow."""
    destination = origin_from_url(url)
    if destination != origin:
        raise SafetyError("A redirect attempted to leave the authorized origin.")
    if timeout_seconds <= 0:
        raise asyncio.TimeoutError("test_duration_exceeded")
    timeout_seconds = min(REQUEST_TIMEOUT_SECONDS, timeout_seconds)
    request_deadline = time.monotonic() + timeout_seconds
    parts = urlsplit(url)
    request_target = parts.path or "/"
    if parts.query:
        request_target += "?" + parts.query
    req_headers = {
        "Host": origin.authority,
        "User-Agent": "CapacityLab/0.1",
        "Accept-Encoding": "identity",
        "Connection": "close",
    }
    req_headers.update(headers)
    payload = body.encode("utf-8") if body is not None else b""
    if payload:
        req_headers["Content-Length"] = str(len(payload))
        if not any(key.lower() == "content-type" for key in req_headers):
            req_headers["Content-Type"] = "application/json"
    lines = [f"{method} {request_target} HTTP/1.1"]
    lines.extend(f"{key}: {value}" for key, value in req_headers.items())
    wire = ("\r\n".join(lines) + "\r\n\r\n").encode("iso-8859-1") + payload
    writer: asyncio.StreamWriter | None = None
    ssl_context = ssl.create_default_context() if origin.scheme == "https" else None
    try:
        dns_timeout = min(CONNECT_TIMEOUT_SECONDS, max(0.001, request_deadline - time.monotonic()))
        addresses = await asyncio.wait_for(
            asyncio.to_thread(resolve_public_addresses, origin.host, origin.port), timeout=dns_timeout
        )
        # Recheck the complete answer set immediately before connect and pin to one address.
        pinned_ip = addresses[0]
        timeout_seconds = request_deadline - time.monotonic()
        if timeout_seconds <= 0:
            raise asyncio.TimeoutError("test_duration_exceeded")
        async with asyncio.timeout(timeout_seconds):
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(
                    host=pinned_ip,
                    port=origin.port,
                    ssl=ssl_context,
                    server_hostname=origin.host if ssl_context else None,
                    happy_eyeballs_delay=None,
                ),
                timeout=min(CONNECT_TIMEOUT_SECONDS, timeout_seconds),
            )
            writer.write(wire)
            await writer.drain()
            return await _read_response(reader, method, max_body_bytes)
    finally:
        if writer is not None:
            writer.close()
            try:
                await writer.wait_closed()
            except (OSError, asyncio.CancelledError):
                pass


def _checked_redirect(origin: Origin, current_url: str, location: str) -> str:
    """Resolve one redirect while preserving the precise authorized origin."""
    candidate = urljoin(current_url, location)
    if any(ord(character) < 32 or ord(character) == 127 for character in candidate) or "\\" in candidate:
        raise SafetyError("Redirect URL contains forbidden characters.")
    if origin_from_url(candidate) != origin:
        raise SafetyError("A redirect attempted to leave the authorized scheme/host/port.")
    if urlsplit(candidate).fragment:
        raise SafetyError("Redirect fragments are not supported.")
    return candidate


class RunnerManager:
    def __init__(self, on_update: Callable[[int, str, dict], Awaitable[None]]):
        self.on_update = on_update
        self.kill_switch_active = False
        self.active: dict[int, RunContext] = {}
        self._manager_lock = asyncio.Lock()

    def has_active_run(self) -> bool:
        return any(ctx.status in {"queued", "running", "stopping"} for ctx in self.active.values())

    async def start(
        self,
        test_id: int,
        target: dict[str, Any],
        steps: list[dict],
        raw_config: dict[str, Any],
    ) -> None:
        config = validate_run_config(raw_config)
        safe_steps = validate_steps(steps)
        if target.get("kind") == "http":
            if not target.get("verified") or not target.get("authorization_confirmed") or not target.get("run_authorization_confirmed"):
                raise RunnerError("Target ownership proof and explicit authorization for this exact origin are required.")
            try:
                origin = parse_origin(target["base_url"])
                if origin.key != target.get("origin_key"):
                    raise SafetyError("Stored target scope does not match the verified origin.")
                await asyncio.to_thread(resolve_public_addresses, origin.host, origin.port)
                if not await asyncio.to_thread(verify_dns_challenge, origin.host, target.get("challenge", "")):
                    raise SafetyError("Ownership TXT proof is missing or no longer valid.")
            except (KeyError, SafetyError) as exc:
                raise RunnerError(str(exc)) from exc
        elif target.get("kind") != "simulator":
            raise RunnerError("Unknown target type; traffic is blocked.")
        async with self._manager_lock:
            if self.kill_switch_active:
                raise RunnerError("Global kill switch is active; resume the runner before starting a test.")
            if self.has_active_run():
                raise RunnerError("Only one test may run at a time in this local installation.")
            ctx = RunContext(test_id=test_id, config=config)
            ctx.status = "queued"
            self.active[test_id] = ctx
            ctx.root_task = asyncio.create_task(self._run(ctx, target, safe_steps))
            await self._publish(ctx)
            ctx.start_gate.set()

    async def stop_one(self, test_id: int, reason: str = "stopped by operator") -> bool:
        ctx = self.active.get(test_id)
        if not ctx or ctx.status not in {"queued", "running", "stopping"}:
            return False
        ctx.stop_reason = reason
        ctx.status = "stopping"
        ctx.stop_event.set()
        ctx.start_gate.set()
        await self._publish(ctx)
        # Cancelling the workers interrupts async connect/read and closes their sockets.
        workers = list(ctx.worker_tasks)
        for task in workers:
            task.cancel()
        if workers:
            await asyncio.gather(*workers, return_exceptions=True)
        if ctx.root_task and ctx.root_task is not asyncio.current_task():
            try:
                await asyncio.wait_for(asyncio.shield(ctx.root_task), timeout=1.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                ctx.root_task.cancel()
        if ctx.status == "stopping":
            ctx.status = "stopped"
            ctx.finished_at = _iso_now()
            await self._publish(ctx)
        return True

    async def kill_all(self) -> None:
        self.kill_switch_active = True
        active_ids = [test_id for test_id, ctx in self.active.items() if ctx.status in {"queued", "running", "stopping"}]
        await asyncio.gather(*(self.stop_one(test_id, "global kill switch") for test_id in active_ids), return_exceptions=True)

    def resume(self) -> None:
        self.kill_switch_active = False

    def subscribe(self, test_id: int) -> asyncio.Queue | None:
        ctx = self.active.get(test_id)
        if ctx is None:
            return None
        queue: asyncio.Queue = asyncio.Queue(maxsize=1)
        ctx.subscribers.add(queue)
        return queue

    def unsubscribe(self, test_id: int, queue: asyncio.Queue) -> None:
        ctx = self.active.get(test_id)
        if ctx:
            ctx.subscribers.discard(queue)

    def snapshot(self, test_id: int) -> dict | None:
        ctx = self.active.get(test_id)
        if not ctx:
            return None
        return self._snapshot(ctx)

    async def _publish(self, ctx: RunContext) -> None:
        payload = self._snapshot(ctx)
        await self.on_update(ctx.test_id, ctx.status, payload)
        for queue in tuple(ctx.subscribers):
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            try:
                queue.put_nowait(payload)
            except asyncio.QueueFull:
                pass

    def _snapshot(self, ctx: RunContext) -> dict:
        stats = ctx.stats
        elapsed = max(0.0, time.monotonic() - ctx.started_monotonic) if ctx.started_monotonic else 0.0
        samples = stats["latencies_ms"]
        completed = stats["completed"]
        return {
            "test_id": ctx.test_id,
            "status": ctx.status,
            "started_at": ctx.started_at,
            "finished_at": ctx.finished_at,
            "stop_reason": ctx.stop_reason,
            "elapsed_seconds": round(elapsed, 2),
            "requests_started": stats["requests_started"],
            "completed": completed,
            "failed": stats["failed"],
            "in_flight": stats["in_flight"],
            "active_users": stats["active_users"],
            "status_counts": dict(stats["status_counts"]),
            "bytes_received": stats["bytes_received"],
            "throughput_rps": round(completed / elapsed, 2) if elapsed > 0 else 0.0,
            "error_rate_percent": round(100 * stats["failed"] / completed, 2) if completed else 0.0,
            "p50_ms": _percentile(samples, 0.50),
            "p95_ms": _percentile(samples, 0.95),
            "p99_ms": _percentile(samples, 0.99),
            "alerts": list(stats["alerts"]),
        }

    async def _run(self, ctx: RunContext, target: dict[str, Any], steps: list[dict]) -> None:
        await ctx.start_gate.wait()
        if ctx.stop_event.is_set():
            ctx.status = "stopped"
            ctx.finished_at = _iso_now()
            await self._publish(ctx)
            return
        ctx.status = "running"
        ctx.started_monotonic = time.monotonic()
        from datetime import datetime, timezone
        ctx.started_at = datetime.now(timezone.utc).isoformat()
        deadline = ctx.started_monotonic + ctx.config["duration_seconds"]
        concurrency = ctx.config["concurrency"]
        ramp = ctx.config["ramp_seconds"]
        ctx.worker_tasks = {
            asyncio.create_task(self._worker(ctx, target, steps, index, deadline, ramp))
            for index in range(concurrency)
        }
        await self._publish(ctx)
        try:
            await asyncio.gather(*ctx.worker_tasks, return_exceptions=True)
        finally:
            if ctx.status == "running":
                ctx.status = "stopped" if ctx.stop_reason in {"stopped by operator", "global kill switch"} else "completed"
            if ctx.status == "stopping":
                ctx.status = "stopped"
            ctx.finished_at = _iso_now()
            await self._publish(ctx)

    async def _reserve_request(self, ctx: RunContext, deadline: float, response_budget: int = MAX_RESPONSE_BYTES_PER_REQUEST) -> int | None:
        if ctx.stop_event.is_set():
            return None
        if time.monotonic() >= deadline:
            ctx.stop_reason = "duration limit reached"
            ctx.stop_event.set()
            return None
        interval = 1.0 / ctx.config["rate_limit_rps"]
        async with ctx.rate_lock:
            now = time.monotonic()
            scheduled = max(now, ctx.next_request_at)
            ctx.next_request_at = scheduled + interval
        delay = scheduled - time.monotonic()
        remaining = deadline - time.monotonic()
        if remaining <= 0 or scheduled >= deadline:
            ctx.stop_reason = "duration limit reached"
            ctx.stop_event.set()
            return None
        if delay > 0:
            try:
                await asyncio.wait_for(ctx.stop_event.wait(), timeout=min(delay, remaining))
                return None
            except asyncio.TimeoutError:
                pass
        if ctx.stop_event.is_set():
            return None
        if time.monotonic() >= deadline:
            ctx.stop_reason = "duration limit reached"
            ctx.stop_event.set()
            return None
        async with ctx.lock:
            if ctx.stats["requests_started"] >= ctx.config["max_requests"]:
                ctx.stop_reason = "total request limit reached"
                ctx.stop_event.set()
                return None
            available_bytes = MAX_RESPONSE_BYTES_PER_TEST - ctx.stats["bytes_received"] - ctx.stats["bytes_reserved"]
            if available_bytes <= 0:
                ctx.stop_reason = "response-byte safety limit reached"
                ctx.stop_event.set()
                return None
            allocation = min(response_budget, available_bytes)
            ctx.stats["requests_started"] += 1
            ctx.stats["in_flight"] += 1
            ctx.stats["bytes_reserved"] += allocation
        await self._publish(ctx)
        return allocation

    async def _record(
        self,
        ctx: RunContext,
        status: int | None,
        elapsed_ms: float,
        byte_count: int,
        error: bool,
        reserved_bytes: int,
    ) -> None:
        async with ctx.lock:
            ctx.stats["in_flight"] = max(0, ctx.stats["in_flight"] - 1)
            ctx.stats["bytes_reserved"] = max(0, ctx.stats["bytes_reserved"] - reserved_bytes)
            ctx.stats["completed"] += 1
            ctx.stats["latencies_ms"].append(round(elapsed_ms, 2))
            if error:
                ctx.stats["failed"] += 1
            if status is not None:
                key = str(status)
                ctx.stats["status_counts"][key] = ctx.stats["status_counts"].get(key, 0) + 1
            remaining = max(0, MAX_RESPONSE_BYTES_PER_TEST - ctx.stats["bytes_received"])
            ctx.stats["bytes_received"] += min(byte_count, reserved_bytes, remaining)
            snapshot = self._snapshot(ctx)
            if ctx.stats["bytes_received"] >= MAX_RESPONSE_BYTES_PER_TEST:
                ctx.stop_reason = "response-byte safety limit reached"
                ctx.stop_event.set()
            if ctx.stats["completed"] >= 10 and not any(a["type"] == "error-rate" for a in ctx.stats["alerts"]):
                if snapshot["error_rate_percent"] > ctx.config["threshold_error_percent"]:
                    ctx.stats["alerts"].append({
                        "type": "error-rate",
                        "message": f"Error rate is {snapshot['error_rate_percent']}% (threshold {ctx.config['threshold_error_percent']}%).",
                    })
            if ctx.stats["completed"] >= 5 and not any(a["type"] == "latency" for a in ctx.stats["alerts"]):
                if snapshot["p95_ms"] > ctx.config["threshold_p95_ms"]:
                    ctx.stats["alerts"].append({
                        "type": "latency",
                        "message": f"p95 latency is {snapshot['p95_ms']} ms (threshold {ctx.config['threshold_p95_ms']} ms).",
                    })
        await self._publish(ctx)

    async def _worker(
        self,
        ctx: RunContext,
        target: dict[str, Any],
        steps: list[dict],
        index: int,
        deadline: float,
        ramp: int,
    ) -> None:
        ramp_delay = ramp * index / max(1, ctx.config["concurrency"] - 1)
        try:
            if ramp_delay:
                await asyncio.wait_for(ctx.stop_event.wait(), timeout=ramp_delay)
                return
        except asyncio.TimeoutError:
            pass
        if ctx.stop_event.is_set() or time.monotonic() >= deadline:
            return
        async with ctx.lock:
            ctx.stats["active_users"] += 1
        await self._publish(ctx)
        try:
            while not ctx.stop_event.is_set() and time.monotonic() < deadline:
                for step in steps:
                    if ctx.stop_event.is_set() or time.monotonic() >= deadline:
                        break
                    if target["kind"] == "simulator":
                        await self._run_simulated_request(ctx, step, deadline)
                        continue
                    origin = parse_origin(target["base_url"])
                    current_url = origin.key + step["path"]
                    for redirect_number in range(MAX_REDIRECTS + 1):
                        reserved_bytes = await self._reserve_request(ctx, deadline)
                        if reserved_bytes is None:
                            break
                        began = time.monotonic()
                        status: int | None = None
                        byte_count = 0
                        failed = False
                        try:
                            remaining = deadline - time.monotonic()
                            status, response_headers, byte_count = await _one_http_hop(
                                origin, current_url, step["method"], step["headers"], step["body"], remaining, reserved_bytes
                            )
                            failed = status >= 400
                            location = response_headers.get("location")
                            if status in {301, 302, 303, 307, 308} and location:
                                if redirect_number >= MAX_REDIRECTS:
                                    failed = True
                                else:
                                    try:
                                        current_url = _checked_redirect(origin, current_url, location)
                                    except SafetyError:
                                        failed = True
                        except asyncio.CancelledError:
                            await self._cancel_request(ctx, reserved_bytes)
                            raise
                        except (SafetyError, OSError, asyncio.TimeoutError, ssl.SSLError, ValueError) as exc:
                            failed = True
                            # Only a coarse error class is retained; no target response/body is logged.
                            _ = type(exc).__name__
                        elapsed_ms = (time.monotonic() - began) * 1000
                        await self._record(ctx, status, elapsed_ms, byte_count, failed, reserved_bytes)
                        if failed or status not in {301, 302, 303, 307, 308}:
                            break
                        if not response_headers.get("location"):
                            break
                        if ctx.stop_event.is_set():
                            break
                if ctx.stop_event.is_set():
                    break
        except asyncio.CancelledError:
            raise
        finally:
            async with ctx.lock:
                ctx.stats["active_users"] = max(0, ctx.stats["active_users"] - 1)
            await self._publish(ctx)

    async def _run_simulated_request(self, ctx: RunContext, step: dict, deadline: float) -> None:
        reserved_bytes = await self._reserve_request(ctx, deadline, response_budget=2048)
        if reserved_bytes is None:
            return
        began = time.monotonic()
        try:
            await asyncio.sleep(random.uniform(0.008, 0.04))
        except asyncio.CancelledError:
            await self._cancel_request(ctx, reserved_bytes)
            raise
        status = 503 if "demo-error" in step["path"] and random.random() < 0.12 else 200
        await self._record(ctx, status, (time.monotonic() - began) * 1000, random.randint(300, 1800), status >= 400, reserved_bytes)

    async def _cancel_request(self, ctx: RunContext, reserved_bytes: int) -> None:
        async with ctx.lock:
            ctx.stats["in_flight"] = max(0, ctx.stats["in_flight"] - 1)
            ctx.stats["bytes_reserved"] = max(0, ctx.stats["bytes_reserved"] - reserved_bytes)
        await self._publish(ctx)


def _iso_now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()
