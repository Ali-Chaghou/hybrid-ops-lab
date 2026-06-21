"""SQS-Consumer-Runtime: Long-Poll -> idempotent verarbeiten -> nach Commit loeschen.

Diese Runtime besitzt KEINE eigene Idempotenzlogik. Sie verdrahtet ausschliesslich
die bereits vorhandenen, getesteten Bausteine:

    app.envelope.validate   -> strikte Envelope-/Contract-Validierung + Fingerprint
    app.store.process_event -> transactional Inbox + Projection in EINER Transaktion
    app.handler.handle_message -> Outcome-Disposition + Delete-Semantik

at-least-once bleibt erhalten: Eine Queue-Nachricht wird ausschliesslich dann
geloescht, wenn der Handler einen loeschbaren Outcome liefert (erfolgreicher
DB-Commit eines erstmaligen Effekts ODER ein idempotent erkanntes Duplikat).
Validierungsfehler, Integritaetskonflikte, DB-Fehler und die Lab-Failure-Injection
loeschen NICHT -> die Nachricht wird nach Visibility-Timeout erneut zugestellt und
beim naechsten Mal idempotent ueber die `event_id` behandelt. KEINE Exactly-once-
Zusage: das Fenster "DB-Commit ok, Queue-Delete schlaegt fehl" ist bewusst moeglich
und wird durch die Idempotenz sicher aufgefangen (siehe docs/idempotency.md).

Prozessmodell: GENAU EIN Poller-Thread pro Prozess (uvicorn ohne --workers/--reload,
ein FastAPI-Lifespan). DB-Zugriff laeuft ueber einen psycopg_pool-ConnectionPool,
der am Start geoeffnet und am Shutdown geschlossen wird. boto3 wird lazy importiert.
Logs enthalten nur sichere Identifikatoren (event_id/Outcome/MessageId) — nie
Payload, Secrets oder DSN.
"""
from __future__ import annotations

import logging
import threading
import time

import psycopg
from psycopg_pool import ConnectionPool

from app import store
from app.config import settings
from app.failure_injection import FailureInjector
from app.handler import handle_message
from app.metrics import (
    DLQ_DEPTH,
    LAST_POLL_TIMESTAMP,
    LAST_RECEIVE_COUNT,
    LAST_SUCCESS_TIMESTAMP,
    MESSAGE_PROCESSING_DURATION,
    QUEUE_ATTR_ERRORS,
    QUEUE_DEPTH,
    RECEIVE_ERRORS,
    REDELIVERIES,
    PrometheusMetrics,
)
from app.store import Outcome, check_schema_version

log = logging.getLogger("consumer")


def _safe_err(exc: BaseException) -> str:
    """Nur der Exception-Typ — verhindert, dass DSN/Host/Secret in Logs landen."""
    return type(exc).__name__


