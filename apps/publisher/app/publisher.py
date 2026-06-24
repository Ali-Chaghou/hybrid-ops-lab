"""Outbox-Publisher-Runtime: claim (Lease) -> publish (ausserhalb DB-Tx) -> finalize.

Genau EIN Poller-Thread pro Prozess, standardmaessig DEAKTIVIERT
(PUBLISHER_ENABLED=false). Deaktiviert: kein Claim, kein SQS-Client, kein Publish,
keine DB-Mutation — nur Health/Metrics. Aktiviert: kurze Lease-Claims, einzelnes
SQS SendMessage, Fencing-Finalisierung; Status 'published' erst NACH bestaetigtem
Publish; Outbox-Zeilen werden NIE geloescht, KEINE neue event_id, KEIN Publish aus
dem HTTP-Request-Pfad. Keine Exactly-once-Garantie (Consumer-Idempotenz faengt
Duplikate ab). Logs nur event_id/Outcome — nie Payload/DSN/Queue-URL.
"""
from __future__ import annotations

import logging
import secrets
import threading
import time

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from app import db
from app.config import settings
from app.envelope import EnvelopeTooLarge, build_body
from app.metrics import (
    AVAILABLE_PENDING,
    CLAIMED_ROWS,
    CLAIMED_TOTAL,
    FINALIZE_CONFLICTS,
    LAST_PUBLISH_TIMESTAMP,
    OLDEST_AVAILABLE_AGE,
    POLL_ERRORS,
    POLLS_OK,
    PUBLISH_ERRORS,
    PUBLISH_SUCCESS,
    RETRIES,
)

log = logging.getLogger("publisher")

_MAX_ERROR_LEN = 64
_MAX_BACKOFF_EXP = 60  # Overflow-/Extremwert-Schutz fuer 2**(attempt-1)


def compute_backoff(attempt_count: int, base_seconds: float, max_seconds: float) -> float:
    """min(base * 2**(attempt-1), max); nie negativ, Overflow sicher begrenzt."""
    attempt = attempt_count if attempt_count >= 1 else 1
    exp = min(attempt - 1, _MAX_BACKOFF_EXP)
    value = base_seconds * (2 ** exp)
    return float(min(value, max_seconds))


def sanitize_error(exc: BaseException) -> str:
    """Nur ein begrenzter Fehlercode (Exception-Typ), feste Maximallaenge.

    KEINE Exception-Nachricht (koennte Payload/DSN/Queue-URL enthalten)."""
    return type(exc).__name__[:_MAX_ERROR_LEN]


