"""Migration-Runner + Schema-Migrationen (Integration gegen echtes PostgreSQL)."""
from __future__ import annotations

import pathlib

import psycopg
import pytest
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from ops.db import migrate
from app.store import SchemaVersionError, check_schema_version

CONSUMER_MIGRATIONS = pathlib.Path(__file__).resolve().parents[1] / "migrations"
INVENTORY_MIGRATIONS = pathlib.Path(__file__).resolve().parents[3] / "apps/inventory/migrations"


def _insert_movement_with_outbox(conn, sku, quantity, warehouse):
    """Fuegt Movement + passendes Outbox-Event in EINER Transaktion ein (ab 0003 Pflicht).

    Gibt die event_id des Movements zurueck. conn ist NICHT autocommit.
    """
    with conn.transaction():
        row = conn.execute(
            "INSERT INTO stock_movements (sku, quantity, warehouse) "
            "VALUES (%s,%s,%s) RETURNING id, event_id, created_at",
            (sku, quantity, warehouse),
        ).fetchone()
        conn.execute(
            "INSERT INTO event_outbox (event_id, movement_id, event_type, schema_version, "
            "occurred_at, source, payload) VALUES (%s,%s,'inventory.movement.recorded',1,%s,"
            "'inventory-service',%s)",
            (str(row["event_id"]), row["id"], row["created_at"],
             Jsonb({"movement_id": row["id"], "sku": sku, "quantity": quantity, "warehouse": warehouse})),
        )
    return row["event_id"]


def test_consumer_clean_migration_creates_tables(consumer_db):
    with psycopg.connect(consumer_db["admin"]) as c:
        tabs = {r[0] for r in c.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema='public'"
        ).fetchall()}
    assert {"event_inbox", "movement_projection", "schema_migrations"} <= tabs


def test_runner_records_version_and_is_idempotent(db_factory):
    admin, _app, _name = db_factory()
    first = migrate.run(admin, CONSUMER_MIGRATIONS)
    assert "0001_init" in first
    second = migrate.run(admin, CONSUMER_MIGRATIONS)  # erneuter Lauf
    assert second == []  # keine Migration doppelt
    with psycopg.connect(admin) as c:
        n = c.execute("SELECT count(*) FROM schema_migrations WHERE version='0001_init'").fetchone()[0]
    assert n == 1


def test_check_schema_version_passes_after_migration(consumer_db):
    with psycopg.connect(consumer_db["app"]) as c:
        check_schema_version(c)  # darf nicht werfen


def test_check_schema_version_fails_without_migration(db_factory):
    admin, _app, _name = db_factory()
    # schema_migrations existiert noch nicht -> Runtime darf nicht starten.
    with psycopg.connect(admin) as c:
        with pytest.raises(SchemaVersionError):
            check_schema_version(c)


def test_check_schema_version_fails_too_old(db_factory):
    admin, _app, _name = db_factory()
    with psycopg.connect(admin) as c:
        c.execute("CREATE TABLE schema_migrations (version text primary key, applied_at timestamptz)")
        # leere Historie -> erwartete Migration fehlt
    with psycopg.connect(admin) as c:
        with pytest.raises(SchemaVersionError):
            check_schema_version(c)


def test_check_schema_version_rejects_unknown_newer(consumer_db):
    with psycopg.connect(consumer_db["admin"], autocommit=True) as c:
        c.execute("INSERT INTO schema_migrations (version) VALUES ('0099_future')")
    with psycopg.connect(consumer_db["app"]) as c:
        with pytest.raises(SchemaVersionError):
            check_schema_version(c)


