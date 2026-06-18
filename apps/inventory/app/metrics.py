"""Prometheus-Metriken. /metrics liefert die Exposition."""
from __future__ import annotations

from prometheus_client import Counter, Histogram

MOVEMENTS_CREATED = Counter(
    "inventory_movements_created_total",
    "Anzahl persistierter Lagerbewegungen (erst nach erfolgreichem Commit).",
)

OUTBOX_EVENTS_WRITTEN = Counter(
    "inventory_outbox_events_written_total",
    "In derselben Transaktion wie das Movement geschriebene Outbox-Events "
    "(erst nach erfolgreichem Commit).",
)

MOVEMENT_TX_FAILURES = Counter(
    "inventory_movement_tx_failures_total",
    "Fehlgeschlagene und vollstaendig zurueckgerollte Movement-/Outbox-Transaktionen.",
)

EVENTS_PUBLISHED = Counter(
    "inventory_events_published_total",
    "Publish-Versuche an die Queue.",
    ["result"],  # success | error | skipped
)

EVENT_PUBLISH_DURATION = Histogram(
    "inventory_event_publish_duration_seconds",
    "Dauer eines Publish-Versuchs an die Queue.",
)

REQUEST_DURATION = Histogram(
    "inventory_http_request_duration_seconds",
    "Dauer der HTTP-Requests.",
    ["method", "path", "status"],
)
