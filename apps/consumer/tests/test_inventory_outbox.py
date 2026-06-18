"""DB-Ebene: Inventory event_outbox — Backfill, 1-zu-1-Invarianten, Least-Privilege
und Contract-Kompatibilitaet.

Alle Tests arbeiten ueber rohes SQL (psycopg) + den geteilten Migration-Runner
(ops.db.migrate) gegen die Inventory-Migrationen. Der Kompatibilitaetstest laedt den
CONSUMER-Validator (app.envelope) — aber NIE die Inventory-Runtime. So bleibt die
Service-Isolation gewahrt.
"""
from __future__ import annotations

import json
import pathlib
import uuid
from datetime import datetime, timezone

import psycopg
import pytest
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from ops.db import migrate

# Consumer-Validator (zulaessig); die Inventory-Runtime wird nie importiert.
from app.envelope import validate

INVENTORY_MIGRATIONS = pathlib.Path(__file__).resolve().parents[3] / "apps/inventory/migrations"

EVENT_TYPE = "inventory.movement.recorded"
SOURCE = "inventory-service"


def _migrated(db_factory):
    """Frisch migrierte Inventory-DB (alle Migrationen). Liefert (admin, app)."""
    admin, app, _name = db_factory("inventory_admin", "inventory_app")
    migrate.run(admin, INVENTORY_MIGRATIONS)
    return admin, app


def _outbox_insert_sql() -> str:
    return (
        "INSERT INTO event_outbox "
        "(event_id, movement_id, event_type, schema_version, occurred_at, source, payload) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s)"
    )


# --- Backfill (Upgrade 0001/0002 -> 0003) -------------------------------------


def test_backfill_creates_one_pending_event_per_existing_movement(inventory_old_db):
    admin = inventory_old_db["admin"]
    with psycopg.connect(admin, autocommit=True) as c:
        for i in range(5):
            c.execute(
                "INSERT INTO stock_movements (sku, quantity, warehouse) VALUES (%s,%s,%s)",
                (f"SKU-{i}", i + 1, "WH"),
            )
    migrate.run(admin, INVENTORY_MIGRATIONS)
    with psycopg.connect(admin, row_factory=dict_row) as c:
        mv = c.execute("SELECT count(*) AS n FROM stock_movements").fetchone()["n"]
        ob = c.execute("SELECT count(*) AS n FROM event_outbox").fetchone()["n"]
        assert mv == 5 and ob == 5  # je Movement genau ein Event
        rows = c.execute(
            "SELECT m.id, m.event_id, m.created_at, m.sku, m.quantity, m.warehouse, "
            "o.movement_id, o.event_id AS o_eid, o.occurred_at, o.status, o.attempt_count, "
            "o.published_at, o.last_error, o.event_type, o.schema_version, o.source, o.payload "
            "FROM stock_movements m JOIN event_outbox o ON o.movement_id = m.id ORDER BY m.id"
        ).fetchall()
    assert len(rows) == 5
    for r in rows:
        # gleiche event_id und movement_id
        assert r["o_eid"] == r["event_id"]
        assert r["movement_id"] == r["id"]
        # occurred_at == created_at
        assert r["occurred_at"] == r["created_at"]
        # pending / 0 / NULL / NULL
        assert r["status"] == "pending"
        assert r["attempt_count"] == 0
        assert r["published_at"] is None
        assert r["last_error"] is None
        # feste Contract-Werte + exakte Payload
        assert r["event_type"] == EVENT_TYPE
        assert r["schema_version"] == 1
        assert r["source"] == SOURCE
        assert r["payload"] == {
            "movement_id": r["id"],
            "sku": r["sku"],
            "quantity": r["quantity"],
            "warehouse": r["warehouse"],
        }


# --- Strikte 1-zu-1-Invariante (DB erzwingt die Zuordnung) ---------------------


def test_movement_without_event_fails_at_commit(db_factory):
    admin, _app = _migrated(db_factory)
    with psycopg.connect(admin) as conn:
        # Der rueckwaerts gerichtete DEFERRABLE-FK feuert erst beim Commit.
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            with conn.transaction():
                conn.execute(
                    "INSERT INTO stock_movements (sku, quantity, warehouse) VALUES ('A',1,'W')"
                )


