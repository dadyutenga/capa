"""Fail-closed target safety primitives for Capacity Lab.

Targets are authorized for one normalized scheme/host/port origin only. DNS answers
are validated per request and the runner pins the socket to an approved answer.
"""
from __future__ import annotations

import ipaddress
import hashlib
import secrets
import socket
from dataclasses import dataclass
from urllib.parse import urlsplit


class SafetyError(ValueError):
    """An input or destination was rejected by a safety gate."""


@dataclass(frozen=True)
class Origin:
    scheme: str
    host: str
    port: int

    @property
    def key(self) -> str:
        return f"{self.scheme}://{self.host}:{self.port}"

    @property
    def authority(self) -> str:
        default = 443 if self.scheme == "https" else 80
        return self.host if self.port == default else f"{self.host}:{self.port}"


def parse_origin(value: str) -> Origin:
    """Accept a bare HTTP(S) origin; never accept a URL with userinfo/path/query."""
    if not isinstance(value, str) or len(value) > 512 or any(ord(c) < 32 for c in value):
        raise SafetyError("Enter a valid staging origin.")
    try:
        parts = urlsplit(value.strip())
        if parts.scheme.lower() not in {"http", "https"}:
            raise SafetyError("Only HTTP or HTTPS staging origins are allowed.")
        if parts.username is not None or parts.password is not None:
            raise SafetyError("Credentials are not allowed in a target URL.")
        if parts.query or parts.fragment or parts.path not in {"", "/"}:
            raise SafetyError("Enter only the origin (scheme, hostname, and optional port).")
        raw_host = parts.hostname
        if not raw_host or "%" in raw_host or "\\" in raw_host:
            raise SafetyError("Enter a valid DNS hostname, not an IP literal.")
        try:
            ipaddress.ip_address(raw_host)
        except ValueError:
            pass
        else:
            raise SafetyError("IP-literal targets are not supported; prove control of a DNS hostname.")
        host = raw_host.encode("idna").decode("ascii").lower().rstrip(".")
        if "." not in host or len(host) > 253:
            raise SafetyError("Use a fully qualified DNS hostname.")
        labels = host.split(".")
        if any(not label or len(label) > 63 or label.startswith("-") or label.endswith("-") for label in labels):
            raise SafetyError("The target hostname is malformed.")
        if any(not all(ch.isalnum() or ch == "-" for ch in label) for label in labels):
            raise SafetyError("The target hostname is malformed.")
        try:
            port = parts.port
        except ValueError as exc:
            raise SafetyError("The target port is invalid.") from exc
        if port is None:
            port = 443 if parts.scheme.lower() == "https" else 80
        if not 1 <= port <= 65535:
            raise SafetyError("The target port must be between 1 and 65535.")
        return Origin(parts.scheme.lower(), host, port)
    except SafetyError:
        raise
    except (UnicodeError, ValueError) as exc:
        raise SafetyError("The target origin is malformed.") from exc


def origin_from_url(value: str) -> Origin:
    """Parse a URL solely to compare its origin against an authorized origin."""
    try:
        parts = urlsplit(value)
        if not parts.scheme or not parts.hostname:
            raise SafetyError("Redirect URL is malformed.")
        origin_url = f"{parts.scheme}://{parts.netloc}"
        return parse_origin(origin_url)
    except SafetyError:
        raise
    except ValueError as exc:
        raise SafetyError("Redirect URL is malformed.") from exc


# Deny special-purpose ranges explicitly as well as anything Python does not
# classify as globally routable. This includes metadata services and mapped v6.
_BLOCKED_NETWORKS = tuple(
    ipaddress.ip_network(item)
    for item in (
        "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8",
        "169.254.0.0/16", "172.16.0.0/12", "192.0.0.0/24", "192.0.2.0/24",
        "192.168.0.0/16", "198.18.0.0/15", "198.51.100.0/24", "203.0.113.0/24",
        "224.0.0.0/4", "240.0.0.0/4", "168.63.129.16/32", "100.100.100.200/32",
        "169.254.169.254/32", "169.254.170.2/32",
        "::/128", "::1/128", "::ffff:0:0/96", "64:ff9b::/96", "64:ff9b:1::/48", "100::/64",
        "2001::/23", "2001:db8::/32", "2002::/16", "fc00::/7", "fe80::/10", "ff00::/8",
        "fd00:ec2::254/128",
    )
)


def is_public_address(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value.split("%", 1)[0])
    except ValueError:
        return False
    return address.is_global and not any(address.version == net.version and address in net for net in _BLOCKED_NETWORKS)


