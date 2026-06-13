"""SQS-Publish hinter Feature-Flag.

Phase 2: EVENTS_ENABLED=false -> No-op (result="skipped").
Phase 3: SQS_ENDPOINT_URL = Toxiproxy-Adresse (Variable, kein Hardcode).
boto3 wird lazy importiert — die Abhaengigkeit existiert erst, wenn der
Flow aktiv ist, und kommt mit Phase 3 in requirements.txt.
"""
from __future__ import annotations

import json
import logging
import time

from app.config import settings
from app.metrics import EVENT_PUBLISH_DURATION, EVENTS_PUBLISHED

log = logging.getLogger("inventory.events")


def publish_movement(movement: dict) -> None:
    if not settings.events_enabled:
        EVENTS_PUBLISHED.labels(result="skipped").inc()
        return

    import boto3  # lazy: nur noetig, wenn der Flow aktiv ist (Phase 3)
    from botocore.config import Config

    config = Config(
        connect_timeout=2,
        read_timeout=2,
        retries={"max_attempts": 0},  # schnell scheitern statt haengen
    )
    client = boto3.client(
        "sqs",
        endpoint_url=settings.sqs_endpoint_url or None,
        region_name=settings.aws_region,
        config=config,
    )

    body = json.dumps(movement, default=str)
    start = time.perf_counter()
    try:
        client.send_message(QueueUrl=settings.sqs_queue_url, MessageBody=body)
        EVENTS_PUBLISHED.labels(result="success").inc()
    except Exception as exc:  # DB ist Source of Truth — Request nicht killen
        EVENTS_PUBLISHED.labels(result="error").inc()
        log.warning("publish to queue failed: %s", exc)
    finally:
        EVENT_PUBLISH_DURATION.observe(time.perf_counter() - start)
