"""
Environment-driven settings.

Every credential is read from os.environ (populated from bot/.env via python-dotenv).
Nothing here is ever written to disk.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

# Load bot/.env if present. Real deployments (systemd on the VPS) may inject env directly.
load_dotenv(Path(__file__).resolve().parents[1] / ".env")


def _req(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


@dataclass(frozen=True)
class Settings:
    # Supabase
    supabase_url: str = field(default_factory=lambda: _req("SUPABASE_URL"))
    supabase_service_role_key: str = field(default_factory=lambda: _req("SUPABASE_SERVICE_ROLE_KEY"))

    # Polymarket
    pm_private_key: str = field(default_factory=lambda: _req("POLYMARKET_PRIVATE_KEY"))
    pm_funder_address: str = field(default_factory=lambda: os.environ.get("POLYMARKET_FUNDER_ADDRESS", ""))
    pm_api_key: str = field(default_factory=lambda: os.environ.get("POLYMARKET_API_KEY", ""))
    pm_api_secret: str = field(default_factory=lambda: os.environ.get("POLYMARKET_API_SECRET", ""))
    pm_api_passphrase: str = field(default_factory=lambda: os.environ.get("POLYMARKET_API_PASSPHRASE", ""))
    pm_chain_id: int = field(default_factory=lambda: int(os.environ.get("POLYMARKET_CHAIN_ID", "137")))

    # Jev
    jev_api_url: str = field(default_factory=lambda: os.environ.get("JEV_API_URL", "https://api.jev.ai/v1"))
    jev_api_key: str = field(default_factory=lambda: _req("JEV_API_KEY"))
    jev_model: str = field(default_factory=lambda: os.environ.get("JEV_MODEL", "system-one"))

    # Weather
    nws_user_agent: str = field(default_factory=lambda: _req("NWS_USER_AGENT"))
    madis_api_token: str = field(default_factory=lambda: os.environ.get("MADIS_API_TOKEN", ""))

    # Runtime
    log_level: str = field(default_factory=lambda: os.environ.get("LOG_LEVEL", "INFO"))


settings = Settings()
