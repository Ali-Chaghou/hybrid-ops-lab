"""Inventory-eigenes Outbox-Modul (Phase 2B).

Reine Event-Erzeugung aus einer bereits persistierten stock_movements-Zeile.
KEINE DB-, Netz-, Queue- oder Env-Abhaengigkeit und bewusst KEIN Import aus
apps/consumer: der Event-Contract (Envelope v1) ist hier absichtlich lokal
gehalten, damit die Service-Isolation erhalten bleibt. Der Consumer validiert
denselben Contract auf seiner Seite unabhaengig.

Envelope v1:
    {
      "event_id":       "<kanonische lowercase UUID>",
      "event_type":     "inventory.movement.recorded",
      "schema_version": 1,
      "occurred_at":    "<timezone-aware RFC3339 UTC>",
      "source":         "inventory-service",
      "payload": { "movement_id": <bigint>, "sku": <str>, "quantity": <int>, "warehouse": <str> }
    }
"""
from __future__ import annotations

from datetime import datetime, timezone

# Feste, an Schema-Version 1 gebundene Auspraegungen (lokal, kein Consumer-Import).
EVENT_TYPE = "inventory.movement.recorded"
SOURCE = "inventory-service"
SCHEMA_VERSION = 1


def build_event(movement: dict) -> dict:
    """Erzeugt das Outbox-Envelope (Schema v1) aus einer persistierten Movement-Zeile.

    Reine Funktion: erwartet die Felder id, event_id, created_at, sku, quantity und
    warehouse der gerade eingefuegten Zeile. event_id wird als kanonische lowercase-
    UUID serialisiert, occurred_at als timezone-aware RFC3339 in UTC (== created_at).
    """
    occurred_at: datetime = movement["created_at"]
    if occurred_at.tzinfo is None:  # Sicherheitsnetz; TIMESTAMPTZ ist tz-aware.
        occurred_at = occurred_at.replace(tzinfo=timezone.utc)
    occurred_at = occurred_at.astimezone(timezone.utc)
    return {
        "event_id": str(movement["event_id"]),  # UUID -> kanonisch (lowercase, hyphenated)
        "event_type": EVENT_TYPE,
        "schema_version": SCHEMA_VERSION,
        "occurred_at": occurred_at.isoformat(),
        "source": SOURCE,
        "payload": {
            "movement_id": movement["id"],
            "sku": movement["sku"],
            "quantity": movement["quantity"],
            "warehouse": movement["warehouse"],
        },
    }
