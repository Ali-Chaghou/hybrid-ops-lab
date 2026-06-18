"""ops.db.reassign — idempotente Eigentums-Uebertragung (echtes PostgreSQL).

Simuliert eine Alt-Rolle, die ein Objekt besitzt, und prueft die kontrollierte
Uebertragung an inventory_admin sowie die idempotenten No-op-Faelle.
"""
from __future__ import annotations

import psycopg
import pytest
from ops.db import reassign

_n = {"i": 0}


def _legacy_role(maint: str) -> str:
    _n["i"] += 1
    role = f"legacy_owner_{_n['i']}"
    with psycopg.connect(maint, autocommit=True) as c:
        c.execute(f'DROP ROLE IF EXISTS "{role}"')
        c.execute(f'CREATE ROLE "{role}" LOGIN')
    return role


def _db_owned_by(maint: str, owner: str) -> str:
    _n["i"] += 1
    name = f"reassign_{_n['i']}"
    with psycopg.connect(maint, autocommit=True) as c:
        c.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        c.execute(f'CREATE DATABASE "{name}" OWNER "{owner}"')
    return name


def _owner_of(dsn: str, relname: str) -> str:
    with psycopg.connect(dsn) as c:
        return c.execute(
            "SELECT pg_get_userbyid(relowner) FROM pg_class WHERE relname=%s", (relname,)
        ).fetchone()[0]


def test_reassign_transfers_table_ownership(pg_server):
    sock, maint = pg_server["sock"], pg_server["maint"]
    legacy = _legacy_role(maint)
    name = _db_owned_by(maint, legacy)
    dsn = f"host={sock} user=postgres dbname={name}"
    with psycopg.connect(f"host={sock} user={legacy} dbname={name}", autocommit=True) as c:
        c.execute("CREATE TABLE stock_movements (id int)")  # erlaubtes Altobjekt
    assert _owner_of(dsn, "stock_movements") == legacy

    res = reassign.reassign_owned(maint, name, from_role=legacy, to_role="inventory_admin")
    assert res["status"] == "reassigned"
    assert res["objects_after"] == 0
    assert "public.stock_movements" in res["relations"]
    assert _owner_of(dsn, "stock_movements") == "inventory_admin"

    # Idempotent: zweiter Lauf bewegt nichts mehr.
    res2 = reassign.reassign_owned(maint, name, from_role=legacy, to_role="inventory_admin")
    assert res2["objects_before"] == 0 and res2["objects_after"] == 0


def test_reassign_aborts_on_unexpected_object(pg_server):
    # Ein Objekt ausserhalb der Allowlist darf NICHT stillschweigend uebertragen
    # werden -> Abbruch, Owner bleibt unveraendert.
    sock, maint = pg_server["sock"], pg_server["maint"]
    legacy = _legacy_role(maint)
    name = _db_owned_by(maint, legacy)
    dsn = f"host={sock} user=postgres dbname={name}"
    with psycopg.connect(f"host={sock} user={legacy} dbname={name}", autocommit=True) as c:
        c.execute("CREATE TABLE stock_movements (id int)")  # erlaubt
        c.execute("CREATE TABLE secrets (id int)")          # NICHT erlaubt
    with pytest.raises(RuntimeError, match="unexpected object"):
        reassign.reassign_owned(maint, name, from_role=legacy, to_role="inventory_admin")
    # Nichts wurde uebertragen.
    assert _owner_of(dsn, "stock_movements") == legacy
    assert _owner_of(dsn, "secrets") == legacy


def test_reassign_noop_when_source_role_absent(pg_server):
    sock, maint = pg_server["sock"], pg_server["maint"]
    name = _db_owned_by(maint, "inventory_admin")
    res = reassign.reassign_owned(maint, name, from_role="does_not_exist", to_role="inventory_admin")
    assert res["status"] == "skipped-missing-source"


def test_reassign_noop_when_same_role(pg_server):
    sock, maint = pg_server["sock"], pg_server["maint"]
    name = _db_owned_by(maint, "inventory_admin")
    res = reassign.reassign_owned(maint, name, from_role="inventory_admin", to_role="inventory_admin")
    assert res["status"] == "noop-same-role"
