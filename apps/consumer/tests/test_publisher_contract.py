"""Echter Cross-Service-Contract-Test: Publisher-Body <-> Consumer-Validator.

Beweist end-to-end, dass der vom PUBLISHER mit seiner tatsaechlichen
Produktionsfunktion (apps/publisher/app/envelope.build_body) erzeugte Message Body
vom tatsaechlichen CONSUMER-Validator (app.envelope.validate) akzeptiert wird und
der Contract (event_id, payload, event_type, schema_version, source, occurred_at,
Fingerprint) exakt eingehalten ist.

Wichtig zur Isolation: Es gibt KEINE Runtime-Abhaengigkeit zwischen den Services.
Beide heissen ihr Paket `app`. Der Consumer-Validator wird ueber das normale
Consumer-`app`-Paket importiert; die Publisher-Envelope-Datei wird AUSSCHLIESSLICH
im Test ueber importlib unter einem EIGENEN Modulnamen aus ihrem Dateipfad geladen
(keine Paketkollision, kein Cross-Import in Produktionscode).
"""
from __future__ import annotations

import importlib.util
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
from app.envelope import MAX_BODY_BYTES, fingerprint, validate  # echter Consumer-Validator

_PUB_ENVELOPE_PATH = Path(__file__).resolve().parents[2] / "publisher" / "app" / "envelope.py"


def _load_publisher_envelope():
    """Laedt apps/publisher/app/envelope.py als eigenstaendiges Modul (kein app-Konflikt)."""
    spec = importlib.util.spec_from_file_location("publisher_envelope_under_test", _PUB_ENVELOPE_PATH)
    assert spec and spec.loader, f"konnte Publisher-Envelope nicht laden: {_PUB_ENVELOPE_PATH}"
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


pub_env = _load_publisher_envelope()


def _outbox_row(event_id: str | None = None, payload: dict | None = None,
                occurred_at: datetime | None = None) -> dict:
    """Repraesentative event_outbox-Zeile, wie sie der Publisher-Claim zurueckgibt
    (event_id als uuid.UUID, occurred_at tz-aware, payload als dict aus jsonb)."""
    return {
        "event_id": uuid.UUID(event_id) if event_id else uuid.UUID("00000000-0000-4000-8000-000000000001"),
        "event_type": "inventory.movement.recorded",
        "schema_version": 1,
        "occurred_at": occurred_at or datetime(2026, 6, 17, 10, 0, tzinfo=timezone.utc),
        "source": "inventory-service",
        "payload": payload or {"movement_id": 1, "sku": "ABC-1", "quantity": 5, "warehouse": "DC"},
    }


def test_module_isolation_loads_publisher_file_not_consumer_app():
    assert pub_env.__name__ == "publisher_envelope_under_test"
    assert str(pub_env.__file__).endswith("apps/publisher/app/envelope.py")
    assert hasattr(pub_env, "build_body")


def test_publisher_body_is_accepted_by_real_consumer_validator():
    row = _outbox_row()
    body = pub_env.build_body(row, max_body_bytes=MAX_BODY_BYTES)  # echte Publisher-Funktion
    env = validate(body)  # echter Consumer-Validator akzeptiert den Body
    # (3) event_id exakt wie in der Outbox
    assert env.event_id == row["event_id"]
    # (4) Payload exakt unveraendert
    assert env.payload == row["payload"]
    # (5) restlicher Contract identisch zum Consumer-Vertrag
    assert env.event_type == "inventory.movement.recorded"
    assert env.schema_version == 1
    assert env.source == "inventory-service"
    assert env.occurred_at == row["occurred_at"]  # gleicher Zeitpunkt, UTC


def test_no_new_uuid_generated_in_body():
    eid = "00000000-0000-4000-8000-0000000000ab"
    body = pub_env.build_body(_outbox_row(event_id=eid), max_body_bytes=MAX_BODY_BYTES)
    assert json.loads(body)["event_id"] == eid  # uebernommen, keine neue UUID


def test_repeated_body_yields_same_consumer_fingerprint():
    row = _outbox_row()
    b1 = pub_env.build_body(row, max_body_bytes=MAX_BODY_BYTES)
    b2 = pub_env.build_body(row, max_body_bytes=MAX_BODY_BYTES)
    assert fingerprint(validate(b1)) == fingerprint(validate(b2))


def test_naive_occurred_at_serialized_as_utc_and_accepted():
    naive = datetime(2026, 6, 17, 12, 0)  # ohne tzinfo
    body = pub_env.build_body(_outbox_row(occurred_at=naive), max_body_bytes=MAX_BODY_BYTES)
    env = validate(body)  # Consumer verlangt tz-aware -> Publisher muss UTC anhaengen
    assert env.occurred_at == naive.replace(tzinfo=timezone.utc)


def test_mutated_payload_same_event_id_detected_by_fingerprint():
    orig_env = validate(pub_env.build_body(_outbox_row(), max_body_bytes=MAX_BODY_BYTES))
    mutated_env = validate(pub_env.build_body(
        _outbox_row(payload={"movement_id": 1, "sku": "ABC-1", "quantity": 999, "warehouse": "DC"}),
        max_body_bytes=MAX_BODY_BYTES,
    ))
    # Gleiche event_id, aber abweichender Inhalt -> anderer Consumer-Fingerprint.
    assert mutated_env.event_id == orig_env.event_id
    assert fingerprint(mutated_env) != fingerprint(orig_env)


def test_publisher_and_consumer_share_same_contract_constants():
    # Drift einer der beiden lokalen Contract-Kopien wuerde hier auffallen.
    from app import envelope as consumer_env

    assert pub_env.EVENT_TYPE == consumer_env.EVENT_TYPE
    assert pub_env.SOURCE == consumer_env.SOURCE
    assert pub_env.SCHEMA_VERSION == consumer_env.SCHEMA_VERSION
