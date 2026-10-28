import socket

import pytest

from backend.safety import (
    SafetyError,
    Origin,
    parse_origin,
    origin_from_url,
    validate_resolved_addresses,
    resolve_public_addresses,
    validate_steps,
)


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1", "10.1.2.3", "100.64.0.1", "169.254.10.20", "169.254.169.254",
        "172.16.1.2", "192.168.1.9", "192.0.2.10", "198.18.0.1", "203.0.113.4",
        "224.0.0.1", "240.0.0.1", "168.63.129.16", "100.100.100.200",
        "::", "::1", "::ffff:127.0.0.1", "fc00::1", "fe80::1", "ff02::1",
        "2001:db8::10", "fd00:ec2::254",
        "64:ff9b::a9fe:a9fe", "64:ff9b::a00:1", "64:ff9b::7f00:1",
        "2002:a9fe:a9fe::", "2002:0a00:0001::", "2002:7f00:0001::",
    ],
)
def test_every_private_special_or_internal_ip_is_rejected(address):
    with pytest.raises(SafetyError):
        validate_resolved_addresses([address])


def test_mixed_public_and_private_dns_answers_fail_closed():
    with pytest.raises(SafetyError, match="non-public"):
        validate_resolved_addresses(["93.184.216.34", "10.0.0.8"])


def test_mixed_public_and_nat64_metadata_answers_fail_closed():
    with pytest.raises(SafetyError, match="non-public"):
        validate_resolved_addresses(["93.184.216.34", "64:ff9b::a9fe:a9fe"])


def test_every_v4_v6_answer_must_be_public():
    with pytest.raises(SafetyError):
        validate_resolved_addresses(["93.184.216.34", "2001:db8::1"])


def test_public_address_set_is_deduplicated():
    assert validate_resolved_addresses(["93.184.216.34", "93.184.216.34"]) == ("93.184.216.34",)


@pytest.mark.parametrize(
    "value",
    [
        "ftp://example.com", "http://user:pass@example.com", "https://example.com/path",
        "https://example.com?next=internal", "http://127.0.0.1", "http://[::1]",
        "http://localhost", "http://example.com\\@127.0.0.1", "http://example.com%2f@127.0.0.1",
    ],
)
def test_unsafe_or_non_origin_target_urls_are_rejected(value):
    with pytest.raises(SafetyError):
        parse_origin(value)


def test_origin_is_normalized_and_includes_exact_port():
    assert parse_origin("HTTPS://Example.COM/") == Origin("https", "example.com", 443)
    assert parse_origin("https://example.com:8443") != parse_origin("https://example.com")


def test_redirect_origin_can_be_compared_without_losing_port_or_scheme():
    authorized = parse_origin("https://staging.example.com:8443")
    assert origin_from_url("https://staging.example.com:8443/redirected") == authorized
    assert origin_from_url("https://staging.example.com/redirected") != authorized
    assert origin_from_url("http://staging.example.com:8443/redirected") != authorized


def test_resolution_rejects_mixed_public_private_answers(monkeypatch):
    def fake_getaddrinfo(host, port, type):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port)),
            (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("fd00::12", port, 0, 0)),
        ]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    with pytest.raises(SafetyError):
        resolve_public_addresses("staging.example.com", 443)


def test_dns_answer_is_rechecked_instead_of_assumed_stable(monkeypatch):
    answers = ["93.184.216.34", "169.254.169.254"]
    calls = []

    def fake_getaddrinfo(host, port, type):
        address = answers[len(calls)]
        calls.append(address)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    assert resolve_public_addresses("staging.example.com", 443) == ("93.184.216.34",)
    with pytest.raises(SafetyError, match="non-public"):
        resolve_public_addresses("staging.example.com", 443)
    assert len(calls) == 2


def test_journey_is_declarative_and_cannot_supply_code_or_absolute_urls():
    with pytest.raises(SafetyError, match="unsupported fields"):
        validate_steps([{"method": "GET", "path": "/", "script": "fetch('http://169.254.169.254')"}])
    with pytest.raises(SafetyError):
        validate_steps([{"method": "GET", "path": "https://attacker.example/"}])
    with pytest.raises(SafetyError):
        validate_steps([{"method": "GET", "path": "/", "headers": {"Host": "169.254.169.254"}}])
    with pytest.raises(SafetyError):
        validate_steps([{"method": "GET", "path": "/", "headers": {"Authorization": "Bearer secret"}}])
    with pytest.raises(SafetyError):
        validate_steps([{"method": "GET", "path": "/", "headers": {"X-Test-Authorization": "secret"}}])
    with pytest.raises(SafetyError):
        validate_steps([{"method": "POST", "path": "/", "body": "x" * 2049}])


def test_only_supported_methods_and_safe_relative_paths_are_allowed():
    assert validate_steps([{"method": "GET", "path": "/home"}])[0]["path"] == "/home"
    for method, path in [("DELETE", "/account"), ("GET", "//evil.example/"), ("GET", "/a#b")]:
        with pytest.raises(SafetyError):
            validate_steps([{"method": method, "path": path}])
