"""DB-Invarianten: die Datenbank selbst schuetzt die kanonische Business-Duplicate-
Referenz (zusammengesetzter FK) und die applied/canonical-Kopplung (CHECK).
Direkte SQL-Manipulationsversuche muessen scheitern."""
from __future__ import annotations

import psycopg
import pytest
from psycopg import errors
from app.envelope import validate
from app.store import process_event

import helpers

_FP = "b" * 64
_INBOX = (
    "INSERT INTO event_inbox "
    "(event_id, event_type, source, source_movement_id, schema_version, fingerprint, disposition, canonical_event_id) "
    "VALUES (%s, 'inventory.movement.recorded', %s, %s, 1, %s, %s, %s)"
)


def apply(app_dsn: str, movement_id: int) -> str:
    eid = helpers.new_uuid()
    with psycopg.connect(app_dsn, autocommit=False) as cn:
        process_event(cn, validate(helpers.event(event_id=eid, movement_id=movement_id)))
    return eid


def test_business_duplicate_to_nonexistent_event_fails(consumer_db):
    apply(consumer_db["app"], 1)  # Projektion (inventory-service, 1, E1)
    with psycopg.connect(consumer_db["admin"], autocommit=True) as c:
        with pytest.raises(errors.ForeignKeyViolation):
            c.execute(_INBOX, (helpers.new_uuid(), "inventory-service", 1, _FP,
                               "business_duplicate", helpers.new_uuid()))  # canonical existiert nicht


def test_business_duplicate_to_event_without_projection_fails(consumer_db):
    # Lone applied-Inbox-Zeile OHNE Projektion (Movement 99) direkt anlegen.
    e_noproj = helpers.new_uuid()
    with psycopg.connect(consumer_db["admin"], autocommit=True) as c:
        c.execute(_INBOX, (e_noproj, "inventory-service", 99, _FP, "applied", None))
        with pytest.raises(errors.ForeignKeyViolation):
            c.execute(_INBOX, (helpers.new_uuid(), "inventory-service", 99, _FP,
                               "business_duplicate", e_noproj))  # kein Projektion (99, e_noproj)


def test_business_duplicate_to_other_movement_fails(consumer_db):
    e1 = apply(consumer_db["app"], 1)  # Projektion (inventory-service, 1, e1)
    with psycopg.connect(consumer_db["admin"], autocommit=True) as c:
        with pytest.raises(errors.ForeignKeyViolation):
            c.execute(_INBOX, (helpers.new_uuid(), "inventory-service", 2, _FP,
                               "business_duplicate", e1))  # Movement 2 != Projektion-Movement 1


def test_business_duplicate_to_other_source_fails(consumer_db):
    # Projektion fuer movement 5 existiert NUR unter einer fremden Source.
    e_other = helpers.new_uuid()
    with psycopg.connect(consumer_db["admin"], autocommit=True) as c:
        c.execute(_INBOX, (e_other, "inventory-service", 5, _FP, "applied", None))  # Inbox-Event existiert
        c.execute(
            "INSERT INTO movement_projection (source, source_movement_id, source_event_id, sku, quantity, warehouse, occurred_at) "
            "VALUES ('other-source', 5, %s, 'x', 1, 'w', now())",
            (e_other,),
        )
        # business_duplicate unter inventory-service kann diese fremd-source Projektion nicht referenzieren.
        with pytest.raises(errors.ForeignKeyViolation):
            c.execute(_INBOX, (helpers.new_uuid(), "inventory-service", 5, _FP,
                               "business_duplicate", e_other))


def test_valid_business_duplicate_reference_works(consumer_db):
    e1 = apply(consumer_db["app"], 7)  # Projektion (inventory-service, 7, e1)
    with psycopg.connect(consumer_db["admin"], autocommit=True) as c:
        c.execute(_INBOX, (helpers.new_uuid(), "inventory-service", 7, _FP,
                           "business_duplicate", e1))  # korrekte Referenz -> ok
        n = c.execute("SELECT count(*) FROM event_inbox WHERE disposition='business_duplicate'").fetchone()[0]
    assert n == 1


def test_applied_with_canonical_fails_check(consumer_db):
    with psycopg.connect(consumer_db["admin"], autocommit=True) as c:
        with pytest.raises(errors.CheckViolation):
            c.execute(_INBOX, (helpers.new_uuid(), "inventory-service", 1, _FP,
                               "applied", helpers.new_uuid()))  # applied + canonical -> CHECK


def test_business_duplicate_without_canonical_fails_check(consumer_db):
    with psycopg.connect(consumer_db["admin"], autocommit=True) as c:
        with pytest.raises(errors.CheckViolation):
            c.execute(_INBOX, (helpers.new_uuid(), "inventory-service", 1, _FP,
                               "business_duplicate", None))  # business_duplicate ohne canonical -> CHECK
