"""Unit-Tests: Config/Validierung, Backoff, Fehler-Sanitizing, Envelope-Serialisierung."""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

import pytest
from app.config import ConfigError, Settings
from app.envelope import EnvelopeTooLarge, build_body
from app.publisher import compute_backoff, sanitize_error


# --- Config -----------------------------------------------------------------

def test_default_disabled(monkeypatch):
    monkeypatch.delenv("PUBLISHER_ENABLED", raising=False)
    assert Settings.from_env().enabled is False


def test_explicit_enable(monkeypatch):
    monkeypatch.setenv("PUBLISHER_ENABLED", "true")
    monkeypatch.setenv("DATABASE_URL", "host=x dbname=y user=inventory_publisher")
    monkeypatch.setenv("SQS_QUEUE_URL", "http://localhost/q")
    s = Settings.from_env()
    assert s.enabled is True
    s.validate_enabled()  # darf nicht werfen


def test_enabled_requires_db_and_queue(monkeypatch):
    monkeypatch.setenv("PUBLISHER_ENABLED", "true")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("SQS_QUEUE_URL", raising=False)
    s = Settings.from_env()
    with pytest.raises(ConfigError):
        s.validate_enabled()


@pytest.mark.parametrize("env,val", [
    ("PUBLISHER_BATCH_SIZE", "0"),
    ("PUBLISHER_LEASE_SECONDS", "0"),
    ("PUBLISHER_POLL_INTERVAL_SECONDS", "0"),
    ("PUBLISHER_BACKOFF_BASE_SECONDS", "0"),
    ("PUBLISHER_BACKOFF_MAX_SECONDS", "1"),  # < base default 5
    ("PUBLISHER_SQS_READ_TIMEOUT", "0"),
])
def test_value_validation_rejects_bad(monkeypatch, env, val):
    monkeypatch.setenv(env, val)
    with pytest.raises(ConfigError):
        Settings.from_env()


# --- Backoff ----------------------------------------------------------------

def test_backoff_progression():
    assert compute_backoff(1, 5, 300) == 5
    assert compute_backoff(2, 5, 300) == 10
    assert compute_backoff(3, 5, 300) == 20


def test_backoff_capped_at_max():
    assert compute_backoff(50, 5, 300) == 300


def test_backoff_overflow_safe_and_nonnegative():
    v = compute_backoff(10_000_000, 5, 300)
    assert v == 300 and v >= 0


def test_backoff_min_attempt():
    assert compute_backoff(0, 5, 300) == 5  # behandelt wie attempt 1


# --- Fehler-Sanitizing ------------------------------------------------------

def test_sanitize_error_is_typename_only_and_bounded():
    class SomeVeryLongExceptionNameThatExceedsTheSixtyFourCharacterLimitForSure(Exception):
        pass

    code = sanitize_error(SomeVeryLongExceptionNameThatExceedsTheSixtyFourCharacterLimitForSure("secret payload here"))
    assert "secret" not in code
    assert len(code) <= 64


# --- Envelope ---------------------------------------------------------------

def _row(eid=None, payload=None):
    return {
        "event_id": uuid.UUID(eid) if eid else uuid.uuid4(),
        "event_type": "inventory.movement.recorded",
        "schema_version": 1,
        "occurred_at": datetime(2026, 6, 17, 10, 0, tzinfo=timezone.utc),
        "source": "inventory-service",
        "payload": payload or {"movement_id": 1, "sku": "A", "quantity": 5, "warehouse": "W"},
    }


def test_envelope_keys_and_compact_json():
    body = build_body(_row(), max_body_bytes=16 * 1024)
    obj = json.loads(body)
    assert set(obj) == {"event_id", "event_type", "schema_version", "occurred_at", "source", "payload"}
    assert b" " not in body  # kompakt (keine Trennzeichen-Spaces)


def test_envelope_event_id_unchanged_no_new_uuid():
    eid = "00000000-0000-4000-8000-000000000001"
    obj = json.loads(build_body(_row(eid=eid), max_body_bytes=16 * 1024))
    assert obj["event_id"] == eid


def test_envelope_payload_unchanged():
    payload = {"movement_id": 7, "sku": "XYZ", "quantity": 3, "warehouse": "DC"}
    obj = json.loads(build_body(_row(payload=payload), max_body_bytes=16 * 1024))
    assert obj["payload"] == payload


def test_envelope_occurred_at_is_utc_rfc3339():
    naive = {"event_id": uuid.uuid4(), "event_type": "inventory.movement.recorded",
             "schema_version": 1, "occurred_at": datetime(2026, 6, 17, 12, 0),
             "source": "inventory-service",
             "payload": {"movement_id": 1, "sku": "A", "quantity": 1, "warehouse": "W"}}
    obj = json.loads(build_body(naive, max_body_bytes=16 * 1024))
    assert obj["occurred_at"].endswith("+00:00")


def test_envelope_size_limit_enforced():
    big = {"movement_id": 1, "sku": "A" * 50_000, "quantity": 1, "warehouse": "W"}
    with pytest.raises(EnvelopeTooLarge):
        build_body(_row(payload=big), max_body_bytes=16 * 1024)
