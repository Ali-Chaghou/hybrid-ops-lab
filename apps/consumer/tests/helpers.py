"""Test-Helfer: gueltige Envelope-Bodies bauen."""
from __future__ import annotations

import json
import uuid

VALID = {
    "event_id": "00000000-0000-4000-8000-000000000001",
    "event_type": "inventory.movement.recorded",
    "schema_version": 1,
    "occurred_at": "2026-06-17T10:00:00+00:00",
    "source": "inventory-service",
    "payload": {"movement_id": 123, "sku": "ABC-123", "quantity": 5, "warehouse": "WH-01"},
}


def new_uuid() -> str:
    return str(uuid.uuid4())


def body(**top_overrides) -> bytes:
    """Serialisiert ein gueltiges Event; top-level Felder via kwargs ueberschreibbar.
    payload kann komplett ersetzt werden (payload=...)."""
    obj = json.loads(json.dumps(VALID))
    obj.update(top_overrides)
    return json.dumps(obj).encode("utf-8")


def body_with_payload(**payload_overrides) -> bytes:
    obj = json.loads(json.dumps(VALID))
    obj["payload"].update(payload_overrides)
    return json.dumps(obj).encode("utf-8")


def event(event_id: str | None = None, occurred_at: str | None = None, **payload_overrides) -> bytes:
    """Vollstaendiger Builder: per Default FRISCHE event_id (fuer Mehr-Event-Tests)."""
    obj = json.loads(json.dumps(VALID))
    obj["event_id"] = event_id or new_uuid()
    if occurred_at is not None:
        obj["occurred_at"] = occurred_at
    obj["payload"].update(payload_overrides)
    return json.dumps(obj).encode("utf-8")
