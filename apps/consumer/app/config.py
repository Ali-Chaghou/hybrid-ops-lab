"""Konfiguration ueber Environment-Variablen (12-factor).

Nichts hartkodiert. SQS-Endpoint und Queue-URL sind bewusst Variablen
(AWS-portabel; in Phase 5 zeigt der Endpoint auf die Toxiproxy-Adresse).
"""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    database_url: str
    sqs_endpoint_url: str
    sqs_queue_url: str
    aws_region: str
    poll_wait_seconds: int
    visibility_timeout_seconds: int
    max_messages_per_poll: int
    pool_min_size: int
    pool_max_size: int
    pool_timeout_seconds: float

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            # Idempotency-Store des Consumers (consumer_app, Least-Privilege).
            # Pflicht: ohne erreichbare DB + gueltiges Schema startet der Consumer nicht.
            database_url=os.environ["DATABASE_URL"],
            sqs_endpoint_url=os.getenv("SQS_ENDPOINT_URL", ""),
            sqs_queue_url=os.environ["SQS_QUEUE_URL"],
            aws_region=os.getenv("AWS_REGION", "eu-central-1"),
            poll_wait_seconds=int(os.getenv("POLL_WAIT_SECONDS", "20")),
            visibility_timeout_seconds=int(os.getenv("VISIBILITY_TIMEOUT_SECONDS", "30")),
            max_messages_per_poll=int(os.getenv("MAX_MESSAGES_PER_POLL", "10")),
            # DB-Connection-Pool (psycopg_pool): am Prozessstart geoeffnet, am
            # Shutdown geschlossen. Klein gehalten — ein einzelner Poller-Thread.
            pool_min_size=int(os.getenv("DB_POOL_MIN_SIZE", "1")),
            pool_max_size=int(os.getenv("DB_POOL_MAX_SIZE", "4")),
            pool_timeout_seconds=float(os.getenv("DB_POOL_TIMEOUT_SECONDS", "5")),
        )


settings = Settings.from_env()
