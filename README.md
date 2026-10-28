# Capacity Lab

A standalone, mobile-first load-testing workspace for replaying declarative user journeys against authorized staging targets. The app keeps this work separate from the customer-care workspace and any other Reel-based project.

## Architecture

- **Client:** React + Vite single-page app. The layout adapts to phone and desktop widths and includes projects, targets, reusable journeys, test configuration, live progress, p50/p95/p99 latency, throughput, error-rate alerts, history, and CSV export.
- **API and runner:** FastAPI with a SQLite store. An asyncio runner schedules virtual users, ramps them in, rate-limits every request (including redirect hops), streams current stats over Server-Sent Events, and cancels in-flight async sockets on stop.
- **Target authorization:** External targets are staging-only. Registration requires an explicit ownership/authorization attestation, then a DNS TXT challenge tied to a digest of the exact normalized scheme/host/port. Verification uses DNS only and sends no HTTP probe to the target. Each external test requires fresh explicit authorization and rechecks the DNS proof and public address set before starting.
- **Egress safety:** Each actual HTTP request resolves all returned A/AAAA addresses, rejects a mixed or special-use answer set, pins the socket to one validated public address, and preserves the authorized hostname for HTTP Host and TLS SNI/certificate verification. Redirects are limited to the same exact origin and re-resolved/rechecked before the next request. IP literals, userinfo, paths on target registration, arbitrary code, credentials, cookies, and unapproved headers are rejected.
- **Local simulator:** A seeded project and deterministic local-only simulator are available for exercising the UI, run lifecycle, telemetry, alerts, and exports without contacting any network target.

## Server-enforced limits

The initial hard caps are 50 virtual users, 300 seconds per test, 5,000 total requests including redirects, 25 requests/second, 30 journey steps, 2 KB per POST body, 64 KB response capture per request, and 20 MB captured per test. Requests have bounded connect/read timeouts. Only one test runs at a time in this local installation. The global kill switch cancels active worker tasks and closes their sockets, and blocks new starts until manually resumed.

## Run locally

Use two terminals from this directory. Keep both services bound to loopback; do not expose them to a network.

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r backend/requirements.txt
uvicorn backend.app:app --host 127.0.0.1 --port 8000
```

```bash
cd frontend
npm install
npm run dev
```

Open the local address printed by Vite (normally `http://127.0.0.1:5173`). The frontend proxies `/api` to the loopback-only API. SQLite data is stored in `data/capacity_lab.sqlite3`; set `CAPACITY_LAB_DB` to use a different path.

For an external staging target, create a **Staging** project, add the exact origin, publish the displayed TXT record at `_capacity-lab-verify.<hostname>`, and select **Verify ownership**. Only after it verifies can a test be started, and the run form will require a fresh authorization confirmation. Use the simulator first.

## Verify the build

```bash
python3 -m pip install -r backend/requirements.txt
python3 -m pytest -q
cd frontend && npm run build
```

The tests include a negative SSRF matrix for IPv4/IPv6 private and special-use ranges, including NAT64/6to4 translation ranges and embedded metadata/private/loopback addresses; mixed A/AAAA sets, runtime DNS rebinding, exact scheme/host/port redirect scope, pinned socket peer behavior, target authorization gates, server caps, duration bounds, kill-switch cancellation, simulator telemetry persistence, and CSV export.

## Known gaps / safe-use notes

This is a functional local single-operator build, not a managed multi-tenant service. It has no login, tenant isolation, remote deployment hardening, distributed workers, egress firewall, or audit/retention controls; keep it on loopback and do not deploy it as-is to a public host. The runner does not yet support authenticated journeys, cookies, token refresh, response-to-request variable extraction/correlation, arbitrary headers, or scripting. Static POST bodies are saved in local SQLite as text: do not put passwords, tokens, or other secrets in them. History is retained locally until the operator deletes the database. Threshold alerts appear in the live UI and history; there is no external notification channel.

The provided Instagram Reel could not be fetched by the video-analysis service during implementation. The user-provided description and added safety requirements were used as the product specification.
