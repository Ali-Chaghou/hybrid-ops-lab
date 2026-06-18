"""Event-Contract: strikte Validierung (Unit, ohne DB)."""
from __future__ import annotations

import json

import pytest
from app.envelope import MAX_BODY_BYTES, EnvelopeError, validate

import helpers


def test_valid_event_parses():
    env = validate(helpers.body())
    assert str(env.event_id) == helpers.VALID["event_id"]
    assert env.event_type == "inventory.movement.recorded"
    assert env.schema_version == 1
    assert env.source == "inventory-service"
    assert env.movement_id == 123
    assert env.occurred_at.utcoffset().total_seconds() == 0  # auf UTC normalisiert


def _reason(raw: bytes) -> str:
    with pytest.raises(EnvelopeError) as ei:
        validate(raw)
    return ei.value.reason


def test_missing_event_id():
    obj = json.loads(helpers.body()); del obj["event_id"]
    assert _reason(json.dumps(obj).encode()) == "missing_field"


def test_invalid_uuid():
    assert _reason(helpers.body(event_id="not-a-uuid")) == "bad_event_id"


def test_non_canonical_uuid_rejected():
    # UUID mit Hex-Buchstaben in Grossschreibung ist nicht-kanonisch.
    assert _reason(helpers.body(event_id="ABCDEF00-000B-4000-8000-00000000000C")) == "bad_event_id"


def test_unknown_schema_version():
    assert _reason(helpers.body(schema_version=2)) == "bad_schema_version"


def test_schema_version_bool_rejected():
    # JSON true -> Python bool; darf NICHT als Integer 1 durchgehen.
    raw = helpers.body().replace(b'"schema_version": 1', b'"schema_version": true')
    assert _reason(raw) == "bad_schema_version"


def test_missing_event_type():
    obj = json.loads(helpers.body()); del obj["event_type"]
    assert _reason(json.dumps(obj).encode()) == "missing_field"


def test_wrong_event_type():
    assert _reason(helpers.body(event_type="something.else")) == "bad_event_type"


def test_wrong_source():
    assert _reason(helpers.body(source="evil-service")) == "bad_source"


def test_naive_timestamp_rejected():
    assert _reason(helpers.body(occurred_at="2026-06-17T10:00:00")) == "bad_occurred_at"


def test_non_rfc3339_timestamp_rejected():
    assert _reason(helpers.body(occurred_at="17.06.2026 10:00")) == "bad_occurred_at"


def test_unknown_top_level_field():
    assert _reason(helpers.body(extra="x")) == "unknown_field"


def test_unknown_payload_field():
    assert _reason(helpers.body_with_payload(extra="x")) == "unknown_field"


def test_payload_not_object():
    assert _reason(helpers.body(payload=[1, 2, 3])) == "payload_not_object"


def test_top_level_not_object():
    assert _reason(b'[1,2,3]') == "not_object"


def test_duplicate_json_keys_rejected():
    # zwei "quantity"-Keys -> kein "letzter gewinnt".
    raw = b'{"event_id":"00000000-0000-4000-8000-000000000001","event_type":"inventory.movement.recorded","schema_version":1,"occurred_at":"2026-06-17T10:00:00+00:00","source":"inventory-service","payload":{"movement_id":1,"sku":"A","quantity":5,"quantity":6,"warehouse":"W"}}'
    assert _reason(raw) == "duplicate_key"


def test_quantity_bool_rejected():
    raw = helpers.body().replace(b'"quantity": 5', b'"quantity": true')
    assert _reason(raw) == "bad_quantity"


def test_movement_id_out_of_bigint_range():
    assert _reason(helpers.body_with_payload(movement_id=2**63)) == "bad_movement_id"


def test_sku_too_long():
    assert _reason(helpers.body_with_payload(sku="x" * 65)) == "bad_sku"


def test_body_exactly_at_limit_passes_size_gate():
    # Genau MAX_BODY_BYTES: das Groessenlimit greift NICHT mehr; die Ablehnung
    # kommt dann aus dem JSON-Parsing (Beweis: Gate bei == Limit offen).
    raw = b" " * MAX_BODY_BYTES
    assert len(raw) == MAX_BODY_BYTES
    assert _reason(raw) == "not_json"


def test_body_over_limit_rejected():
    big = b"x" * (MAX_BODY_BYTES + 1)
    assert _reason(big) == "too_large"


def test_non_finite_rejected():
    raw = helpers.body().replace(b'"quantity": 5', b'"quantity": NaN')
    assert _reason(raw) in {"non_finite", "bad_quantity"}
