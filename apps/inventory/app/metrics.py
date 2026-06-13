"""Prometheus-Metriken. /metrics liefert die Exposition."""
from __future__ import annotations

from prometheus_client import Counter, Histogram

MOVEMENTS_CREATED = Counter(
    "inventory_movements_created_total",
    "Anzahl persistierter Lagerbewegungen.",
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
