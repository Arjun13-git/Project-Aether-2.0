"""Runtime configuration (LOCAL-DEV.md). Fails fast on invalid values."""

from __future__ import annotations

import ipaddress
from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from aether import science


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AETHER_", env_file=".env", extra="ignore")

    database_url: str = Field(default="", description="postgresql+psycopg://user:pass@host:port/db")
    storage_root: Path = Path("./.data")
    stac_api_url: str = "https://earth-search.aws.element84.com/v1"
    stac_collection: str = science.STAC_COLLECTION
    bind_host: str = "127.0.0.1"
    worker_poll_s: float = 1.0
    log_level: str = "INFO"

    @field_validator("stac_collection")
    @classmethod
    def _single_collection(cls, v: str) -> str:
        if v != science.STAC_COLLECTION:
            raise ValueError(f"MVP supports only {science.STAC_COLLECTION} (ADR-016)")
        return v

    @field_validator("stac_api_url")
    @classmethod
    def _https(cls, v: str) -> str:
        if not v.startswith("https://"):
            raise ValueError("STAC API URL must be https")
        return v.rstrip("/")

    @field_validator("bind_host")
    @classmethod
    def _loopback(cls, v: str) -> str:
        host = "127.0.0.1" if v == "localhost" else v
        if not ipaddress.ip_address(host).is_loopback:
            raise ValueError("MVP binds to loopback only; network exposure requires an auth ADR")
        return v

    def require_db(self) -> str:
        if not self.database_url:
            raise RuntimeError("AETHER_DATABASE_URL is not set (see .env.example)")
        return self.database_url


@lru_cache
def get_settings() -> Settings:
    return Settings()