class SqsConsumer:
    """Ein Poller-Thread + ein DB-Connection-Pool pro Consumer-Prozess.

    Konfiguration kommt per Default aus `settings`; alle relevanten Werte sind ueber
    den Konstruktor injizierbar, damit die Verarbeitung ohne echte Queue/Umgebung
    integrationsgetestet werden kann.
    """

    def __init__(
        self,
        *,
        database_url: str | None = None,
        queue_url: str | None = None,
        dlq_queue_url: str | None = None,
        endpoint_url: str | None = None,
        aws_region: str | None = None,
        poll_wait_seconds: int | None = None,
        visibility_timeout_seconds: int | None = None,
        max_messages_per_poll: int | None = None,
        pool_min_size: int | None = None,
        pool_max_size: int | None = None,
        pool_timeout_seconds: float | None = None,
        injector: FailureInjector | None = None,
        metrics: PrometheusMetrics | None = None,
    ) -> None:
        self._database_url = database_url if database_url is not None else settings.database_url
        self._queue_url = queue_url if queue_url is not None else settings.sqs_queue_url
        self._dlq_queue_url = (
            dlq_queue_url if dlq_queue_url is not None else settings.sqs_dlq_queue_url
        )
        self._endpoint_url = endpoint_url if endpoint_url is not None else settings.sqs_endpoint_url
        self._aws_region = aws_region if aws_region is not None else settings.aws_region
        self._poll_wait = (
            poll_wait_seconds if poll_wait_seconds is not None else settings.poll_wait_seconds
        )
        self._visibility = (
            visibility_timeout_seconds
            if visibility_timeout_seconds is not None
            else settings.visibility_timeout_seconds
        )
        self._max_messages = (
            max_messages_per_poll
            if max_messages_per_poll is not None
            else settings.max_messages_per_poll
        )
        self._pool_timeout = (
            pool_timeout_seconds
            if pool_timeout_seconds is not None
            else settings.pool_timeout_seconds
        )
        self._injector = injector if injector is not None else FailureInjector.from_env()
        self._metrics = metrics if metrics is not None else PrometheusMetrics()

        # Botocore-Read-Timeout des Long-Poll-Receive. Der Shutdown-Join MUSS laenger
        # als dieser Maximalwert warten, sonst koennte er einen noch blockierenden
        # Receive vorzeitig aufgeben.
        self._read_timeout = self._poll_wait + 10
        # Backoff nach Receive-/DB-Fehlern (konfigurierbar fuer schnelle Tests).
        self._error_backoff = 2.0

        # Pool wird erst beim open_pool()/verify_schema() tatsaechlich geoeffnet.
        # check=check_connection verwirft defekte Verbindungen vor der Ausgabe.
        self._pool = ConnectionPool(
            conninfo=self._database_url,
            min_size=pool_min_size if pool_min_size is not None else settings.pool_min_size,
            max_size=pool_max_size if pool_max_size is not None else settings.pool_max_size,
            timeout=self._pool_timeout,
            open=False,
            check=ConnectionPool.check_connection,
            kwargs={"autocommit": False, "connect_timeout": 5},
        )

        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._last_poll_ok = 0.0
        self._last_success = 0.0
        self._schema_ok = False
        self._db_ok = False

    # --- Lifecycle ------------------------------------------------------------

    def open_pool(self) -> None:
        """Oeffnet den Pool (fail closed): wartet auf min_size Verbindungen.

        Wirft, wenn die DB nicht erreichbar ist (PoolTimeout/connect-Fehler).
        """
        self._pool.open(wait=True, timeout=self._pool_timeout)
        self._db_ok = True

    def verify_schema(self) -> None:
        """Startup-Gate (fail closed): Pool oeffnen, DB-Schema validieren.

        Wirft bei nicht erreichbarer DB oder falschem/fehlendem Schema
        (SchemaVersionError). Keine DDL, keine Migration, keine DSN/Secret-Ausgabe.
        """
        self.open_pool()
        with self._pool.connection() as conn:
            check_schema_version(conn)
        self._schema_ok = True
        log.info("consumer schema validated")

    def start(self) -> None:
        # Genau EIN Poller-Thread: ein erneuter start() ohne vorheriges stop() ist
        # ein No-op (schuetzt vor versehentlichem Doppelstart).
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                log.warning("consumer already running; ignoring duplicate start()")
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="sqs-consumer", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        # Laenger warten als der maximale Long-Poll-Read-Timeout, damit ein noch
        # blockierender Receive sauber zurueckkehrt.
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=self._read_timeout + 5)
            if thread.is_alive():
                # Fail closed: Pool NICHT schliessen, solange der Thread ihn evtl.
                # noch nutzt; _thread NICHT auf None setzen (kein Doppelstart daneben).
                log.error("consumer poller did not stop within timeout; pool left open")
                return
            self._thread = None
        try:
            self._pool.close()
        except Exception:  # pragma: no cover - defensives Close
            pass

    # --- Health / Readiness ---------------------------------------------------

    @property
    def healthy(self) -> bool:
        """Liveness: reine Prozess-/Loop-Lebendigkeit (kein DB-/Queue-Urteil)."""
        return self._thread is not None and self._thread.is_alive()

    @property
    def ready(self) -> bool:
        """Readiness: Schema validiert, Poll frisch UND DB ueber den Pool erreichbar."""
        if not self._schema_ok:
            return False
        if not self._poll_fresh():
            return False
        return self._db_ping()

    def _poll_fresh(self) -> bool:
        # Ein erfolgreicher (auch leerer) Long-Poll haelt die Freshness aufrecht.
        return (time.time() - self._last_poll_ok) < (self._poll_wait + 30)

    def _db_ping(self) -> bool:
        try:
            with self._pool.connection() as c:
                c.execute("SELECT 1")
            self._db_ok = True
            return True
        except Exception as exc:
            self._db_ok = False
            log.warning("db ping failed: %s", _safe_err(exc))
            return False

    def _ready_to_receive(self) -> bool:
        """Gate vor jedem Receive: nach einem DB-Fehler (`_db_ok=False`) wird erst
        wieder gepollt, sobald ein `_db_ping()` erfolgreich war."""
        return self._db_ok or self._db_ping()

    # --- Poll-Loop ------------------------------------------------------------

    def _client(self):
        import boto3  # lazy, wie in der inventory-App
        from botocore.config import Config

        config = Config(
            # read_timeout MUSS groesser als poll_wait_seconds sein, sonst bricht
            # jeder Long-Poll-Receive ab.
            connect_timeout=5,
            read_timeout=self._read_timeout,
            retries={"max_attempts": 0},
        )
        return boto3.client(
            "sqs",
            endpoint_url=self._endpoint_url or None,
            region_name=self._aws_region,
            config=config,
        )

    def _attrs_client(self):
        """Eigener SQS-Client NUR fuer GetQueueAttributes (Tiefen) — mit KURZEN
        Timeouts, damit ein langsames/nicht erreichbares ElasticMQ weder den Poll
        blockiert noch laenger haengt. Entkoppelt vom Long-Poll-Client."""
        import boto3  # lazy, wie in der inventory-App
        from botocore.config import Config

        config = Config(
            connect_timeout=2,
            read_timeout=3,
            retries={"max_attempts": 0},
        )
        return boto3.client(
            "sqs",
            endpoint_url=self._endpoint_url or None,
            region_name=self._aws_region,
            config=config,
        )

    def _run(self) -> None:
        client = self._client()
        attrs_client = self._attrs_client()
        log.info("consumer started, polling queue")  # KEINE URL/keine Secrets
        while not self._stop.is_set():
            # Readiness-Gate: nach einem DB-Fehler erst wieder pollen, wenn ein
            # _db_ping() erfolgreich war (Schema wurde beim Start verifiziert).
            if not self._ready_to_receive():
                self._stop.wait(self._error_backoff)
                continue
            try:
                resp = client.receive_message(
                    QueueUrl=self._queue_url,
                    MaxNumberOfMessages=self._max_messages,
                    WaitTimeSeconds=self._poll_wait,
                    VisibilityTimeout=self._visibility,
                    # System-Attribut fuer Observability (ApproximateReceiveCount).
                    # NUR Beobachtung — die DLQ-Verschiebung entscheidet die native
                    # SQS/ElasticMQ-Redrive-Policy, nicht der Client. AttributeNames
                    # ist mit ElasticMQ kompatibel.
                    AttributeNames=["ApproximateReceiveCount"],
                )
                # Auch ein LEERER Poll ist ein erfolgreicher Poll -> Freshness halten.
                self._mark_poll_ok()
                # Tiefen ueber den separaten Short-Timeout-Client; Fehler hier duerfen
                # den Loop NICHT stoppen (nur Fehler-Metrik, letzte Gauge-Werte bleiben).
                self._update_depth(attrs_client)
            except Exception as exc:
                RECEIVE_ERRORS.inc()
                log.warning("receive failed: %s", _safe_err(exc))
                self._stop.wait(self._error_backoff)
                continue

            for message in resp.get("Messages", []):
                if self._stop.is_set():  # nach Shutdown-Beginn nichts mehr starten
                    break
                self._process_message(client, message)

    def _mark_poll_ok(self) -> None:
        self._last_poll_ok = time.time()
        LAST_POLL_TIMESTAMP.set(self._last_poll_ok)

    # --- Per-Message-Verarbeitung (an den bestehenden Handler delegiert) -------

    def _process_message(self, client, message: dict) -> Outcome:
        raw = self._raw_body(message)
        msg_id = message.get("MessageId", "?")
        receive_count = self._receive_count(message)
        if receive_count is not None:
            LAST_RECEIVE_COUNT.set(receive_count)
            if receive_count > 1:
                REDELIVERIES.inc()
        start = time.perf_counter()

        # Verbindung aus dem Pool. getconn mit check liefert nur lebende Connections;
        # eine nicht erreichbare DB fuehrt zu DB_FAILURE (kein Effekt, kein Delete).
        try:
            conn = self._pool.getconn(timeout=self._pool_timeout)
        except Exception as exc:
            self._db_ok = False
            self._metrics.database_failure()
            log.warning("db unavailable, message left for redelivery: msg_id=%s err=%s",
                        msg_id, _safe_err(exc))
            MESSAGE_PROCESSING_DURATION.observe(time.perf_counter() - start)
            return Outcome.DB_FAILURE

        try:
            outcome = handle_message(
                raw,
                conn,
                delete_fn=lambda _env: self._delete(client, message),
                injector=self._injector,
                metrics=self._metrics,
            )
            # Der Handler faengt psycopg.Error ab und liefert DB_FAILURE -> dann gilt
            # die DB als nicht gesund und der Loop pollt erst nach erfolgreichem
            # _db_ping() wieder (siehe _ready_to_receive).
            self._db_ok = outcome is not Outcome.DB_FAILURE
        finally:
            # store.process_event committet/rollt selbst zurueck; defensiv saeubern
            # und die Verbindung in den Pool zuruecklegen (defekte werden verworfen).
            try:
                conn.rollback()
            except Exception:  # pragma: no cover
                pass
            try:
                self._pool.putconn(conn)
            except Exception:  # pragma: no cover
                pass
            MESSAGE_PROCESSING_DURATION.observe(time.perf_counter() - start)

        if store.should_delete(outcome):
            self._last_success = time.time()
            LAST_SUCCESS_TIMESTAMP.set(self._last_success)
        # Strukturiertes Log: nur sichere Identifikatoren, KEINE Payload/Secrets.
        log.info("message handled: msg_id=%s outcome=%s receive_count=%s",
                 msg_id, outcome.value, receive_count if receive_count is not None else "?")
        return outcome

    @staticmethod
    def _receive_count(message: dict) -> int | None:
        """Robustes Parsen von ApproximateReceiveCount; None, wenn nicht vorhanden."""
        raw = (message.get("Attributes") or {}).get("ApproximateReceiveCount")
        if raw is None:
            return None
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None

    def _delete(self, client, message: dict) -> None:
        client.delete_message(
            QueueUrl=self._queue_url,
            ReceiptHandle=message["ReceiptHandle"],
        )

    @staticmethod
    def _raw_body(message: dict) -> bytes:
        body = message.get("Body", "")
        if isinstance(body, (bytes, bytearray)):
            return bytes(body)
        return str(body).encode("utf-8")

    def _update_depth(self, client) -> None:
        self._set_depth(client, self._queue_url, QUEUE_DEPTH)
        # DLQ-Tiefe nur, wenn konfiguriert. Der Consumer liest/loescht NICHT aus der
        # DLQ — nur GetQueueAttributes fuer die Tiefen-Metrik.
        if self._dlq_queue_url:
            self._set_depth(client, self._dlq_queue_url, DLQ_DEPTH)

    @staticmethod
    def _set_depth(client, queue_url: str, gauge) -> None:
        try:
            attrs = client.get_queue_attributes(
                QueueUrl=queue_url,
                AttributeNames=["ApproximateNumberOfMessages"],
            )
            gauge.set(int(attrs["Attributes"]["ApproximateNumberOfMessages"]))
        except Exception as exc:
            # Fehler-Metrik erhoehen, letzten bekannten Gauge-Wert BEIBEHALTEN
            # (nicht auf 0 setzen) und NICHT propagieren.
            QUEUE_ATTR_ERRORS.inc()
            log.debug("queue depth update failed: %s", _safe_err(exc))


consumer = SqsConsumer()
