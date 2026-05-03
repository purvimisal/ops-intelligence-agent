from __future__ import annotations

from typing import Any, List

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    anthropic_api_key: str = ""
    openai_api_key: str = ""
    database_url: str = "postgresql://opsagent:opsagent@localhost:5432/opsagent"
    redis_url: str = "redis://localhost:6379/0"
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_host: str = "https://cloud.langfuse.com"
    slack_webhook_url: str = ""

    log_level: str = "INFO"
    signal_rate: int = 100
    metrics_interval_sec: int = 30
    risk_score_interval_sec: int = 30
    chaos_mode: bool = False

    # --- Detection thresholds (all configurable via env) ---
    error_rate_warn: float = 0.05
    error_rate_critical: float = 0.15
    consumer_lag_warn: float = 5000.0
    consumer_lag_critical: float = 20000.0
    memory_pct_warn: float = 80.0
    memory_pct_critical: float = 90.0
    cpu_pct_warn: float = 80.0
    cpu_pct_critical: float = 90.0

    # --- Detection window config ---
    window_size_min: int = 5
    trend_window_steps: int = 10
    pattern_similarity_threshold: float = 0.75

    # --- Monitored services (comma-separated env var or list) ---
    monitored_services: List[str] = [
        "kafka-consumer",
        "pricing-engine",
        "audit-service",
        "payment-processor",
    ]

    @field_validator("monitored_services", mode="before")
    @classmethod
    def _parse_monitored_services(cls, v: Any) -> List[str]:
        if isinstance(v, str):
            return [s.strip() for s in v.split(",") if s.strip()]
        return v


settings = Settings()
