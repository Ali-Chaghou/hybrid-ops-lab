"""SQS-Consumer: Long-Poll -> verarbeiten -> nach Erfolg loeschen.

at-least-once: Eine Nachricht wird erst nach erfolgreicher Verarbeitung
geloescht. Schlaegt die Verarbeitung fehl, bleibt sie in der Queue und wird
nach Ablauf des Visibility-Timeouts erneut zugestellt. Die Verarbeitung muss
daher idempotent sein.

boto3 wird lazy importiert (wie in der inventory-App).
"""
from __future__ import annotations

import json
import logging
import threading
import time

from app.config import settings
from app.metrics import (
    MESSAGES_CONSUMED,
    MESSAGE_PROCESSING_DURATION,
    QUEUE_DEPTH,
    RECEIVE_ERRORS,
)

log = logging.getLogger("consumer")


class SqsConsumer:
    """Hintergrund-Loop, der die Queue pollt. Stoppt sauber via Event."""

    def __init__(self) -> None:
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_poll_ok = 0.0

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="sqs-consumer", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=settings.poll_wait_seconds + 5)

    @property
    def healthy(self) -> bool:
        """readyz: zuletzt erfolgreicher Poll nicht zu lange her."""
        return (time.time() - self._last_poll_ok) < (settings.poll_wait_seconds + 30)

    def _client(self):
        import boto3  # lazy, wie in der inventory-App
        from botocore.config import Config

        config = Config(
            # read_timeout MUSS groesser als poll_wait_seconds sein, sonst
            # bricht jeder Long-Poll-Receive ab (anders als beim Publish).
            connect_timeout=5,
            read_timeout=settings.poll_wait_seconds + 10,
            retries={"max_attempts": 0},
        )
        return boto3.client(
            "sqs",
            endpoint_url=settings.sqs_endpoint_url or None,
            region_name=settings.aws_region,
            config=config,
        )

    def _run(self) -> None:
        client = self._client()
        log.info("consumer started, polling %s", settings.sqs_queue_url)
        while not self._stop.is_set():
            try:
                resp = client.receive_message(
                    QueueUrl=settings.sqs_queue_url,
                    MaxNumberOfMessages=settings.max_messages_per_poll,
                    WaitTimeSeconds=settings.poll_wait_seconds,
                    VisibilityTimeout=settings.visibility_timeout_seconds,
                )
                self._last_poll_ok = time.time()
                self._update_depth(client)
            except Exception as exc:
                RECEIVE_ERRORS.inc()
                log.warning("receive failed: %s", exc)
                self._stop.wait(2)  # kurz zurueckhalten, dann neuer Versuch
                continue

            for message in resp.get("Messages", []):
                self._handle(client, message)

    def _handle(self, client, message: dict) -> None:
        start = time.perf_counter()
        try:
            payload = json.loads(message.get("Body", "{}"))
            log.info("processed movement: %s", payload)
            client.delete_message(
                QueueUrl=settings.sqs_queue_url,
                ReceiptHandle=message["ReceiptHandle"],
            )
            MESSAGES_CONSUMED.labels(result="success").inc()
        except Exception as exc:
            # bewusst nicht loeschen -> Redelivery nach Visibility-Timeout
            MESSAGES_CONSUMED.labels(result="error").inc()
            log.warning("processing failed, message will be redelivered: %s", exc)
        finally:
            MESSAGE_PROCESSING_DURATION.observe(time.perf_counter() - start)

    def _update_depth(self, client) -> None:
        try:
            attrs = client.get_queue_attributes(
                QueueUrl=settings.sqs_queue_url,
                AttributeNames=["ApproximateNumberOfMessages"],
            )
            QUEUE_DEPTH.set(int(attrs["Attributes"]["ApproximateNumberOfMessages"]))
        except Exception as exc:
            log.debug("queue depth update failed: %s", exc)


consumer = SqsConsumer()