def test_event_without_movement_fails_immediately(db_factory):
    admin, _app = _migrated(db_factory)
    with psycopg.connect(admin) as conn:
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            with conn.transaction():
                conn.execute(
                    _outbox_insert_sql(),
                    (str(uuid.uuid4()), 999999, EVENT_TYPE, 1,
                     datetime.now(timezone.utc), SOURCE,
                     Jsonb({"movement_id": 999999, "sku": "A", "quantity": 1, "warehouse": "W"})),
                )


def test_wrong_movement_event_combination_fails(db_factory):
    admin, _app = _migrated(db_factory)
    with psycopg.connect(admin, row_factory=dict_row) as conn:
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            with conn.transaction():
                row = conn.execute(
                    "INSERT INTO stock_movements (sku, quantity, warehouse) "
                    "VALUES ('A',1,'W') RETURNING id, event_id, created_at"
                ).fetchone()
                wrong_eid = uuid.uuid4()  # != row['event_id']
                conn.execute(
                    _outbox_insert_sql(),
                    (str(wrong_eid), row["id"], EVENT_TYPE, 1, row["created_at"], SOURCE,
                     Jsonb({"movement_id": row["id"], "sku": "A", "quantity": 1, "warehouse": "W"})),
                )


# --- Least Privilege (inventory_app) ------------------------------------------


def test_inventory_app_can_fill_both_tables(db_factory):
    admin, app = _migrated(db_factory)
    with psycopg.connect(app, row_factory=dict_row) as conn:
        with conn.transaction():
            row = conn.execute(
                "INSERT INTO stock_movements (sku, quantity, warehouse) "
                "VALUES ('A',1,'W') RETURNING id, event_id, created_at"
            ).fetchone()
            conn.execute(
                _outbox_insert_sql(),
                (str(row["event_id"]), row["id"], EVENT_TYPE, 1, row["created_at"], SOURCE,
                 Jsonb({"movement_id": row["id"], "sku": "A", "quantity": 1, "warehouse": "W"})),
            )
    with psycopg.connect(admin, row_factory=dict_row) as c:
        assert c.execute("SELECT count(*) AS n FROM event_outbox").fetchone()["n"] == 1


def test_inventory_app_cannot_read_modify_event_outbox(db_factory):
    _admin, app = _migrated(db_factory)
    for stmt in (
        "SELECT * FROM event_outbox",
        "UPDATE event_outbox SET status='published'",
        "DELETE FROM event_outbox",
    ):
        with psycopg.connect(app) as conn:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                with conn.transaction():
                    conn.execute(stmt)


def test_inventory_app_cannot_modify_stock_movements(db_factory):
    _admin, app = _migrated(db_factory)
    for stmt in (
        "UPDATE stock_movements SET sku='x'",
        "DELETE FROM stock_movements",
    ):
        with psycopg.connect(app) as conn:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                with conn.transaction():
                    conn.execute(stmt)


