"""ops.db.reassign — gezielte, transaktionale Ownership-Uebertragung (echtes PG).

Reproduziert insbesondere die Produktions-Topologie: die Alt-Rolle ist der
Bootstrap-Superuser und besitzt geteilte Cluster-Objekte (`postgres`, `template0`,
`template1`, Tablespaces). Prueft, dass die gezielte Uebertragung (ALTER ... OWNER,
KEIN REASSIGN OWNED) dennoch funktioniert, geteilte Objekte unberuehrt laesst,
unerwartete Objekte ablehnt und idempotent ist.
"""
from __future__ import annotations

import psycopg
import pytest
from ops.db import reassign

_n = {"i": 0}


def _role(maint: str, prefix: str) -> str:
    _n["i"] += 1
    role = f"{prefix}_{_n['i']}"
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


def _create_legacy_inventory(sock: str, name: str, table_owner: str) -> None:
    """Legt im DB `name` die Pre-2B-Objekte an (Tabelle + PK-Index + Identity-Seq) und
    setzt deren Owner auf `table_owner`. Die DB selbst gehoert NICHT `table_owner`
    (wie nach db-prepare): nur die Tabelle/Sequenz/Index gehoeren der Alt-Rolle.
    `ALTER TABLE ... OWNER` zieht Sequenz und Index automatisch mit."""
    with psycopg.connect(f"host={sock} user=postgres dbname={name}", autocommit=True) as c:
        c.execute(
            "CREATE TABLE stock_movements ("
            " id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,"
            " sku TEXT NOT NULL)"
        )
        if table_owner != "postgres":
            c.execute(f'ALTER TABLE stock_movements OWNER TO "{table_owner}"')


# --- Bootstrap-Superuser-Topologie (der eigentliche Produktionsfall) ----------


def test_transfer_with_bootstrap_owner_of_shared_objects(pg_server):
    # Im Test-Cluster ist `postgres` der Bootstrap-Superuser und besitzt die
    # geteilten Datenbanken/Tablespaces — exakt die Produktions-Topologie.
    sock, maint = pg_server["sock"], pg_server["maint"]
    name = _db_owned_by(maint, "inventory_admin")     # DB gehoert (wie nach prepare) inventory_admin
    _create_legacy_inventory(sock, name, "postgres")  # Objekte gehoeren dem Bootstrap-Superuser
    dsn = f"host={sock} user=postgres dbname={name}"
    assert _owner_of(dsn, "stock_movements") == "postgres"

    # Vorbedingung: postgres besitzt tatsaechlich geteilte Objekte.
    with psycopg.connect(dsn) as c:
        shared_dbs = {r[0] for r in c.execute(
            "SELECT datname FROM pg_database WHERE pg_get_userbyid(datdba)='postgres'").fetchall()}
        shared_ts = {r[0] for r in c.execute(
            "SELECT spcname FROM pg_tablespace WHERE pg_get_userbyid(spcowner)='postgres'").fetchall()}
    assert {"postgres", "template0", "template1"} <= shared_dbs
    assert {"pg_default", "pg_global"} <= shared_ts

    res = reassign.transfer_ownership(maint, name, from_role="postgres", to_role="inventory_admin")
    assert res["status"] == "transferred"
    # nur Tabelle + Sequenz erhalten ein explizites ALTER (Index folgt der Tabelle)
    assert any("ALTER TABLE public.stock_movements" in s for s in res["transferred"])
    assert any("ALTER SEQUENCE public.stock_movements_id_seq" in s for s in res["transferred"])
    assert not any("pkey" in s for s in res["transferred"])  # Index NICHT direkt geaendert

    # Ziel-DB-Objekte gehoeren jetzt inventory_admin — inkl. mitgezogenem PK-Index.
    assert _owner_of(dsn, "stock_movements") == "inventory_admin"
    assert _owner_of(dsn, "stock_movements_id_seq") == "inventory_admin"
    assert _owner_of(dsn, "stock_movements_pkey") == "inventory_admin"

    # Geteilte Objekte UNVERAENDERT bei postgres.
    with psycopg.connect(dsn) as c:
        for db in ("postgres", "template0", "template1"):
            assert c.execute(
                "SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname=%s", (db,)
            ).fetchone()[0] == "postgres"
        for ts in ("pg_default", "pg_global"):
            assert c.execute(
                "SELECT pg_get_userbyid(spcowner) FROM pg_tablespace WHERE spcname=%s", (ts,)
            ).fetchone()[0] == "postgres"

    # Idempotent: zweiter Lauf -> No-op, Owner bleibt korrekt.
    res2 = reassign.transfer_ownership(maint, name, from_role="postgres", to_role="inventory_admin")
    assert res2["status"] == "noop-already-correct"
    assert res2["transferred"] == []
    assert _owner_of(dsn, "stock_movements") == "inventory_admin"


