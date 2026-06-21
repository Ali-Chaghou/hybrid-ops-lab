"""Prometheus-Metriken. /metrics liefert die Exposition."""
from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

MESSAGE_PROCESSING_DURATION = Histogram(
    "consumer_message_processing_duration_seconds",
    "Dauer der Verarbeitung einer einzelnen Nachricht (Validierung + DB-Transaktion + Delete).",
)

RECEIVE_ERRORS = Counter(
    "consumer_receive_errors_total",
    "Fehlgeschlagene Receive-Aufrufe gegen die Queue.",
)

# Fehler bei GetQueueAttributes (Tiefen-Update). Entkoppelt vom Receive-Pfad; ein
# Fehler haelt die letzten bekannten Tiefen-Gauges, bricht aber nichts ab.
QUEUE_ATTR_ERRORS = Counter(
    "consumer_queue_attr_errors_total",
    "Fehlgeschlagene GetQueueAttributes-Aufrufe (Tiefen-Update).",
)

QUEUE_DEPTH = Gauge(
    "consumer_queue_depth_approximate",
    "Ungefaehre Anzahl sichtbarer Nachrichten in der Main-Queue.",
)

# DLQ-Tiefe (ueber GetQueueAttributes auf der DLQ; nur wenn SQS_DLQ_QUEUE_URL gesetzt).
DLQ_DEPTH = Gauge(
    "consumer_dlq_depth_approximate",
    "Ungefaehre Anzahl sichtbarer Nachrichten in der Dead-Letter-Queue.",
)

# Letzter beobachteter ApproximateReceiveCount (niedrig-kardinal: KEIN Label).
LAST_RECEIVE_COUNT = Gauge(
    "consumer_last_receive_count",
    "ApproximateReceiveCount der zuletzt verarbeiteten Nachricht (0 wenn unbekannt).",
)

# Liveness/Readiness als Gauges, zur Scrape-Zeit gesetzt (1 = ja, 0 = nein).
CONSUMER_LIVE = Gauge("consumer_live", "1 wenn der Poller-Thread laeuft, sonst 0.")
CONSUMER_READY = Gauge("consumer_ready", "1 wenn Schema/DB/Poll-Freshness ok, sonst 0.")

# Zeitpunkt der letzten erfolgreich abgeschlossenen Verarbeitung (applied ODER
# idempotent erkanntes Duplikat) — fuer eine "Consumer macht Fortschritt"-Sicht.
LAST_SUCCESS_TIMESTAMP = Gauge(
    "consumer_last_success_timestamp_seconds",
    "Unix-Zeit der letzten erfolgreich abgeschlossenen (loeschbaren) Verarbeitung.",
)

# Zeitpunkt des letzten erfolgreichen Long-Polls (auch wenn er leer war) — Basis
# fuer Poll-Freshness/Readiness und zum Erkennen haengender Receive-Aufrufe.
LAST_POLL_TIMESTAMP = Gauge(
    "consumer_last_poll_timestamp_seconds",
    "Unix-Zeit des letzten erfolgreichen Receive-Aufrufs (leerer Poll zaehlt).",
)

# --- Idempotenz-Consumer (Phase 1): eindeutige, niedrig-kardinale Metriken -----
# Bewusst KEINE event_id / movement_id / correlation_id / Payload als Label.
EVENTS_RECEIVED = Counter("consumer_events_received_total", "Empfangene Events (vor Verarbeitung).")
EVENTS_APPLIED = Counter("consumer_events_applied_total", "Events, die den fachlichen Effekt erstmalig erzeugten.")
TRANSPORT_DUPLICATES = Counter("consumer_transport_duplicates_total", "Gleiche event_id, gleicher Fingerprint (Transport-Redelivery).")
BUSINESS_DUPLICATES = Counter("consumer_business_duplicates_total", "Neue event_id fuer bereits angewandtes Movement, identische Fachdaten.")
INTEGRITY_CONFLICTS = Counter(
    "consumer_integrity_conflicts_total",
    "Integritaetskonflikte.",
    ["kind"],  # event_id | movement  (fest, begrenzt)
)
VALIDATION_FAILURES = Counter(
    "consumer_validation_failures_total",
    "Abgelehnte Events nach strikter Envelope-Validierung.",
    ["reason"],  # fester Satz aus EnvelopeError.reason
)
DATABASE_FAILURES = Counter("consumer_database_failures_total", "DB-Fehler waehrend der Verarbeitung (kein Effekt, kein Delete).")
MESSAGE_DELETE_FAILURES = Counter("consumer_message_delete_failures_total", "Delete-Fehler NACH erfolgreichem Commit (Redelivery moeglich).")
FAILURE_INJECTIONS = Counter("consumer_failure_injections_total", "Ausgeloeste Lab-Failure-Injections (nach Commit, vor Delete).")
# Nachrichten, die mehr als einmal zugestellt wurden (ApproximateReceiveCount > 1).
# Beobachtung, KEINE Routing-Entscheidung — die DLQ-Verschiebung macht SQS/ElasticMQ.
REDELIVERIES = Counter("consumer_redeliveries_total", "Empfangene Nachrichten mit ApproximateReceiveCount > 1.")


class PrometheusMetrics:
    """Adapter: entkoppelt Handler-Logik von prometheus_client (testbar via Fake)."""

    def received(self) -> None: EVENTS_RECEIVED.inc()
    def applied(self) -> None: EVENTS_APPLIED.inc()
    def transport_duplicate(self) -> None: TRANSPORT_DUPLICATES.inc()
    def business_duplicate(self) -> None: BUSINESS_DUPLICATES.inc()
    def integrity_conflict(self, kind: str) -> None: INTEGRITY_CONFLICTS.labels(kind=kind).inc()
    def validation_failure(self, reason: str) -> None: VALIDATION_FAILURES.labels(reason=reason).inc()
    def database_failure(self) -> None: DATABASE_FAILURES.inc()
    def delete_failure(self) -> None: MESSAGE_DELETE_FAILURES.inc()
    def failure_injection(self) -> None: FAILURE_INJECTIONS.inc()
