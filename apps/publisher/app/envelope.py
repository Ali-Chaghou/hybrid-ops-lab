"""Envelope-Serialisierung des Publishers (Envelope v1).

Reine Funktion: baut den Queue-Nachrichten-Body aus einer bereits geclaimten
event_outbox-Zeile. KEIN Import aus apps/inventory oder apps/consumer (Service-
Isolation); der Contract (Envelope v1) ist lokal gehalten und identisch zum
Producer (apps/inventory/app/outbox.py) und Consumer-Validator
(apps/consumer/app/envelope.py).

Strikt:
- `event_id` wird aus der Outbox UEBERNOMMEN (keine neue UUID);
- Payload wird UNVERAENDERT uebernommen (keine Neuberechnung aus stock_movements);
- `occurred_at` kanonisch als timezone-aware RFC3339/UTC;
- kompaktes, valides JSON; Groesse VOR dem Publish begrenzt.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

# Feste, an Schema-Version 1 gebundene Auspraegungen (lokal, kein Cross-Import).
EVENT_TYPE = "inventory.movement.recorded"
SOURCE = "inventory-service"
SCHEMA_VERSION = 1


class EnvelopeTooLarge(ValueError):
    """Serialisierter Body ueberschreitet das Groessenlimit."""


def _occurred_at_iso(value: datetime) -> str:
    if value.tzinfo is None:  # Sicherheitsnetz; TIMESTAMPTZ ist tz-aware.
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def build_body(row: dict, *, max_body_bytes: int) -> bytes:
    """Serialisiert eine geclaimte Outbox-Zeile zum Queue-Body (Envelope v1).

    `row` enthaelt mindestens: event_id, event_type, schema_version, occurred_at,
    source, payload. Wirft EnvelopeTooLarge, wenn der Body das Limit ueberschreitet.
    """
    envelope = {
        "event_id": str(row["event_id"]),       # uebernommen, keine neue UUID
        "event_type": row["event_type"],
        "schema_version": row["schema_version"],
        "occurred_at": _occurred_at_iso(row["occurred_at"]),
        "source": row["source"],
        "payload": row["payload"],               # unveraendert (jsonb -> dict)
    }
    body = json.dumps(envelope, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    if len(body) > max_body_bytes:
        raise EnvelopeTooLarge(f"{len(body)}>{max_body_bytes}")
    return body
