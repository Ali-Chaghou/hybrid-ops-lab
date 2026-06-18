"""Migration-Runner + Schema-Migrationen (Integration gegen echtes PostgreSQL)."""
from __future__ import annotations

import pathlib

import psycopg
import pytest
from ops.db import migrate
from app.store import SchemaVersionError, check_schema_version

CONSUMER_MIGRATIONS = pathlib.Path(__file__).resolve().parents[1] / "migrations"
INVENTORY_MIGRATIONS = pathlib.Path(__file__).resolve().parents[3] / "apps/inventory/migrations"


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
    # Leere Datenbank (KEIN stock_movements) -> beide Migrationen -> vollstaendiges Schema.
    admin, app, _name = db_factory("inventory_admin", "inventory_app")
    applied = migrate.run(admin, INVENTORY_MIGRATIONS)
    assert applied == ["0001_create_stock_movements", "0002_add_stable_event_id"]
    with psycopg.connect(admin) as c:
        cols = {r[0] for r in c.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name='stock_movements'"
        ).fetchall()}
        assert {"id", "sku", "quantity", "warehouse", "created_at", "event_id"} <= cols
        notnull = c.execute(
            "SELECT is_nullable FROM information_schema.columns "
            "WHERE table_name='stock_movements' AND column_name='event_id'"
        ).fetchone()[0]
        assert notnull == "NO"
    # Runtime-Rolle darf ein Movement einfuegen (event_id kommt per Default).
    with psycopg.connect(app, autocommit=True) as c:
        eid = c.execute(
            "INSERT INTO stock_movements (sku, quantity, warehouse) VALUES ('X',1,'W') RETURNING event_id"
        ).fetchone()[0]
        assert eid is not None
        # ...aber keine DDL.
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute("CREATE TABLE evil (i int)")


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
    with psycopg.connect(admin, autocommit=True) as c:
        row = c.execute(
            "INSERT INTO stock_movements (sku, quantity, warehouse) VALUES ('X',1,'W') RETURNING event_id"
        ).fetchone()
        # zweite Zeile: andere event_id (Default greift pro Insert)
        row2 = c.execute(
            "INSERT INTO stock_movements (sku, quantity, warehouse) VALUES ('Y',1,'W') RETURNING event_id"
        ).fetchone()
    assert row[0] is not None and row2[0] is not None and row[0] != row2[0]


def test_inventory_migration_adds_unique_constraint(inventory_old_db):
    admin = inventory_old_db["admin"]
    migrate.run(admin, INVENTORY_MIGRATIONS)
    with psycopg.connect(admin, autocommit=True) as c:
        eid = c.execute(
            "INSERT INTO stock_movements (sku, quantity, warehouse) VALUES ('X',1,'W') RETURNING event_id"
        ).fetchone()[0]
        with pytest.raises(psycopg.errors.UniqueViolation):
            c.execute("INSERT INTO stock_movements (sku, quantity, warehouse, event_id) VALUES ('Z',1,'W',%s)", (eid,))
