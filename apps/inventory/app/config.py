"""Konfiguration ueber Environment-Variablen (12-factor).

Nichts hartkodiert. Der SQS-Endpoint ist bewusst eine Variable
(AWS-portabel, in Phase 3 = Toxiproxy-Adresse).
"""
from __future__ import annotations

import os
from dataclasses import dataclass


def _bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    database_url: str
    events_enabled: bool
    sqs_endpoint_url: str
    sqs_queue_url: str
    aws_region: str
    pool_min_size: int
    pool_max_size: int

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            database_url=os.environ["DATABASE_URL"],
            events_enabled=_bool(os.getenv("EVENTS_ENABLED"), False),
            sqs_endpoint_url=os.getenv("SQS_ENDPOINT_URL", ""),
            sqs_queue_url=os.getenv("SQS_QUEUE_URL", ""),
            aws_region=os.getenv("AWS_REGION", "eu-central-1"),
            pool_min_size=int(os.getenv("DB_POOL_MIN_SIZE", "1")),
            pool_max_size=int(os.getenv("DB_POOL_MAX_SIZE", "10")),
        )


settings = Settings.from_env()
