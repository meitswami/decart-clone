"""Mint short-lived, scoped Decart client keys. The real key never leaves here.

Same call the production Node app makes through @decartai/sdk
(`client.tokens.create`), done directly over HTTP:

    POST {DECART_BASE_URL}/v1/client/tokens
    X-API-KEY: <real key>
    {"expiresIn": 120, "allowedModels": [...], "allowedOrigins": [...],
     "constraints": {"realtime": {"maxSessionDuration": 20}}}
 -> {"apiKey": "ek_...", "expiresAt": "...", "permissions": {...}, ...}

Docs: https://docs.platform.decart.ai/api-reference/create-client-token
"""
from __future__ import annotations

import logging

import httpx

from .config import settings

log = logging.getLogger("tryon.decart")


class DecartTokenError(Exception):
    """`.message` is safe to show the shopper; details go to the log only."""

    def __init__(self, message: str, status: int = 502):
        self.message = message
        self.status = status
        super().__init__(message)


def _payload(model: str, origin: str, max_seconds: int) -> dict:
    body: dict = {
        "expiresIn": settings.decart_token_ttl_seconds,
        "allowedModels": [model],
        "constraints": {"realtime": {"maxSessionDuration": max_seconds}},
    }
    if origin:
        body["allowedOrigins"] = [origin]
    return body


async def mint_client_token(*, model: str, origin: str, max_seconds: int,
                            metadata: dict | None = None,
                            client: httpx.AsyncClient | None = None) -> dict:
    """Return {"apiKey", "expiresAt"} for the browser. Raises DecartTokenError."""
    if not settings.decart_api_key:
        raise DecartTokenError("Live try-on isn't set up yet.", status=503)

    body = _payload(model, origin, max_seconds)
    if metadata:
        body["metadata"] = metadata
    url = settings.decart_base_url.rstrip("/") + "/v1/client/tokens"
    headers = {"X-API-KEY": settings.decart_api_key,
               "Content-Type": "application/json",
               "User-Agent": "virtual-tryon-server/1.0"}

    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=15)
    try:
        resp = await client.post(url, json=body, headers=headers)
    except httpx.HTTPError as e:
        log.error("Decart token request failed: %s", e)
        raise DecartTokenError("Couldn't reach the live service right now.") from e
    finally:
        if owns_client:
            await client.aclose()

    if resp.status_code != 200:
        # Never echo Decart's body to the browser; it can name the provider.
        log.error("Decart refused to mint a token: %s %s",
                  resp.status_code, resp.text[:300])
        if resp.status_code in (401, 403):
            raise DecartTokenError("Live try-on isn't available right now.", 503)
        raise DecartTokenError("Couldn't start a live session right now.")

    data = resp.json()
    key = data.get("apiKey")
    if not key or key == settings.decart_api_key:
        # Belt and braces: never hand the real key to a browser.
        log.error("Decart token response had no usable apiKey")
        raise DecartTokenError("Couldn't start a live session right now.")
    return {"apiKey": key, "expiresAt": data.get("expiresAt")}
