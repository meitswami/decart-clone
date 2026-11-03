"""Signed, short-lived tickets that let a browser use OUR live GPU engine.

Minted by the VPS (/live/token, LIVE_BACKEND=local) and verified by the GPU
box (webrtc/live_server.py) with the shared ENGINE_SECRET. Same idea as the
scoped Decart key: no ticket, no GPU time; tickets expire fast and carry the
session cap.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time


class TicketError(Exception):
    pass


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def mint_ticket(secret: str, *, session_id: str, store_id: str, max_seconds: int,
                ttl: int = 60) -> str:
    if not secret:
        raise TicketError("ENGINE_SECRET not set")
    body = _b64(json.dumps({"sid": session_id, "store_id": store_id, "max_seconds": max_seconds,
                            "exp": int(time.time()) + ttl}, separators=(",", ":")).encode())
    sig = _b64(hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest())
    return f"{body}.{sig}"


def verify_ticket(ticket: str, secret: str) -> dict:
    if not secret:
        raise TicketError("engine not configured")
    try:
        body, sig = ticket.split(".", 1)
    except ValueError:
        raise TicketError("bad ticket") from None
    want = _b64(hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(sig, want):
        raise TicketError("bad ticket")
    claims = json.loads(_unb64(body))
    if claims.get("exp", 0) < time.time():
        raise TicketError("ticket expired")
    return claims
