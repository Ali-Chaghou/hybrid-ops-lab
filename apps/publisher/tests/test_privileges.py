"""Least-Privilege der Rolle inventory_publisher (Integration).

Positiv: SELECT/Spalten-UPDATE auf event_outbox, SELECT schema_migrations.
Negativ: kein stock_movements, kein INSERT/DELETE, keine DDL, kein UPDATE auf
nicht erlaubte Spalten (payload/event_id).
"""
from __future__ import annotations

import psycopg
import pytest
from psycopg import errors


def _pub(outbox_db, **kw):
    return psycopg.connect(outbox_db["publisher"], autocommit=True, **kw)


# --- Positiv ----------------------------------------------------------------

def test_can_select_event_outbox(outbox_db, seed_pending):
    seed_pending(1)
    with _pub(outbox_db) as c:
        assert c.execute("SELECT count(*) FROM event_outbox").fetchone()[0] == 1


def test_can_select_schema_migrations(outbox_db):
    with _pub(outbox_db) as c:
        assert c.execute("SELECT count(*) FROM schema_migrations").fetchone()[0] >= 4


def test_can_update_allowed_columns(outbox_db, seed_pending):
    seed_pending(1)
    with _pub(outbox_db) as c:
        c.execute("UPDATE event_outbox SET available_at = now(), attempt_count = attempt_count")


# --- Negativ ----------------------------------------------------------------

def test_cannot_read_stock_movements(outbox_db, seed_pending):
    seed_pending(1)
    with _pub(outbox_db) as c:
        with pytest.raises(errors.InsufficientPrivilege):
            c.execute("SELECT count(*) FROM stock_movements")


def test_cannot_insert_outbox(outbox_db):
    with _pub(outbox_db) as c:
        with pytest.raises(errors.InsufficientPrivilege):
            c.execute(
                "INSERT INTO event_outbox (event_id, movement_id, event_type, schema_version,"
                " occurred_at, source, payload) VALUES "
                "(gen_random_uuid(), 1, 'inventory.movement.recorded', 1, now(), 'inventory-service', '{}'::jsonb)"
            )


def test_cannot_delete_outbox(outbox_db, seed_pending):
    seed_pending(1)
    with _pub(outbox_db) as c:
        with pytest.raises(errors.InsufficientPrivilege):
            c.execute("DELETE FROM event_outbox")


def test_cannot_ddl(outbox_db):
    with _pub(outbox_db) as c:
        with pytest.raises(errors.InsufficientPrivilege):
            c.execute("CREATE TABLE evil (i int)")


def test_cannot_update_forbidden_columns(outbox_db, seed_pending):
    seed_pending(1)
    with _pub(outbox_db) as c:
        # payload ist NICHT im Spalten-UPDATE-Grant -> verweigert (keine Payload-Mutation).
        with pytest.raises(errors.InsufficientPrivilege):
            c.execute("UPDATE event_outbox SET payload = '{}'::jsonb")
    with _pub(outbox_db) as c:
        # event_id ist NICHT im Grant -> verweigert (keine event_id-Mutation).
        with pytest.raises(errors.InsufficientPrivilege):
            c.execute("UPDATE event_outbox SET event_id = gen_random_uuid()")
