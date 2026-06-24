"""Konfiguration des Outbox-Publishers ueber Environment-Variablen (12-factor).

Eigene Konfiguration, getrennt vom Inventory. PUBLISHER_ENABLED hat einen EIGENEN
Default `false` (unabhaengig von EVENTS_ENABLED). Ist der Publisher deaktiviert,
sind DB-/Queue-Pflichtwerte NICHT erforderlich; ist er aktiviert, werden sie beim
Start fail closed verlangt. Alle Laufzeitwerte sind konfigurierbar und validiert.
"""
from __future__ import annotations

import os
from dataclasses import dataclass


def _bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


class ConfigError(ValueError):
    """Ungueltige/fehlende Publisher-Konfiguration — fail closed."""


@dataclass(frozen=True)
class Settings:
    enabled: bool
    database_url: str
    sqs_endpoint_url: str
    sqs_queue_url: str
    aws_region: str
    batch_size: int
    lease_seconds: int
    poll_interval_seconds: float
    backoff_base_seconds: float
    backoff_max_seconds: float
    sqs_connect_timeout: float
    sqs_read_timeout: float
    max_body_bytes: int
    pool_min_size: int
    pool_max_size: int
    pool_timeout_seconds: float

    @classmethod
    def from_env(cls) -> "Settings":
        s = cls(
            # Eigener Default false; NICHT EVENTS_ENABLED.
            enabled=_bool(os.getenv("PUBLISHER_ENABLED"), False),
            # Optional (nur bei enabled erforderlich) -> validate_enabled().
            database_url=os.getenv("DATABASE_URL", ""),
            sqs_endpoint_url=os.getenv("SQS_ENDPOINT_URL", ""),
            sqs_queue_url=os.getenv("SQS_QUEUE_URL", ""),
            aws_region=os.getenv("AWS_REGION", "eu-central-1"),
            batch_size=int(os.getenv("PUBLISHER_BATCH_SIZE", "5")),
            lease_seconds=int(os.getenv("PUBLISHER_LEASE_SECONDS", "60")),
            poll_interval_seconds=float(os.getenv("PUBLISHER_POLL_INTERVAL_SECONDS", "2")),
            backoff_base_seconds=float(os.getenv("PUBLISHER_BACKOFF_BASE_SECONDS", "5")),
            backoff_max_seconds=float(os.getenv("PUBLISHER_BACKOFF_MAX_SECONDS", "300")),
            sqs_connect_timeout=float(os.getenv("PUBLISHER_SQS_CONNECT_TIMEOUT", "2")),
            sqs_read_timeout=float(os.getenv("PUBLISHER_SQS_READ_TIMEOUT", "5")),
            # Muss zum Consumer-Contract passen (envelope.MAX_BODY_BYTES).
            max_body_bytes=int(os.getenv("PUBLISHER_MAX_BODY_BYTES", str(16 * 1024))),
            pool_min_size=int(os.getenv("DB_POOL_MIN_SIZE", "1")),
            pool_max_size=int(os.getenv("DB_POOL_MAX_SIZE", "4")),
            pool_timeout_seconds=float(os.getenv("DB_POOL_TIMEOUT_SECONDS", "5")),
        )
        s.validate_values()
        return s

    def validate_values(self) -> None:
        """Bereichspruefung der numerischen Laufzeitwerte (immer, unabhaengig von enabled)."""
        if self.batch_size < 1:
            raise ConfigError("PUBLISHER_BATCH_SIZE must be >= 1")
        if self.lease_seconds < 1:
            raise ConfigError("PUBLISHER_LEASE_SECONDS must be >= 1")
        if self.poll_interval_seconds <= 0:
            raise ConfigError("PUBLISHER_POLL_INTERVAL_SECONDS must be > 0")
        if self.backoff_base_seconds <= 0:
            raise ConfigError("PUBLISHER_BACKOFF_BASE_SECONDS must be > 0")
        if self.backoff_max_seconds < self.backoff_base_seconds:
            raise ConfigError("PUBLISHER_BACKOFF_MAX_SECONDS must be >= base")
        if self.sqs_connect_timeout <= 0 or self.sqs_read_timeout <= 0:
            raise ConfigError("SQS timeouts must be > 0")
        if self.max_body_bytes < 1:
            raise ConfigError("PUBLISHER_MAX_BODY_BYTES must be >= 1")
        if self.pool_min_size < 1 or self.pool_max_size < self.pool_min_size:
            raise ConfigError("invalid DB pool sizes")

    def validate_enabled(self) -> None:
        """Nur wenn enabled: Pflichtwerte (DB + Queue-URL) muessen gesetzt sein.

        Fail closed beim Start; keine DSN/Queue-URL in der Fehlermeldung.
        """
        if not self.enabled:
            return
        if not self.database_url:
            raise ConfigError("DATABASE_URL is required when PUBLISHER_ENABLED=true")
        if not self.sqs_queue_url:
            raise ConfigError("SQS_QUEUE_URL is required when PUBLISHER_ENABLED=true")


settings = Settings.from_env()