def validate_resolved_addresses(addresses: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    """Reject empty, malformed, or mixed public/internal DNS answer sets."""
    unique = tuple(dict.fromkeys(addresses))
    if not unique:
        raise SafetyError("The target hostname did not resolve to an address.")
    if any(not is_public_address(address) for address in unique):
        raise SafetyError("The target resolves to a non-public or special-use IP address; traffic was blocked.")
    return unique


def resolve_public_addresses(host: str, port: int) -> tuple[str, ...]:
    """Resolve all IPv4/IPv6 answers and fail if any answer is not globally routable."""
    try:
        rows = socket.getaddrinfo(host.rstrip(".") + ".", port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise SafetyError("The target hostname could not be resolved.") from exc
    addresses = [row[4][0] for row in rows]
    return validate_resolved_addresses(addresses)


def challenge_record_name(host: str) -> str:
    return f"_capacity-lab-verify.{host.rstrip('.').lower()}"


def new_challenge(origin_key: str) -> str:
    """Make a unique DNS token cryptographically scoped to one exact origin."""
    scope = hashlib.sha256(origin_key.encode("utf-8")).hexdigest()[:16]
    return f"{scope}.{secrets.token_urlsafe(24)}"


def verify_dns_challenge(host: str, token: str) -> bool:
    """Verify a DNS TXT ownership challenge without sending traffic to the target."""
    try:
        import dns.resolver  # type: ignore[import-not-found]
    except ImportError as exc:
        raise SafetyError("DNS verification support is unavailable; install backend requirements.") from exc
    name = challenge_record_name(host)
    try:
        answers = dns.resolver.resolve(name + ".", "TXT", lifetime=3.0)
    except Exception as exc:
        raise SafetyError("Ownership TXT record was not found or could not be queried.") from exc
    expected = f"capacity-lab-verification={token}"
    for answer in answers:
        raw = b"".join(answer.strings).decode("utf-8", "replace").strip('"')
        if secrets.compare_digest(raw, expected):
            return True
    return False


def validate_steps(steps: object) -> list[dict]:
    """Validate a small declarative request list; executable code is never accepted."""
    if not isinstance(steps, list) or not 1 <= len(steps) <= 30:
        raise SafetyError("A journey must have between 1 and 30 request steps.")
    clean: list[dict] = []
    for index, step in enumerate(steps, 1):
        if not isinstance(step, dict) or set(step) - {"method", "path", "headers", "body"}:
            raise SafetyError(f"Step {index} contains unsupported fields; journeys are declarative only.")
        method = step.get("method", "GET")
        path = step.get("path")
        if method not in {"GET", "HEAD", "POST"}:
            raise SafetyError(f"Step {index} has an unsupported method.")
        if not isinstance(path, str) or len(path) > 2048 or not path.startswith("/") or path.startswith("//"):
            raise SafetyError(f"Step {index} must use a relative path beginning with one slash.")
        if "\\" in path or any(ord(c) < 32 for c in path):
            raise SafetyError(f"Step {index} path contains forbidden characters.")
        parsed = urlsplit(path)
        if parsed.scheme or parsed.netloc or parsed.fragment:
            raise SafetyError(f"Step {index} cannot change the target origin or include a fragment.")
        headers = step.get("headers", {})
        if not isinstance(headers, dict) or len(headers) > 8:
            raise SafetyError(f"Step {index} headers are invalid or too numerous.")
        safe_headers: dict[str, str] = {}
        for key, value in headers.items():
            if not isinstance(key, str) or not isinstance(value, str):
                raise SafetyError(f"Step {index} headers must be text pairs.")
            low = key.lower()
            if low not in {"accept", "content-type"}:
                raise SafetyError(f"Step {index} header is not on the safe allowlist.")
            if len(key) > 64 or len(value) > 256 or any(c in key + value for c in "\r\n"):
                raise SafetyError(f"Step {index} header is too long or malformed.")
            safe_headers[key] = value
        body = step.get("body")
        if body is not None:
            if method != "POST":
                raise SafetyError(f"Step {index} request bodies are allowed only for POST.")
            if isinstance(body, (dict, list)):
                import json
                body = json.dumps(body, separators=(",", ":"))
            if not isinstance(body, str) or len(body.encode("utf-8")) > 2048:
                raise SafetyError(f"Step {index} body must be text or JSON up to 2 KB.")
        clean.append({"method": method, "path": path, "headers": safe_headers, "body": body})
    return clean
