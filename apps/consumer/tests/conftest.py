"""Pytest-Fixtures: echte, ephemere PostgreSQL-Instanz (kein Mock, kein Docker noetig).

Server-Binaries via PG_BINDIR (Default: ~/.local/pgtest/pg/bin). In CI zeigt PG_BINDIR
auf ein Container-/System-PostgreSQL. Lokale Unix-Socket-Instanz, trust-Auth; die
Rollen-Privilegien (GRANTs) werden trotzdem erzwungen und sind so testbar.
"""
from __future__ import annotations

import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time

import psycopg
import pytest

# Repo-Root auf den Pfad legen, damit die geteilten Ops-Tools (ops.db) importierbar
# sind, OHNE das fremde Service-`app`-Paket sichtbar zu machen (keine Kollision).
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

PG_BINDIR = pathlib.Path(os.environ.get("PG_BINDIR", str(pathlib.Path.home() / ".local/pgtest/pg/bin")))
CONSUMER_MIGRATIONS = pathlib.Path(__file__).resolve().parents[1] / "migrations"
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

_counter = {"n": 0}


@pytest.fixture(scope="session")
def pg_server():
    """Startet einen Postgres fuer die ganze Test-Session, raeumt ihn am Ende ab."""
    if not (PG_BINDIR / "postgres").exists():
        pytest.skip(f"PostgreSQL-Binaries nicht gefunden in {PG_BINDIR}")
    data = tempfile.mkdtemp(prefix="hol_pgdata_")
    sock = tempfile.mkdtemp(prefix="hol_pgsock_")
    subprocess.run(
        [str(PG_BINDIR / "initdb"), "-D", data, "-U", "postgres", "--auth=trust", "-E", "UTF8"],
        check=True, capture_output=True,
    )
    # Wegwerf-Instanz: Durability-Flags aus -> deutlich schnellere DB-Anlage je Test.
    opts = f"-k {sock} -c listen_addresses= -c fsync=off -c full_page_writes=off -c synchronous_commit=off"
    # WICHTIG: -l logfile. Ohne separates Logfile erbt der Server-Daemon die von
    # capture_output erzeugte Pipe und haelt sie offen -> subprocess.run blockiert
    # ewig beim Lesen von stdout. Mit -l schreibt der Server in die Datei.
    logfile = str(pathlib.Path(data) / "startup.log")
    subprocess.run(
        [str(PG_BINDIR / "pg_ctl"), "-D", data, "-o", opts, "-l", logfile, "-w", "start"],
        check=True, capture_output=True,
    )
    admin_maint = f"host={sock} user=postgres dbname=postgres"
    # auf Bereitschaft warten
    for _ in range(50):
        try:
            with psycopg.connect(admin_maint, connect_timeout=2):
                break
        except psycopg.OperationalError:
            time.sleep(0.2)
    # Cluster-Rollen ueber das echte Bootstrap-Tool anlegen (DRY + uebt es mit).
    from ops.db.bootstrap import ensure_roles
    ensure_roles(admin_maint)
    try:
        yield {"sock": sock, "maint": admin_maint}
    finally:
        subprocess.run([str(PG_BINDIR / "pg_ctl"), "-D", data, "-m", "immediate", "-w", "stop"],
                       capture_output=True)
        shutil.rmtree(data, ignore_errors=True)
        shutil.rmtree(sock, ignore_errors=True)


def _fresh_db(pg_server, owner: str) -> str:
    _counter["n"] += 1
    name = f"t_{owner}_{_counter['n']}"
    with psycopg.connect(pg_server["maint"], autocommit=True) as c:
        c.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        c.execute(f'CREATE DATABASE "{name}" OWNER "{owner}"')
    return name


def _dsn(pg_server, user: str, db: str) -> str:
    return f"host={pg_server['sock']} user={user} dbname={db}"


@pytest.fixture
def db_factory(pg_server):
    """Factory: erzeugt eine frische, leere DB und liefert (admin_dsn, app_dsn, name)."""
    def make(owner: str = "consumer_admin", app: str = "consumer_app"):
        db = _fresh_db(pg_server, owner)
        return _dsn(pg_server, owner, db), _dsn(pg_server, app, db), db
    return make


@pytest.fixture
def consumer_db(pg_server):
    """Frische DB mit angewandter Consumer-Migration. Liefert admin_dsn + app_dsn."""
    from ops.db import migrate

    db = _fresh_db(pg_server, "consumer_admin")
    admin_dsn = _dsn(pg_server, "consumer_admin", db)
    app_dsn = _dsn(pg_server, "consumer_app", db)
    migrate.run(admin_dsn, CONSUMER_MIGRATIONS)
    yield {"admin": admin_dsn, "app": app_dsn, "name": db, "server": pg_server}


@pytest.fixture
def inventory_old_db(pg_server):
    """Frische DB mit ALTEM stock_movements-Schema (vor der event_id-Migration)."""
    db = _fresh_db(pg_server, "inventory_admin")
    admin_dsn = _dsn(pg_server, "inventory_admin", db)
    app_dsn = _dsn(pg_server, "inventory_app", db)
    with psycopg.connect(admin_dsn, autocommit=True) as c:
        c.execute(_OLD_STOCK_MOVEMENTS)
    yield {"admin": admin_dsn, "app": app_dsn, "name": db, "server": pg_server}
