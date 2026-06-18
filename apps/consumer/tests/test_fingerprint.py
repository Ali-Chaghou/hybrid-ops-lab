"""Fingerprint: stabil, key-/whitespace-/UTC-invariant, event_id-unabhaengig (Unit)."""
from __future__ import annotations

import json

from app.envelope import fingerprint, validate

import helpers


def fp(raw: bytes) -> str:
    return fingerprint(validate(raw))


def test_fingerprint_is_64_hex_lowercase():
    h = fp(helpers.body())
    assert len(h) == 64 and h == h.lower()
    int(h, 16)  # reine Hex


def test_independent_of_key_order():
    a = helpers.body()
    obj = json.loads(a)
    reordered = {
        "payload": {"warehouse": obj["payload"]["warehouse"], "quantity": obj["payload"]["quantity"],
                     "sku": obj["payload"]["sku"], "movement_id": obj["payload"]["movement_id"]},
        "source": obj["source"], "occurred_at": obj["occurred_at"], "schema_version": obj["schema_version"],
        "event_type": obj["event_type"], "event_id": obj["event_id"],
    }
    assert fp(a) == fp(json.dumps(reordered).encode())


def test_independent_of_whitespace():
    compact = helpers.body()
    spaced = json.dumps(json.loads(compact), indent=4).encode()
    assert fp(compact) == fp(spaced)


def test_equivalent_utc_spellings_same_fingerprint():
    z = helpers.body(occurred_at="2026-06-17T10:00:00Z")
    plus = helpers.body(occurred_at="2026-06-17T10:00:00+00:00")
    other_offset = helpers.body(occurred_at="2026-06-17T12:00:00+02:00")  # selber Zeitpunkt
    assert fp(z) == fp(plus) == fp(other_offset)


def test_different_instant_changes_fingerprint():
    a = helpers.body(occurred_at="2026-06-17T10:00:00+00:00")
    b = helpers.body(occurred_at="2026-06-17T10:00:01+00:00")
    assert fp(a) != fp(b)


def test_event_id_not_part_of_fingerprint():
    a = helpers.body(event_id=helpers.new_uuid())
    b = helpers.body(event_id=helpers.new_uuid())
    assert fp(a) == fp(b)


def test_payload_change_changes_fingerprint():
    a = helpers.body()
    b = helpers.body_with_payload(quantity=999)
    assert fp(a) != fp(b)
