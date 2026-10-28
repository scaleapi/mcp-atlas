"""Tests for the sandbox API's shared-secret auth (VULN-3072).

Builds a stand-in app with the real dependency, so no MCP servers are needed.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi import Depends, FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

SRC = Path(__file__).parent.parent / "src"
sys.path.insert(0, str(SRC))

from agent_environment.auth import (  # noqa: E402
    PUBLIC_PATHS,
    load_token,
    make_auth_dependency,
    token_matches,
)

TOKEN = "s3cret-token"


@pytest.fixture
def client() -> TestClient:
    app = FastAPI(dependencies=[Depends(make_auth_dependency(TOKEN))])

    @app.get("/")
    async def root():
        return {"ok": True}

    @app.get("/health")
    async def health():
        return {"ok": True}

    @app.post("/call-tool")
    async def call_tool():
        return {"ran": True}

    @app.post("/list-tools")
    async def list_tools():
        return []

    @app.post("/cache-clear")
    async def cache_clear():
        return {}

    @app.get("/enabled-servers")
    async def enabled_servers():
        return {}

    return TestClient(app)


PROTECTED = [
    ("post", "/call-tool"),
    ("post", "/list-tools"),
    ("post", "/cache-clear"),
    ("get", "/enabled-servers"),
]


@pytest.mark.parametrize("method,path", PROTECTED)
def test_missing_token_is_rejected(client, method, path):
    resp = getattr(client, method)(path)
    assert resp.status_code == 401
    assert resp.headers["www-authenticate"] == "Bearer"


@pytest.mark.parametrize(
    "header", ["Bearer wrong", f"Bearer {TOKEN}x", TOKEN, f"Basic {TOKEN}", ""]
)
def test_wrong_token_is_rejected(client, header):
    resp = client.post("/call-tool", headers={"Authorization": header})
    assert resp.status_code == 401
    assert "ran" not in resp.text


@pytest.mark.parametrize("method,path", PROTECTED)
def test_correct_token_is_accepted(client, method, path):
    resp = getattr(client, method)(path, headers={"Authorization": f"Bearer {TOKEN}"})
    assert resp.status_code == 200


@pytest.mark.parametrize("path", sorted(PUBLIC_PATHS))
def test_liveness_probes_stay_open(client, path):
    assert client.get(path).status_code == 200


def test_token_matches():
    assert token_matches(f"Bearer {TOKEN}", TOKEN)
    assert not token_matches(None, TOKEN)
    assert not token_matches(f"bearer {TOKEN}", TOKEN)


def test_service_refuses_to_start_without_token(monkeypatch):
    monkeypatch.delenv("SANDBOX_AUTH_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="SANDBOX_AUTH_TOKEN"):
        load_token()
    monkeypatch.setenv("SANDBOX_AUTH_TOKEN", "   ")
    with pytest.raises(RuntimeError):
        load_token()
    monkeypatch.setenv("SANDBOX_AUTH_TOKEN", TOKEN)
    assert load_token() == TOKEN


def test_main_app_applies_auth_to_every_route():
    """main.py must attach the dependency app-wide, so new endpoints are covered too."""
    main = (SRC / "agent_environment" / "main.py").read_text()
    assert re.search(r"require_token\s*=\s*make_auth_dependency\(load_token\(\)\)", main)
    assert re.search(r"FastAPI\([^)]*dependencies=\[Depends\(require_token\)\]", main)
