"""
Environment-driven settings.

Every credential is read from os.environ (populated from bot/.env via python-dotenv).
Nothing here is ever written to disk.

Only Supabase is required to start. Polymarket and Jev credentials are optional at
load time so the discovery tool and simulation mode run without them; the modules
that need them call `require()` and fail loudly at the point of use.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

# Load bot/.env if present. Real deployments (systemd on the VPS) may inject env directly.
load_dotenv(Path(__file__).resolve().parents[1] / ".env")


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


@dataclass(frozen=True)
class Settings:
    # Supabase
    supabase_url: str = field(default_factory=lambda: _env("SUPABASE_URL"))
    supabase_service_role_key: str = field(default_factory=lambda: _env("SUPABASE_SERVICE_ROLE_KEY"))

    # Polymarket (API creds are derived from the private key by the SDK if omitted)
    pm_private_key: str = field(default_factory=lambda: _env("POLYMARKET_PRIVATE_KEY"))
    pm_wallet_address: str = field(default_factory=lambda: _env("POLYMARKET_FUNDER_ADDRESS"))

    # Jev (TypeSafe System One). Pin the model so behaviour doesn't change under us.
    jev_api_key: str = field(default_factory=lambda: _env("TYPESAFE_API_KEY") or _env("JEV_API_KEY"))
    jev_model: str = field(default_factory=lambda: _env("TYPESAFE_DEFAULT_MODEL") or _env("JEV_MODEL", "jev-1.13.0"))

    # Open-Meteo commercial key (free endpoint is non-commercial only)
    open_meteo_api_key: str = field(default_factory=lambda: _env("OPEN_METEO_API_KEY"))

    # Weather — public APIs ask for a contact User-Agent
    user_agent: str = field(default_factory=lambda: _env("NWS_USER_AGENT", "WeatherBotMark3"))

    # Runtime
    log_level: str = field(default_factory=lambda: _env("LOG_LEVEL", "INFO"))

    def require(self, *names: str) -> None:
        missing = [n for n in names if not getattr(self, n)]
        if missing:
            env_names = {
                "supabase_url": "SUPABASE_URL",
                "supabase_service_role_key": "SUPABASE_SERVICE_ROLE_KEY",
                "pm_private_key": "POLYMARKET_PRIVATE_KEY",
                "jev_api_key": "TYPESAFE_API_KEY",
            }
            raise RuntimeError(
                "Missing required environment variable(s) in bot/.env: "
                + ", ".join(env_names.get(n, n) for n in missing)
            )


settings = Settings()