def test_transfer_does_not_execute_reassign_owned(pg_server, monkeypatch):
    # Beweis: der Produktionscode setzt KEIN 'REASSIGN OWNED' ab. Wir fangen jede
    # ausgefuehrte SQL ab und pruefen, dass nichts davon REASSIGN OWNED enthaelt.
    sock, maint = pg_server["sock"], pg_server["maint"]
    name = _db_owned_by(maint, "inventory_admin")
    _create_legacy_inventory(sock, name, "postgres")

    seen: list[str] = []
    real_execute = psycopg.Connection.execute

    def spy(self, query, params=None, **kw):
        try:
            seen.append(query.as_string(self) if hasattr(query, "as_string") else str(query))
        except Exception:
            seen.append(str(query))
        return real_execute(self, query, params, **kw)

    monkeypatch.setattr(psycopg.Connection, "execute", spy)
    reassign.transfer_ownership(maint, name, from_role="postgres", to_role="inventory_admin")
    assert seen, "no SQL captured"
    assert not any("REASSIGN OWNED" in s.upper() for s in seen)
    assert any("ALTER TABLE" in s.upper() for s in seen)


# --- Allowlist-Abbruch (vor jeder Mutation) -----------------------------------


def test_aborts_on_unexpected_target_relation(pg_server):
    sock, maint = pg_server["sock"], pg_server["maint"]
    legacy = _role(maint, "legacy_owner")
    name = _db_owned_by(maint, "inventory_admin")
    dsn = f"host={sock} user=postgres dbname={name}"
    with psycopg.connect(f"host={sock} user=postgres dbname={name}", autocommit=True) as c:
        c.execute("CREATE TABLE stock_movements (id int)")  # erlaubt
        c.execute(f'ALTER TABLE stock_movements OWNER TO "{legacy}"')
        c.execute("CREATE TABLE secrets (id int)")          # NICHT erlaubt
        c.execute(f'ALTER TABLE secrets OWNER TO "{legacy}"')
    with pytest.raises(RuntimeError, match="unexpected object"):
        reassign.transfer_ownership(maint, name, from_role=legacy, to_role="inventory_admin")
    # Keine Mutation: beide bleiben bei der Alt-Rolle.
    assert _owner_of(dsn, "stock_movements") == legacy
    assert _owner_of(dsn, "secrets") == legacy


def test_aborts_on_unexpected_shared_database(pg_server):
    sock, maint = pg_server["sock"], pg_server["maint"]
    legacy = _role(maint, "legacy_shared")
    name = _db_owned_by(maint, "inventory_admin")
    extra = _db_owned_by(maint, legacy)  # zusaetzliche DB im Besitz der Alt-Rolle (unerwartet)
    dsn = f"host={sock} user=postgres dbname={name}"
    _create_legacy_inventory(sock, name, legacy)
    with pytest.raises(RuntimeError, match="unexpected shared database"):
        reassign.transfer_ownership(maint, name, from_role=legacy, to_role="inventory_admin")
    # Keine Mutation an Ziel-DB-Objekten.
    assert _owner_of(dsn, "stock_movements") == legacy
    # Die zusaetzliche DB bleibt unveraendert (Diagnose, keine Mutation).
    with psycopg.connect(maint) as c:
        assert c.execute(
            "SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname=%s", (extra,)
        ).fetchone()[0] == legacy


# --- Partial-Failure -> Rollback ----------------------------------------------


def test_partial_failure_rolls_back(pg_server, monkeypatch):
    # Erzwinge einen Verifikationsfehler nach dem ersten ALTER: die gesamte
    # Transaktion muss zurueckgerollt werden (kein Owner-Wechsel persistiert).
    sock, maint = pg_server["sock"], pg_server["maint"]
    legacy = _role(maint, "legacy_partial")
    name = _db_owned_by(maint, "inventory_admin")
    dsn = f"host={sock} user=postgres dbname={name}"
    _create_legacy_inventory(sock, name, legacy)

    # list_allowlisted_relations so manipulieren, dass die Verifikation scheitert
    # (simuliert eine inkonsistente Nach-ALTER-Sicht).
    monkeypatch.setattr(
        reassign, "list_allowlisted_relations",
        lambda *a, **k: [("public", "stock_movements", "r", legacy)],
    )
    with pytest.raises(RuntimeError, match="verification failed"):
        reassign.transfer_ownership(maint, name, from_role=legacy, to_role="inventory_admin")
    # Rollback: Tabelle (und Sequenz) weiterhin bei der Alt-Rolle.
    assert _owner_of(dsn, "stock_movements") == legacy
    assert _owner_of(dsn, "stock_movements_id_seq") == legacy


# --- Idempotente No-op-Faelle -------------------------------------------------


def test_noop_when_source_role_absent(pg_server):
    maint = pg_server["maint"]
    name = _db_owned_by(maint, "inventory_admin")
    res = reassign.transfer_ownership(maint, name, from_role="does_not_exist", to_role="inventory_admin")
    assert res["status"] == "skipped-missing-source"


def test_noop_when_same_role(pg_server):
    maint = pg_server["maint"]
    name = _db_owned_by(maint, "inventory_admin")
    res = reassign.transfer_ownership(maint, name, from_role="inventory_admin", to_role="inventory_admin")
    assert res["status"] == "noop-same-role"
