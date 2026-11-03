"""Central config, loaded from environment / .env."""
from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    brand_name: str = "Your Brand"

    live_backend: str = "decart"  # "decart" | "local"

    decart_api_key: str = ""
    decart_model: str = "lucy-vton-3.6"

    gpu_engine_url: str = "http://127.0.0.1:8900"

    max_session_seconds: int = 20
    per_store_daily_sessions: int = 100
    site_daily_sessions: int = 2000
    site_daily_seconds: int = 120_000

    recording_enabled: bool = True
    record_output_frames: bool = False
    recordings_dir: str = "./data/recordings"
    recording_retention_days: int = 365

    host: str = "0.0.0.0"
    port: int = 3070


settings = Settings()
