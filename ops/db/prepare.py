"""Idempotente Vorbereitung einer Service-Datenbank: CREATE DATABASE + Owner.

Cluster-Admin-Werkzeug; laeuft VOR den Migrationen. Keine Tabellen-DDL, keine
Connection-/Passwort-Ausgabe. Zielname wird ausschliesslich ueber
psycopg.sql.Identifier behandelt (kein interpoliertes untrusted SQL).
"""
from __future__ import annotations

import argparse
import os
import sys

import psycopg
from psycopg import sql


def ensure_database(admin_dsn: str, dbname: str, owner: str) -> str:
    """Stellt sicher, dass `dbname` existiert und `owner` gehoert.

    Rueckgabe: 'created' | 'owner-corrected' | 'ok'. Idempotent.
    CREATE DATABASE/ALTER DATABASE laufen NICHT in einer Transaktion -> autocommit.
    """
    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        current_owner = conn.execute(
            "SELECT pg_catalog.pg_get_userbyid(datdba) FROM pg_database WHERE datname = %s",
            (dbname,),
        ).fetchone()
        if current_owner is None:
            conn.execute(
                sql.SQL("CREATE DATABASE {} OWNER {}").format(
                    sql.Identifier(dbname), sql.Identifier(owner)
                )
            )
            return "created"
        if current_owner[0] != owner:
            # kontrollierte Korrektur (Cluster-Admin ist Owner-of-record / Superuser)
            conn.execute(
                sql.SQL("ALTER DATABASE {} OWNER TO {}").format(
                    sql.Identifier(dbname), sql.Identifier(owner)
                )
            )
            return "owner-corrected"
        return "ok"


def _main(argv: list[str]) -> int:
    admin_dsn = os.environ.get("PG_ADMIN_DSN")
    if not admin_dsn:
        print("error: PG_ADMIN_DSN not set", file=sys.stderr)
        return 2
    ap = argparse.ArgumentParser(description="Ensure a service database exists with the right owner.")
    ap.add_argument("--database", default=os.environ.get("INVENTORY_DB", "inventory"))
    ap.add_argument("--owner", default=os.environ.get("INVENTORY_DB_OWNER", "inventory_admin"))
    args = ap.parse_args(argv)
    result = ensure_database(admin_dsn, args.database, args.owner)
    print(f"database {args.database}: {result}")  # nur Status, kein Secret
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
