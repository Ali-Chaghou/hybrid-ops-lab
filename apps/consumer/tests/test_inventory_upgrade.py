"""Upgrade-Pfad einer ALTEN site-dc-Installation auf Phase 2B (echtes PostgreSQL).

Reproduziert den Live-Ist-Zustand:
  * Datenbank + Tabelle `stock_movements` gehoeren einer Alt-Rolle (wie `inventory`),
  * genau ein bestehender Datensatz,
  * keine Tabelle `schema_migrations`,
  * Rollen `inventory_admin`/`inventory_app` existieren (Cluster-Bootstrap), sind aber
    NICHT Owner der Altobjekte.

Beweist:
  1. OHNE die Ownership-Uebertragung scheitert die Migration (das diagnostizierte Problem).
  2. MIT prepare + gezielter Ownership-Uebertragung + migrate laeuft das Upgrade
     sauber durch und backfillt event_id (0002) und event_outbox (0003).
"""
from __future__ import annotations

import pathlib

import psycopg
import pytest
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from ops.db import migrate, prepare, reassign

INVENTORY_MIGRATIONS = pathlib.Path(__file__).resolve().parents[3] / "apps/inventory/migrations"

_OLD_STOCK_MOVEMENTS = """
CREATE TABLE stock_movements (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    sku         TEXT        NOT NULL,
    quantity    INTEGER     NOT NULL,
    warehouse   TEXT        NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

_n = {"i": 0}


def _legacy_install(pg_server):
    """Erzeugt eine Alt-Installation: DB + stock_movements (1 Zeile) im Besitz einer
    Alt-Rolle. Liefert (legacy_role, db_name, admin_dsn, app_dsn, maint, sock)."""
    sock, maint = pg_server["sock"], pg_server["maint"]
    _n["i"] += 1
    legacy = f"legacy_inv_{_n['i']}"
    name = f"inv_upgrade_{_n['i']}"
    with psycopg.connect(maint, autocommit=True) as c:
        c.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        c.execute(f'DROP ROLE IF EXISTS "{legacy}"')
        c.execute(f'CREATE ROLE "{legacy}" LOGIN')
        c.execute(f'CREATE DATABASE "{name}" OWNER "{legacy}"')
    # Alt-Tabelle + genau ein Datensatz, im Besitz der Alt-Rolle.
    with psycopg.connect(f"host={sock} user={legacy} dbname={name}", autocommit=True) as c:
        c.execute(_OLD_STOCK_MOVEMENTS)
        c.execute("INSERT INTO stock_movements (sku, quantity, warehouse) VALUES ('PAL-OLD',3,'DC')")
    admin_dsn = f"host={sock} user=inventory_admin dbname={name}"
    app_dsn = f"host={sock} user=inventory_app dbname={name}"
    return legacy, name, admin_dsn, app_dsn, maint, sock


def test_upgrade_without_reassign_fails(pg_server):
    # Diagnose: db-prepare aendert nur den DB-Owner; die Tabelle gehoert weiter der
    # Alt-Rolle -> die als inventory_admin laufende Migration 0002 scheitert.
    legacy, name, admin_dsn, _app, maint, _sock = _legacy_install(pg_server)
    prepare.ensure_database(maint, name, "inventory_admin")  # nur DB-Owner
    with pytest.raises(RuntimeError):
        migrate.run(admin_dsn, INVENTORY_MIGRATIONS)
    # Tabelle weiterhin im Besitz der Alt-Rolle, nicht voll migriert.
    with psycopg.connect(admin_dsn) as c:
        owner = c.execute(
            "SELECT pg_get_userbyid(relowner) FROM pg_class WHERE relname='stock_movements'"
        ).fetchone()[0]
        applied = {r[0] for r in c.execute("SELECT version FROM schema_migrations").fetchall()}
    assert owner == legacy
    assert "0002_add_stable_event_id" not in applied


def test_upgrade_with_reassign_succeeds_and_backfills(pg_server):
    legacy, name, admin_dsn, app_dsn, maint, _sock = _legacy_install(pg_server)

    # Kontrollierte Upgrade-Reihenfolge: prepare -> Ownership-Uebertragung -> migrate.
    assert prepare.ensure_database(maint, name, "inventory_admin") == "owner-corrected"
    res = reassign.transfer_ownership(maint, name, from_role=legacy, to_role="inventory_admin")
    assert res["status"] == "transferred"
    applied = migrate.run(admin_dsn, INVENTORY_MIGRATIONS)
    assert applied == [
        "0001_create_stock_movements",
        "0002_add_stable_event_id",
        "0003_create_event_outbox",
        "0004_add_outbox_claim_fields",
    ]

    with psycopg.connect(admin_dsn, row_factory=dict_row) as c:
        # Ownership liegt jetzt bei inventory_admin.
        for tbl in ("stock_movements", "event_outbox"):
            owner = c.execute(
                "SELECT pg_get_userbyid(relowner) FROM pg_class WHERE relname=%s", (tbl,)
            ).fetchone()["pg_get_userbyid"]
            assert owner == "inventory_admin", (tbl, owner)
        # 0002 hat event_id der bestehenden Zeile backfilled.
        mv = c.execute(
            "SELECT id, event_id, created_at, sku, quantity, warehouse FROM stock_movements"
        ).fetchall()
        assert len(mv) == 1 and mv[0]["event_id"] is not None
        # 0003 hat genau ein passendes pending-Outbox-Event backfilled.
        ob = c.execute(
            "SELECT movement_id, event_id, occurred_at, status, attempt_count, "
            "published_at, last_error, payload FROM event_outbox"
        ).fetchall()
        assert len(ob) == 1
        e = ob[0]
        assert e["movement_id"] == mv[0]["id"]
        assert e["event_id"] == mv[0]["event_id"]
        assert e["occurred_at"] == mv[0]["created_at"]
        assert e["status"] == "pending" and e["attempt_count"] == 0
        assert e["published_at"] is None and e["last_error"] is None
        assert e["payload"] == {
            "movement_id": mv[0]["id"], "sku": "PAL-OLD", "quantity": 3, "warehouse": "DC",
        }

    # Runtime-Rolle kann nach dem Upgrade Movement + Outbox atomar schreiben ...
    with psycopg.connect(app_dsn, row_factory=dict_row) as conn:
        with conn.transaction():
            row = conn.execute(
                "INSERT INTO stock_movements (sku, quantity, warehouse) "
                "VALUES ('NEW',1,'DC') RETURNING id, event_id, created_at"
            ).fetchone()
            conn.execute(
                "INSERT INTO event_outbox (event_id, movement_id, event_type, schema_version, "
                "occurred_at, source, payload) VALUES (%s,%s,'inventory.movement.recorded',1,%s,"
                "'inventory-service',%s)",
                (str(row["event_id"]), row["id"], row["created_at"],
                 Jsonb({"movement_id": row["id"], "sku": "NEW", "quantity": 1, "warehouse": "DC"})),
            )
    # ... darf event_outbox aber nicht lesen (Least Privilege erhalten).
    with psycopg.connect(app_dsn) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with conn.transaction():
                conn.execute("SELECT 1 FROM event_outbox")

    # Zweiter Migrationslauf ist ein No-op (Idempotenz).
    assert migrate.run(admin_dsn, INVENTORY_MIGRATIONS) == []
