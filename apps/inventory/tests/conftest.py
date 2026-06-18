"""Service-isolierte Inventory-Tests: echte FastAPI-App, eigener Importpfad.

Treibt den ECHTEN Inventory-Lifespan in Subprozessen (mit DATABASE_URL je Szenario),
sodass kein fremdes `app`-Paket geladen wird. Nutzt die geteilten Ops-Tools
(ops.db.bootstrap / ops.db.migrate) — dieselben, die Compose/CI/Deployment nutzen.
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

INV_ROOT = pathlib.Path(__file__).resolve().parents[1]            # apps/inventory
REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))                            # fuer ops.db

PG_BINDIR = pathlib.Path(os.environ.get("PG_BINDIR", str(pathlib.Path.home() / ".local/pgtest/pg/bin")))
MIGRATIONS = INV_ROOT / "migrations"

# Treiber: startet den ECHTEN Lifespan, fuegt (bei Erfolg) ein Movement OHNE Publish
# ein und gibt den geladenen Modulpfad aus. Bei nicht vorbereitetem Schema wirft der
# Lifespan -> der Prozess endet mit Fehler (nonzero exit, Meldung in stderr).
_DRIVER = r"""
import asyncio
from app.main import app, lifespan
from app import db
async def main():
    async with lifespan(app):
        rec = db.insert_movement("SKU-1", 3, "WH-1")
        assert rec["event_id"] is not None, "event_id missing"
        assert rec["created_at"] is not None, "created_at missing"
        print("EVENT_ID", rec["event_id"])
        print("CREATED_AT", rec["created_at"])
    print("OK", db.__file__)
asyncio.run(main())
"""

# Treiber nur fuer den Import-Pfad-Nachweis (kein DB-Zugriff noetig).
_PATH_DRIVER = r"""
from app import db, main
print("DBFILE", db.__file__)
print("MAINFILE", main.__file__)
"""


@pytest.fixture(scope="session")
def pg_server():
    if not (PG_BINDIR / "postgres").exists():
        pytest.skip(f"PostgreSQL-Binaries nicht gefunden in {PG_BINDIR}")
    data = tempfile.mkdtemp(prefix="inv_pgdata_")
    sock = tempfile.mkdtemp(prefix="inv_pgsock_")
    subprocess.run([str(PG_BINDIR / "initdb"), "-D", data, "-U", "postgres", "--auth=trust", "-E", "UTF8"],
                   check=True, capture_output=True)
    opts = f"-k {sock} -c listen_addresses= -c fsync=off -c full_page_writes=off -c synchronous_commit=off"
    subprocess.run([str(PG_BINDIR / "pg_ctl"), "-D", data, "-o", opts,
                    "-l", str(pathlib.Path(data) / "log"), "-w", "start"], check=True, capture_output=True)
    maint = f"host={sock} user=postgres dbname=postgres"
    for _ in range(50):
        try:
            with psycopg.connect(maint, connect_timeout=2):
                break
        except psycopg.OperationalError:
            time.sleep(0.2)
    from ops.db.bootstrap import ensure_roles
    ensure_roles(maint)
    try:
        yield {"sock": sock, "maint": maint}
    finally:
        subprocess.run([str(PG_BINDIR / "pg_ctl"), "-D", data, "-m", "immediate", "-w", "stop"],
                       capture_output=True)
        shutil.rmtree(data, ignore_errors=True)
        shutil.rmtree(sock, ignore_errors=True)


_counter = {"n": 0}


def _dsn(sock, user, db):
    return f"host={sock} user={user} dbname={db}"


@pytest.fixture
def make_inventory_db(pg_server):
    """Erzeugt eine frische Inventory-DB in einem bestimmten Schema-Zustand.

    state: 'correct' | 'missing' | 'too_old' | 'unknown'
    Liefert dict(app_dsn, admin_dsn).
    """
    from ops.db import migrate
    sock, maint = pg_server["sock"], pg_server["maint"]

    def make(state: str):
        _counter["n"] += 1
        name = f"inv_{state}_{_counter['n']}"
        with psycopg.connect(maint, autocommit=True) as c:
            c.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
            c.execute(f'CREATE DATABASE "{name}" OWNER inventory_admin')
        admin = _dsn(sock, "inventory_admin", name)
        app = _dsn(sock, "inventory_app", name)
        if state == "correct":
            migrate.run(admin, MIGRATIONS)
        elif state == "missing":
            pass  # leere DB, keine Migration, kein schema_migrations
        elif state == "too_old":
            migrate.run(admin, MIGRATIONS)
            with psycopg.connect(admin, autocommit=True) as c:
                c.execute("DELETE FROM schema_migrations WHERE version='0002_add_stable_event_id'")
        elif state == "unknown":
            migrate.run(admin, MIGRATIONS)
            with psycopg.connect(admin, autocommit=True) as c:
                c.execute("INSERT INTO schema_migrations (version) VALUES ('0099_future')")
        else:
            raise ValueError(state)
        return {"app": app, "admin": admin, "name": name}
    return make


@pytest.fixture
def run_lifespan():
    """Startet den echten Inventory-Lifespan im Subprozess (Inventory-Runtime).

    Optionale `args` werden als sys.argv[1:] an den Treiber gereicht (z. B. der
    Fault-Punkt fuer den Rollback-Nachweis).
    """
    def run(app_dsn: str, driver: str = _DRIVER, args: tuple[str, ...] = ()):
        env = {**os.environ, "DATABASE_URL": app_dsn, "PYTHONPATH": str(INV_ROOT),
               "EVENTS_ENABLED": "false"}
        return subprocess.run([sys.executable, "-c", driver, *args], env=env,
                              capture_output=True, text=True, timeout=60)
    return run


@pytest.fixture
def admin_conn(pg_server):
    """Kontextmanager-Factory fuer eine psycopg-Verbindung als inventory_admin.

    Verifikation im Testprozess laeuft ueber rohes SQL — die Inventory-Runtime
    (app.db) wird NIE in den Testprozess importiert (Service-Isolation)."""
    import contextlib

    @contextlib.contextmanager
    def connect(db):
        with psycopg.connect(db["admin"], autocommit=True) as conn:
            yield conn

    return connect


@pytest.fixture
def path_driver():
    return _PATH_DRIVER
