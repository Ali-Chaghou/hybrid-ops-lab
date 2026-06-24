"""Pytest-Fixtures: echte, ephemere PostgreSQL-Instanz (kein Mock, kein Docker).

Spiegelt das vorhandene Projektmuster (consumer/inventory): Server-Binaries via
PG_BINDIR, Unix-Socket, trust-Auth — Rollen-Privilegien werden dennoch erzwungen
und sind testbar. Stellt eine migrierte Inventory-/Outbox-DB bereit und liefert
DSNs fuer inventory_admin, inventory_app und inventory_publisher.
"""
from __future__ import annotations

import dataclasses
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time

import psycopg
import pytest
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))  # fuer ops.db

PG_BINDIR = pathlib.Path(os.environ.get("PG_BINDIR", str(pathlib.Path.home() / ".local/pgtest/pg/bin")))
INVENTORY_MIGRATIONS = _REPO_ROOT / "apps/inventory/migrations"

_counter = {"n": 0}


@pytest.fixture(scope="session")
def pg_server():
    if not (PG_BINDIR / "postgres").exists():
        pytest.skip(f"PostgreSQL-Binaries nicht gefunden in {PG_BINDIR}")
    data = tempfile.mkdtemp(prefix="pub_pgdata_")
    sock = tempfile.mkdtemp(prefix="pub_pgsock_")
    subprocess.run(
        [str(PG_BINDIR / "initdb"), "-D", data, "-U", "postgres", "--auth=trust", "-E", "UTF8"],
        check=True, capture_output=True,
    )
    opts = f"-k {sock} -c listen_addresses= -c fsync=off -c full_page_writes=off -c synchronous_commit=off"
    subprocess.run(
        [str(PG_BINDIR / "pg_ctl"), "-D", data, "-o", opts,
         "-l", str(pathlib.Path(data) / "log"), "-w", "start"],
        check=True, capture_output=True,
    )
    maint = f"host={sock} user=postgres dbname=postgres"
    for _ in range(50):
        try:
            with psycopg.connect(maint, connect_timeout=2):
                break
        except psycopg.OperationalError:
            time.sleep(0.2)
    # Rollen ueber das echte Bootstrap-Tool (inkl. inventory_publisher).
    from ops.db.bootstrap import ensure_roles
    ensure_roles(maint)
    try:
        yield {"sock": sock, "maint": maint}
    finally:
        subprocess.run([str(PG_BINDIR / "pg_ctl"), "-D", data, "-m", "immediate", "-w", "stop"],
                       capture_output=True)
        shutil.rmtree(data, ignore_errors=True)
        shutil.rmtree(sock, ignore_errors=True)


def _dsn(sock, user, db):
    return f"host={sock} user={user} dbname={db}"


@pytest.fixture
def outbox_db(pg_server):
    """Frische Inventory-DB mit allen Migrationen (inkl. 0004). Liefert DSNs."""
    from ops.db import migrate
    sock, maint = pg_server["sock"], pg_server["maint"]
    _counter["n"] += 1
    name = f"pub_{_counter['n']}"
    with psycopg.connect(maint, autocommit=True) as c:
        c.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        c.execute(f'CREATE DATABASE "{name}" OWNER inventory_admin')
    admin = _dsn(sock, "inventory_admin", name)
    migrate.run(admin, INVENTORY_MIGRATIONS)
    return {
        "name": name,
        "admin": admin,
        "app": _dsn(sock, "inventory_app", name),
        "publisher": _dsn(sock, "inventory_publisher", name),
    }


@pytest.fixture
def seed_pending(outbox_db):
    """Fuegt n pending Movement+Outbox-Zeilen ein (als inventory_app, atomar)."""
    def seed(n: int = 1) -> list:
        event_ids = []
        with psycopg.connect(outbox_db["app"], row_factory=dict_row) as c:
            for i in range(n):
                with c.transaction():
                    row = c.execute(
                        "INSERT INTO stock_movements (sku, quantity, warehouse) "
                        "VALUES (%s,%s,%s) RETURNING id, event_id, created_at",
                        (f"SKU-{i}", i + 1, "WH"),
                    ).fetchone()
                    c.execute(
                        "INSERT INTO event_outbox (event_id, movement_id, event_type, schema_version,"
                        " occurred_at, source, payload) VALUES "
                        "(%s,%s,'inventory.movement.recorded',1,%s,'inventory-service',%s)",
                        (str(row["event_id"]), row["id"], row["created_at"],
                         Jsonb({"movement_id": row["id"], "sku": f"SKU-{i}",
                                "quantity": i + 1, "warehouse": "WH"})),
                    )
                event_ids.append(str(row["event_id"]))
        return event_ids
    return seed


@pytest.fixture
def pub_conn(outbox_db):
    """Factory fuer eine psycopg-Verbindung als inventory_publisher (autocommit=False)."""
    conns = []

    def connect():
        c = psycopg.connect(outbox_db["publisher"], autocommit=False, row_factory=dict_row)
        conns.append(c)
        return c

    yield connect
    for c in conns:
        try:
            c.close()
        except Exception:
            pass


@pytest.fixture
def make_settings(outbox_db):
    """Baut eine Publisher-Settings-Instanz mit Test-DSN + kleinen Timeouts."""
    from app.config import Settings

    base = Settings(
        enabled=True,
        database_url=outbox_db["publisher"],
        sqs_endpoint_url="",
        sqs_queue_url="http://localhost/queue",
        aws_region="eu-central-1",
        batch_size=5,
        lease_seconds=60,
        poll_interval_seconds=0.01,
        backoff_base_seconds=5,
        backoff_max_seconds=300,
        sqs_connect_timeout=2,
        sqs_read_timeout=2,
        max_body_bytes=16 * 1024,
        pool_min_size=1,
        pool_max_size=4,
        pool_timeout_seconds=5,
    )

    def build(**overrides):
        return dataclasses.replace(base, **overrides)

    return build
