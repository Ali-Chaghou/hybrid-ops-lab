"""Prometheus-Metriken. /metrics liefert die Exposition."""
from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

MESSAGES_CONSUMED = Counter(
    "consumer_messages_consumed_total",
    "Verarbeitete Nachrichten aus der Queue.",
    ["result"],  # success | error
)

MESSAGE_PROCESSING_DURATION = Histogram(
    "consumer_message_processing_duration_seconds",
    "Dauer der Verarbeitung einer einzelnen Nachricht.",
)

RECEIVE_ERRORS = Counter(
    "consumer_receive_errors_total",
    "Fehlgeschlagene Receive-Aufrufe gegen die Queue.",
)

QUEUE_DEPTH = Gauge(
    "consumer_queue_depth_approximate",
    "Ungefaehre Anzahl sichtbarer Nachrichten in der Queue.",
)
