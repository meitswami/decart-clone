"""Central config, loaded from environment / .env."""
from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    brand_name: str = "Your Brand"

    live_backend: str = "decart"  # "decart" | "local"

    # ---- Decart (white-label live backend) ----
    decart_api_key: str = ""
    decart_model: str = "lucy-vton-3.6"
    decart_base_url: str = "https://api.decart.ai"
    # Only gates *starting* a session (same as the production Node app).
    decart_token_ttl_seconds: int = 120

    # Site address, e.g. https://tryon.example.com. When set, /live/token only
    # answers requests from this origin and Decart keys only work from it.
    public_origin: str = ""
    # Optional shared code visitors must send (x-access-code) to start live.
    access_code: str = ""
    # Behind nginx: trust X-Real-IP / X-Forwarded-For for per-visitor limits.
    trust_proxy: bool = False
    tokens_per_ip_per_hour: int = 10

    # Our own live engine on the GPU box (LIVE_BACKEND=local). Public HTTPS URL,
    # e.g. https://<pod-id>-8900.proxy.runpod.net. ENGINE_SECRET is shared with
    # the GPU box and signs per-session tickets.
    gpu_engine_url: str = "http://127.0.0.1:8900"
    engine_secret: str = ""
    # WebRTC ICE servers for the local engine, as JSON. Pods take no inbound
    # UDP, so production needs a TURN relay (e.g. coturn on the VPS).
    ice_servers: str = '[{"urls": "stun:stun.l.google.com:19302"}]'
    # "ws" = frames over a WebSocket on the page's own origin (works through
    # RunPod's HTTPS proxy and on mobile data); "webrtc" = webrtc/live_server.py.
    live_transport: str = "ws"
    engine_max_sessions: int = 2
    live_key_interval: float = 1.0
    keepalive_file: str = "/workspace/.keepalive"

    # Consented training captures (photos, live frames, videos) + admin gallery.
    captures_dir: str = "./data/captures"
    admin_token: str = ""

    max_session_seconds: int = 20
    per_store_daily_sessions: int = 100
    site_daily_sessions: int = 2000
    site_daily_seconds: int = 120_000

    recording_enabled: bool = True
    record_output_frames: bool = False
    recordings_dir: str = "./data/recordings"
    recording_retention_days: int = 365

    host: str = "0.0.0.0"
    # 3070 is taken on the VPS by the production Node app (live-virtual-try-on).
    port: int = 3071

    @property
    def origin(self) -> str:
        return self.public_origin.strip().rstrip("/").lower()


settings = Settings()
