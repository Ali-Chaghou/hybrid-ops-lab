"""Least-Privilege der Runtime-Rolle consumer_app (Integration)."""
from __future__ import annotations

import psycopg
import pytest
from psycopg import errors

_APPLIED_ROW = (
    "INSERT INTO event_inbox (event_id, event_type, source, source_movement_id, schema_version, fingerprint, disposition) "
    "VALUES (gen_random_uuid(), 'inventory.movement.recorded', 'inventory-service', 1, 1, %s, 'applied')"
)
_FP = "a" * 64


def test_runtime_can_select(consumer_db):
    with psycopg.connect(consumer_db["app"]) as c:
        assert c.execute("SELECT count(*) FROM event_inbox").fetchone()[0] == 0


def test_runtime_can_insert(consumer_db):
    with psycopg.connect(consumer_db["app"], autocommit=True) as c:
        c.execute(_APPLIED_ROW, (_FP,))
        assert c.execute("SELECT count(*) FROM event_inbox").fetchone()[0] == 1


@pytest.mark.parametrize("sql", [
    "CREATE TABLE evil (id int)",
    "ALTER TABLE event_inbox ADD COLUMN evil int",
    "DROP TABLE movement_projection",
    "DELETE FROM event_inbox",
    "TRUNCATE event_inbox",
    "UPDATE event_inbox SET disposition='applied'",
])
def test_runtime_denied_privileged_operations(consumer_db, sql):
    with psycopg.connect(consumer_db["app"], autocommit=True) as c:
        with pytest.raises(errors.InsufficientPrivilege):
            c.execute(sql)
