"""Kleiner, nachvollziehbarer Migration-Runner (kein Framework).

- versionierte *.sql-Dateien in lexikografischer Reihenfolge,
- Tabelle `schema_migrations` haelt angewandte Versionen,
- jede Migration genau einmal, jede zusammen mit ihrem Vermerk in EINER Transaktion,
- Fehler bricht den Lauf ab (kein Eintrag in schema_migrations),
- als Owner/Admin-Rolle auszufuehren (nicht als Runtime-Rolle),
- **PostgreSQL-Advisory-Lock** serialisiert konkurrierende Runner pro Datenbank.

Aufruf:
    python -m ops.db.migrate --database-url <ADMIN_DSN> --migrations-dir <DIR>
Credentials werden nicht ausgegeben.
"""
from __future__ import annotations

import argparse
import os
import pathlib
import sys

import psycopg

# Fester, dokumentierter Lock-Identifier dieses Migrationssystems. Der zweite
# Schluessel (hashtext(current_database())) macht den Lock pro Datenbank eindeutig,
# sodass Migrationen verschiedener DBs einander nicht blockieren.
MIGRATION_LOCK_CLASS = 4711  # int4, willkuerlich aber fix fuer "hybrid-ops-lab migrations"

_SCHEMA_MIGRATIONS = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version    text        PRIMARY KEY,
    applied_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""


def _applied(conn: psycopg.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT version FROM schema_migrations").fetchall()}


def run(database_url: str, migrations_dir: str | pathlib.Path) -> list[str]:
    """Wendet ausstehende Migrationen an und gibt die neu angewandten Versionen zurueck.

    Ein zweiter, gleichzeitig gestarteter Runner wartet am Advisory-Lock und stellt
    danach fest, dass nichts mehr zu tun ist (keine doppelte Ausfuehrung, kein
    'already exists'-Laerm).
    """
    directory = pathlib.Path(migrations_dir)
    files = sorted(directory.glob("*.sql"))
    applied_now: list[str] = []

    with psycopg.connect(database_url, autocommit=True) as conn:
        # Advisory-Lock VOR Pruefung und Ausfuehrung. Session-Lock -> ueberdauert die
        # einzelnen Migrations-Transaktionen; in finally zuverlaessig freigegeben.
        conn.execute(
            "SELECT pg_advisory_lock(%s, hashtext(current_database()))",
            (MIGRATION_LOCK_CLASS,),
        )
        try:
            with conn.transaction():
                conn.execute(_SCHEMA_MIGRATIONS)
            done = _applied(conn)
            for path in files:
                version = path.stem  # z. B. "0001_init"
                if version in done:
                    continue
                sql = path.read_text(encoding="utf-8")
                try:
                    with conn.transaction():  # Migration + Vermerk atomar
                        conn.execute(sql)
                        conn.execute(
                            "INSERT INTO schema_migrations (version) VALUES (%s)", (version,)
                        )
                except psycopg.Error as exc:
                    raise RuntimeError(
                        f"migration {version} failed: {exc.__class__.__name__}"
                    ) from exc
                applied_now.append(version)
        finally:
            conn.execute(
                "SELECT pg_advisory_unlock(%s, hashtext(current_database()))",
                (MIGRATION_LOCK_CLASS,),
            )
    return applied_now


def _main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Apply versioned SQL migrations.")
    # Optional: ohne --database-url wird DATABASE_URL aus der Umgebung genommen,
    # damit das Passwort NICHT als CLI-Argument im Process-Listing erscheint.
    ap.add_argument("--database-url", default=None)
    ap.add_argument("--migrations-dir", required=True)
    args = ap.parse_args(argv)
    database_url = args.database_url or os.environ.get("DATABASE_URL")
    if not database_url:
        print("error: provide --database-url or set DATABASE_URL", file=sys.stderr)
        return 2
    applied = run(database_url, args.migrations_dir)
    print("applied:", ", ".join(applied) if applied else "(up to date)")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