class Publisher:
    def __init__(self, *, config=None, owner: str | None = None) -> None:
        self._cfg = config if config is not None else settings
        # Opaker, prozess-zufaelliger Claim-Token (kein Host/IP/Benutzer; kein Label).
        self._owner = owner if owner is not None else secrets.token_hex(16)

        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._schema_ok = False
        self._pool: ConnectionPool | None = None

    @property
    def owner(self) -> str:
        return self._owner

    @property
    def enabled(self) -> bool:
        return self._cfg.enabled

    # --- Lifecycle ------------------------------------------------------------

    def _ensure_pool(self) -> ConnectionPool:
        if self._pool is None:
            self._pool = ConnectionPool(
                conninfo=self._cfg.database_url,
                min_size=self._cfg.pool_min_size,
                max_size=self._cfg.pool_max_size,
                timeout=self._cfg.pool_timeout_seconds,
                open=False,
                check=ConnectionPool.check_connection,
                kwargs={"autocommit": False, "connect_timeout": 5, "row_factory": dict_row},
            )
        return self._pool

    def verify_schema(self) -> None:
        """Startup-Gate (nur enabled): DB erreichbar + Migration 0004 vorhanden."""
        pool = self._ensure_pool()
        pool.open(wait=True, timeout=self._cfg.pool_timeout_seconds)
        with pool.connection() as conn:
            db.check_schema(conn)
        self._schema_ok = True
        log.info("publisher schema validated")

    def start(self) -> None:
        # Deaktiviert: KEIN Pool, KEIN Thread, KEINE DB/Queue. Nur messbar als aus.
        if not self._cfg.enabled:
            log.info("publisher disabled (PUBLISHER_ENABLED=false) — idle")
            return
        self._cfg.validate_enabled()  # fail closed bei fehlenden Pflichtwerten
        self.verify_schema()
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                log.warning("publisher already running; ignoring duplicate start()")
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="outbox-publisher", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=self._cfg.sqs_read_timeout + 10)
            if thread.is_alive():
                log.error("publisher poller did not stop within timeout; pool left open")
                return
            self._thread = None
        if self._pool is not None:
            try:
                self._pool.close()
            except Exception:  # pragma: no cover
                pass

    # --- Health / Readiness ---------------------------------------------------

    @property
    def live(self) -> bool:
        # Deaktiviert: Prozess lebt absichtlich idle. Aktiviert: Poller-Thread lebt.
        if not self._cfg.enabled:
            return True
        return self._thread is not None and self._thread.is_alive()

    @property
    def ready(self) -> bool:
        # Deaktiviert: bewusst idle -> bereit (kein Fehler). Aktiviert: Schema+DB ok.
        if not self._cfg.enabled:
            return True
        if not self._schema_ok or self._pool is None:
            return False
        return self._db_ping()

    def _db_ping(self) -> bool:
        if self._pool is None:
            return False
        try:
            with self._pool.connection() as c:
                c.execute("SELECT 1")
            return True
        except Exception as exc:
            log.warning("db ping failed: %s", type(exc).__name__)
            return False

    # --- SQS ------------------------------------------------------------------

    def _sqs_client(self):
        import boto3  # lazy: nur bei aktiviertem Publisher
        from botocore.config import Config

        config = Config(
            connect_timeout=self._cfg.sqs_connect_timeout,
            read_timeout=self._cfg.sqs_read_timeout,
            retries={"max_attempts": 0},  # keine unendlichen SDK-Retries
        )
        return boto3.client(
            "sqs",
            endpoint_url=self._cfg.sqs_endpoint_url or None,
            region_name=self._cfg.aws_region,
            config=config,
        )

    # --- Poll-Loop ------------------------------------------------------------

    def _run(self) -> None:
        client = self._sqs_client()
        log.info("publisher started, polling outbox")  # keine URL/keine Secrets
        while not self._stop.is_set():
            try:
                rows = self._poll_once(client)
                POLLS_OK.inc()
            except Exception as exc:
                POLL_ERRORS.inc()
                log.warning("poll cycle failed: %s", type(exc).__name__)
                self._stop.wait(self._cfg.poll_interval_seconds)
                continue
            if not rows:
                self._stop.wait(self._cfg.poll_interval_seconds)

    def _poll_once(self, client) -> list[dict]:
        pool = self._ensure_pool()
        with pool.connection() as conn:
            rows = db.claim_batch(
                conn,
                batch_size=self._cfg.batch_size,
                lease_seconds=self._cfg.lease_seconds,
                owner=self._owner,
            )
            self._update_backlog(conn)
        if rows:
            CLAIMED_TOTAL.inc(len(rows))
            CLAIMED_ROWS.set(len(rows))
        for row in rows:
            if self._stop.is_set():  # nach Shutdown-Beginn keinen neuen Publish starten
                break
            self._publish_one(client, row)
        return rows

    def _update_backlog(self, conn) -> None:
        try:
            c = db.counts(conn)
            AVAILABLE_PENDING.set(c["available_pending"])
            CLAIMED_ROWS.set(c["claimed"])
            OLDEST_AVAILABLE_AGE.set(db.oldest_available_age_seconds(conn))
        except Exception as exc:  # pragma: no cover - reine Beobachtung
            log.debug("backlog metric update failed: %s", type(exc).__name__)

    def _publish_one(self, client, row: dict) -> None:
        eid = row["event_id"]
        try:
            body = build_body(row, max_body_bytes=self._cfg.max_body_bytes)
        except EnvelopeTooLarge:
            PUBLISH_ERRORS.inc()
            self._finalize_failure(row, "EnvelopeTooLarge")
            log.warning("event too large, deferred: event_id=%s", eid)
            return
        try:
            client.send_message(QueueUrl=self._cfg.sqs_queue_url, MessageBody=body.decode("utf-8"))
        except Exception as exc:
            # Antwort nicht bestaetigt -> Zeile bleibt pending (Backoff, Republish spaeter).
            PUBLISH_ERRORS.inc()
            self._finalize_failure(row, sanitize_error(exc))
            log.warning("publish failed: event_id=%s code=%s", eid, sanitize_error(exc))
            return
        PUBLISH_SUCCESS.inc()
        LAST_PUBLISH_TIMESTAMP.set(time.time())
        self._finalize_success(row)
        log.info("published: event_id=%s outcome=published", eid)

    def _finalize_success(self, row: dict) -> None:
        pool = self._ensure_pool()
        with pool.connection() as conn:
            n = db.finalize_success(
                conn, event_id=row["event_id"], owner=self._owner, claimed_at=row["claimed_at"]
            )
        if n == 0:
            FINALIZE_CONFLICTS.inc()
            log.warning("finalize-success conflict (stale claim): event_id=%s", row["event_id"])

    def _finalize_failure(self, row: dict, error_code: str) -> None:
        backoff = compute_backoff(
            row.get("attempt_count", 1), self._cfg.backoff_base_seconds, self._cfg.backoff_max_seconds
        )
        pool = self._ensure_pool()
        with pool.connection() as conn:
            n = db.finalize_failure(
                conn,
                event_id=row["event_id"],
                owner=self._owner,
                claimed_at=row["claimed_at"],
                backoff_seconds=backoff,
                error_code=error_code,
            )
        if n == 0:
            FINALIZE_CONFLICTS.inc()
            log.warning("finalize-failure conflict (stale claim): event_id=%s", row["event_id"])
        else:
            RETRIES.inc()


publisher = Publisher()
