"""DB-Prepare (ops.db.prepare): idempotente DB-Anlage + Owner-Verwaltung."""
from __future__ import annotations

import psycopg
import pytest
from ops.db import prepare

_n = {"i": 0}


def _name(tag: str) -> str:
    _n["i"] += 1
    return f"prep_{tag}_{_n['i']}"


def _owner(maint: str, db: str):
    with psycopg.connect(maint) as c:
        row = c.execute(
            "SELECT pg_catalog.pg_get_userbyid(datdba) FROM pg_database WHERE datname=%s", (db,)
        ).fetchone()
    return row[0] if row else None


def test_creates_db_if_missing(pg_server):
    db = _name("create")
    assert prepare.ensure_database(pg_server["maint"], db, "inventory_admin") == "created"
    assert _owner(pg_server["maint"], db) == "inventory_admin"


def test_idempotent_second_run(pg_server):
    db = _name("idem")
    prepare.ensure_database(pg_server["maint"], db, "inventory_admin")
    assert prepare.ensure_database(pg_server["maint"], db, "inventory_admin") == "ok"


def test_corrects_wrong_owner(pg_server):
    db = _name("owner")
    with psycopg.connect(pg_server["maint"], autocommit=True) as c:
        c.execute(f'CREATE DATABASE "{db}" OWNER consumer_admin')   # absichtlich falscher Owner
    assert prepare.ensure_database(pg_server["maint"], db, "inventory_admin") == "owner-corrected"
    assert _owner(pg_server["maint"], db) == "inventory_admin"


def test_missing_env_aborts(monkeypatch, capsys):
    monkeypatch.delenv("PG_ADMIN_DSN", raising=False)
    assert prepare._main([]) == 2
    assert "PG_ADMIN_DSN" in capsys.readouterr().err


def test_cli_runs_and_prints_no_secret(pg_server, monkeypatch, capsys):
    db = _name("cli")
    monkeypatch.setenv("PG_ADMIN_DSN", pg_server["maint"])
    rc = prepare._main(["--database", db, "--owner", "inventory_admin"])
    out = capsys.readouterr().out
    assert rc == 0
    assert db in out and "password" not in out.lower()
