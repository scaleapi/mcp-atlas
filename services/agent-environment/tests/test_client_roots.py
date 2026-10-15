"""Tests for MCP client roots configuration."""

from agent_environment.client_roots import DEFAULT_CLIENT_ROOTS, parse_client_roots


def test_default_roots(monkeypatch):
    monkeypatch.delenv("MCP_CLIENT_ROOTS", raising=False)
    assert parse_client_roots(None) == DEFAULT_CLIENT_ROOTS
    assert parse_client_roots("") == DEFAULT_CLIENT_ROOTS
    assert parse_client_roots("   ") == DEFAULT_CLIENT_ROOTS


def test_single_root():
    assert parse_client_roots("/data") == ["/data"]


def test_multiple_roots():
    assert parse_client_roots("/data,/tmp/workspace") == ["/data", "/tmp/workspace"]


def test_strips_whitespace():
    assert parse_client_roots(" /data , /tmp ") == ["/data", "/tmp"]
