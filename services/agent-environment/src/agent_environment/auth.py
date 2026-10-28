"""Shared-secret auth for the sandbox HTTP API.

Every endpoint can run tools with the operator's API keys (including code
execution), so callers must send `Authorization: Bearer <SANDBOX_AUTH_TOKEN>`.
Only the liveness probes stay open: they run no tools and expose no config.
"""

import hmac
import os

from fastapi import HTTPException, Request

TOKEN_ENV_VAR = "SANDBOX_AUTH_TOKEN"
PUBLIC_PATHS = frozenset({"/", "/health"})


def load_token() -> str:
    """Return the shared secret, refusing to start the service without one."""
    token = os.environ.get(TOKEN_ENV_VAR, "").strip()
    if not token:
        raise RuntimeError(
            f"{TOKEN_ENV_VAR} is not set. The sandbox API runs tools with your API keys, "
            f"so it requires a shared secret: add {TOKEN_ENV_VAR}=<random value> to .env "
            f"(e.g. `openssl rand -hex 32`; `make run-docker` and run_all.sh generate one) "
            f"and pass it with --env-file .env."
        )
    return token


def token_matches(header: str | None, token: str) -> bool:
    """Constant-time check of an Authorization header against the token."""
    return hmac.compare_digest((header or "").encode(), f"Bearer {token}".encode())


def make_auth_dependency(token: str):
    """Build a FastAPI dependency that rejects requests without the bearer token."""

    async def require_token(request: Request) -> None:
        if request.url.path in PUBLIC_PATHS:
            return
        if not token_matches(request.headers.get("authorization"), token):
            raise HTTPException(
                status_code=401,
                detail="Unauthorized",
                headers={"WWW-Authenticate": "Bearer"},
            )

    return require_token