def test_inventory_app_cannot_set_publisher_columns(db_factory):
    # inventory_app hat INSERT nur auf den Producer-Spalten. Operative Publisher-
    # Spalten (status, published_at, ...) explizit zu setzen scheitert, OHNE dass
    # eine zusaetzliche Zeile zurueckbleibt; die operativen Spalten kommen sonst
    # ausschliesslich aus ihren Defaults.
    admin, app = _migrated(db_factory)
    # Baseline: normaler Producer-Insert committet weiterhin (nur Producer-Spalten).
    with psycopg.connect(app, row_factory=dict_row) as conn:
        with conn.transaction():
            row = conn.execute(
                "INSERT INTO stock_movements (sku, quantity, warehouse) "
                "VALUES ('A',1,'W') RETURNING id, event_id, created_at"
            ).fetchone()
            conn.execute(
                _outbox_insert_sql(),
                (str(row["event_id"]), row["id"], EVENT_TYPE, 1, row["created_at"], SOURCE,
                 Jsonb({"movement_id": row["id"], "sku": "A", "quantity": 1, "warehouse": "W"})),
            )
    # Direktes Setzen einer operativen Spalte -> InsufficientPrivilege (Rechtepruefung
    # vor jeder Constraint-Pruefung).
    for col, literal in (("status", "'published'"), ("published_at", "now()")):
        with psycopg.connect(app) as conn:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                with conn.transaction():
                    conn.execute(
                        "INSERT INTO event_outbox (event_id, movement_id, event_type, "
                        f"schema_version, occurred_at, source, payload, {col}) "
                        f"VALUES (%s, %s, %s, %s, now(), %s, %s, {literal})",
                        (str(uuid.uuid4()), 424242, EVENT_TYPE, 1, SOURCE,
                         Jsonb({"movement_id": 424242, "sku": "B", "quantity": 1, "warehouse": "W"})),
                    )
    # Keine zusaetzliche Movement- oder Outbox-Zeile zurueckgeblieben: genau die Baseline.
    with psycopg.connect(admin, row_factory=dict_row) as c:
        assert c.execute("SELECT count(*) AS n FROM stock_movements").fetchone()["n"] == 1
        assert c.execute("SELECT count(*) AS n FROM event_outbox").fetchone()["n"] == 1


# --- Publisher-Index (partiell, fuer die spaetere pending-Abfrage) -------------


def test_pending_outbox_partial_index(db_factory):
    admin, _app = _migrated(db_factory)
    with psycopg.connect(admin, row_factory=dict_row) as c:
        row = c.execute(
            """
            SELECT
                i.indpred IS NOT NULL                    AS is_partial,
                pg_get_expr(i.indpred, i.indrelid)       AS predicate,
                array_agg(a.attname ORDER BY k.ord)      AS cols
            FROM pg_index i
            JOIN pg_class ic ON ic.oid = i.indexrelid
            JOIN pg_class tc ON tc.oid = i.indrelid
            JOIN LATERAL unnest(i.indkey) WITH ORDINALITY AS k(attnum, ord) ON true
            JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = k.attnum
            WHERE ic.relname = 'event_outbox_pending_available_idx'
              AND tc.relname = 'event_outbox'
            GROUP BY i.indpred, i.indrelid
            """
        ).fetchone()
    assert row is not None                                   # Index existiert
    assert row["is_partial"] is True                         # partieller Index
    pred = row["predicate"]
    assert "status" in pred and "'pending'" in pred          # Praedikat status = 'pending'
    assert {"available_at", "created_at", "event_id"} <= set(row["cols"])  # Indexspalten


# --- Contract-Kompatibilitaet (Consumer-Validator, ohne Inventory-Runtime) ----


def test_stored_event_validates_against_consumer_contract(inventory_old_db):
    # Bestands-Movement -> Backfill erzeugt das gespeicherte Outbox-Event.
    admin = inventory_old_db["admin"]
    with psycopg.connect(admin, autocommit=True) as c:
        c.execute(
            "INSERT INTO stock_movements (sku, quantity, warehouse) VALUES ('PAL-001',10,'DC')"
        )
    migrate.run(admin, INVENTORY_MIGRATIONS)
    with psycopg.connect(admin, row_factory=dict_row) as c:
        row = c.execute(
            "SELECT event_id, event_type, schema_version, occurred_at, source, payload "
            "FROM event_outbox"
        ).fetchone()

    # Das gespeicherte Event als vollstaendiges Envelope serialisieren ...
    envelope = {
        "event_id": str(row["event_id"]),
        "event_type": row["event_type"],
        "schema_version": row["schema_version"],
        "occurred_at": row["occurred_at"].astimezone(timezone.utc).isoformat(),
        "source": row["source"],
        "payload": row["payload"],
    }
    raw = json.dumps(envelope).encode("utf-8")

    # ... und mit dem bestehenden Consumer-Validator pruefen.
    env = validate(raw)
    assert str(env.event_id) == envelope["event_id"]
    assert env.movement_id == row["payload"]["movement_id"]
    assert env.sku == "PAL-001"
    assert env.quantity == 10
    assert env.warehouse == "DC"