def test_inventory_clean_install(db_factory):
    # Leere Datenbank (KEIN stock_movements) -> alle Migrationen -> vollstaendiges Schema.
    admin, app, _name = db_factory("inventory_admin", "inventory_app")
    applied = migrate.run(admin, INVENTORY_MIGRATIONS)
    assert applied == [
        "0001_create_stock_movements",
        "0002_add_stable_event_id",
        "0003_create_event_outbox",
        "0004_add_outbox_claim_fields",
    ]
    with psycopg.connect(admin) as c:
        tabs = {r[0] for r in c.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema='public'"
        ).fetchall()}
        assert {"stock_movements", "event_outbox", "schema_migrations"} <= tabs
        cols = {r[0] for r in c.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name='stock_movements'"
        ).fetchall()}
        assert {"id", "sku", "quantity", "warehouse", "created_at", "event_id"} <= cols
        notnull = c.execute(
            "SELECT is_nullable FROM information_schema.columns "
            "WHERE table_name='stock_movements' AND column_name='event_id'"
        ).fetchone()[0]
        assert notnull == "NO"
    # Runtime-Rolle darf ein Movement + sein Outbox-Event einfuegen (ab 0003 atomar).
    with psycopg.connect(app, row_factory=dict_row) as c:
        eid = _insert_movement_with_outbox(c, "X", 1, "W")
        assert eid is not None
    # ...aber keine DDL.
    with psycopg.connect(app, autocommit=True) as c:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute("CREATE TABLE evil (i int)")


def test_inventory_second_migration_run_is_noop(db_factory):
    # Zweiter Lauf auf bereits migrierter DB: nichts Neues, keine Nebenwirkungen.
    admin, _app, _name = db_factory("inventory_admin", "inventory_app")
    migrate.run(admin, INVENTORY_MIGRATIONS)
    again = migrate.run(admin, INVENTORY_MIGRATIONS)
    assert again == []
    with psycopg.connect(admin) as c:
        n = c.execute("SELECT count(*) FROM schema_migrations").fetchone()[0]
        assert n == 4


def test_failed_migration_not_marked_applied(db_factory, tmp_path):
    admin, _app, _name = db_factory()
    bad = tmp_path / "0001_bad.sql"
    bad.write_text("CREATE TABLE ok (id int); SELECT * FROM does_not_exist;", encoding="utf-8")
    with pytest.raises(RuntimeError):
        migrate.run(admin, tmp_path)
    with psycopg.connect(admin) as c:
        rows = c.execute("SELECT count(*) FROM schema_migrations WHERE version='0001_bad'").fetchone()[0]
        # auch die Tabelle 'ok' darf nicht bestehen (Transaktion zurueckgerollt)
        tab = c.execute("SELECT to_regclass('public.ok')").fetchone()[0]
    assert rows == 0 and tab is None


def test_inventory_migration_backfills_distinct_event_ids(inventory_old_db):
    admin = inventory_old_db["admin"]
    with psycopg.connect(admin, autocommit=True) as c:
        for i in range(5):
            c.execute("INSERT INTO stock_movements (sku, quantity, warehouse) VALUES (%s,%s,%s)",
                      (f"SKU-{i}", i + 1, "WH"))
    migrate.run(admin, INVENTORY_MIGRATIONS)
    with psycopg.connect(admin) as c:
        total = c.execute("SELECT count(*) FROM stock_movements").fetchone()[0]
        nulls = c.execute("SELECT count(*) FROM stock_movements WHERE event_id IS NULL").fetchone()[0]
        distinct = c.execute("SELECT count(DISTINCT event_id) FROM stock_movements").fetchone()[0]
    assert total == 5 and nulls == 0 and distinct == 5


def test_inventory_migration_sets_default_for_new_rows(inventory_old_db):
    admin = inventory_old_db["admin"]
    migrate.run(admin, INVENTORY_MIGRATIONS)  # leeres Schema (keine Zeilen)
    with psycopg.connect(admin, row_factory=dict_row) as c:
        # Default-event_id greift pro Insert -> zwei Zeilen, zwei verschiedene UUIDs.
        eid1 = _insert_movement_with_outbox(c, "X", 1, "W")
        eid2 = _insert_movement_with_outbox(c, "Y", 1, "W")
    assert eid1 is not None and eid2 is not None and eid1 != eid2


def test_inventory_migration_adds_unique_constraint(inventory_old_db):
    admin = inventory_old_db["admin"]
    migrate.run(admin, INVENTORY_MIGRATIONS)
    with psycopg.connect(admin, row_factory=dict_row) as c:
        eid = _insert_movement_with_outbox(c, "X", 1, "W")
        # Dieselbe event_id explizit erneut -> Unique-Index auf event_id feuert beim Insert.
        with pytest.raises(psycopg.errors.UniqueViolation):
            with c.transaction():
                c.execute(
                    "INSERT INTO stock_movements (sku, quantity, warehouse, event_id) "
                    "VALUES ('Z',1,'W',%s)", (eid,)
                )
