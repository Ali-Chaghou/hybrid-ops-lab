"""Idempotente, ABGESICHERTE Eigentums-Uebertragung bestehender Objekte.

Upgrade-Pfad-Werkzeug (Cluster-Admin). Eine ALTE Installation kann Tabellen,
Sequenzen oder Schemaobjekte besitzen, die einer Alt-Rolle (z. B. dem frueheren
Superuser/Owner `inventory`) gehoeren. Die versionierten Migrationen laufen als
`inventory_admin`; `ALTER TABLE`/`GRANT`/`REVOKE` setzen aber Ownership voraus.
`db-prepare` aendert NUR den Datenbank-Owner (`ALTER DATABASE ... OWNER`), nicht den
Owner bestehender Tabellen/Sequenzen. Dieses Werkzeug uebertraegt die Objekte der
Alt-Rolle KONTROLLIERT an `inventory_admin`, BEVOR `migrate` laeuft.

Sicherheit:
- Laeuft ausschliesslich in der Ziel-Datenbank (dbname wird gesetzt).
- UEBERTRAEGT NICHT BLIND: vor `REASSIGN OWNED` werden die der Alt-Rolle gehoerenden
  Objekte enumeriert und gegen eine Allowlist (Name-Patterns + Schemata) geprueft.
  Ein unerwartetes Objekt oder Schema fuehrt zum ABBRUCH, nicht zur stillen
  Uebertragung.
- Idempotent: fehlende Alt-Rolle -> No-op; Alt == Ziel -> No-op.
- Keine Connection-/Passwort-Ausgabe (nur Status/Objektnamen/Zahlen).
"""
from __future__ import annotations

import argparse
import os
import re
import sys

import psycopg
from psycopg import sql

# Erwartete Altobjekte eines Pre-2B-Standes: die Basistabelle plus ihre implizit
# miterzeugten Objekte (Primary-Key-Index, Identity-Sequenz) — alle mit Praefix
# "stock_movements". Schemata: nur "public".
DEFAULT_REL_PATTERNS = (r"^stock_movements",)
DEFAULT_SCHEMAS = ("public",)


def _role_exists(conn: psycopg.Connection, role: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)
    ).fetchone() is not None


def list_owned(conn: psycopg.Connection, role: str) -> dict:
    """Listet die der Rolle gehoerenden Relationen und Schemata (ausser System)."""
    relations = conn.execute(
        """
        SELECT n.nspname, c.relname, c.relkind
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        JOIN pg_roles r ON r.oid = c.relowner
        WHERE r.rolname = %s
          AND n.nspname NOT IN ('pg_catalog', 'information_schema')
          AND n.nspname !~ '^pg_toast'
        ORDER BY 1, 2
        """,
        (role,),
    ).fetchall()
    schemas = conn.execute(
        """
        SELECT n.nspname
        FROM pg_namespace n
        JOIN pg_roles r ON r.oid = n.nspowner
        WHERE r.rolname = %s
          AND n.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
        ORDER BY 1
        """,
        (role,),
    ).fetchall()
    return {
        "relations": [(s, nm, k) for s, nm, k in relations],
        "schemas": [s for (s,) in schemas],
    }


def _assert_allowed(owned: dict, from_role: str,
                    rel_patterns: tuple[str, ...], allowed_schemas: tuple[str, ...]) -> None:
    compiled = [re.compile(p) for p in rel_patterns]
    for nsp, name, kind in owned["relations"]:
        if nsp not in allowed_schemas:
            raise RuntimeError(
                f"abort: {from_role} owns relation in unexpected schema {nsp}.{name} ({kind})"
            )
        if not any(rx.match(name) for rx in compiled):
            raise RuntimeError(
                f"abort: {from_role} owns unexpected object {nsp}.{name} ({kind})"
            )
    for nsp in owned["schemas"]:
        if nsp not in allowed_schemas:
            raise RuntimeError(f"abort: {from_role} owns unexpected schema {nsp}")


def reassign_owned(
    admin_dsn: str,
    database: str,
    from_role: str,
    to_role: str = "inventory_admin",
    rel_patterns: tuple[str, ...] = DEFAULT_REL_PATTERNS,
    allowed_schemas: tuple[str, ...] = DEFAULT_SCHEMAS,
) -> dict:
    """Uebertraegt Objekte von `from_role` auf `to_role` in `database`. Idempotent.

    Bricht ab, wenn die Alt-Rolle Objekte ausserhalb der Allowlist besitzt.
    Rueckgabe-dict: status, objects_before/after, relations (uebertragene Namen).
    """
    if not database:
        raise ValueError("database must be set")
    if not from_role or not to_role:
        raise ValueError("from_role and to_role must be set")
    with psycopg.connect(admin_dsn, dbname=database, autocommit=True) as conn:
        if not _role_exists(conn, to_role):
            raise RuntimeError(f"target role {to_role!r} does not exist (run bootstrap first)")
        if from_role == to_role:
            return {"status": "noop-same-role", "objects_before": 0, "objects_after": 0, "relations": []}
        if not _role_exists(conn, from_role):
            return {"status": "skipped-missing-source", "objects_before": 0, "objects_after": 0, "relations": []}

        owned = list_owned(conn, from_role)
        _assert_allowed(owned, from_role, rel_patterns, allowed_schemas)
        before = len(owned["relations"])
        moved = [f"{nsp}.{name}" for nsp, name, _k in owned["relations"]]

        conn.execute(
            sql.SQL("REASSIGN OWNED BY {} TO {}").format(
                sql.Identifier(from_role), sql.Identifier(to_role)
            )
        )
        after = len(list_owned(conn, from_role)["relations"])
        return {
            "status": "reassigned",
            "objects_before": before,
            "objects_after": after,
            "relations": moved,
        }


def _main(argv: list[str]) -> int:
    admin_dsn = os.environ.get("PG_ADMIN_DSN")
    if not admin_dsn:
        print("error: PG_ADMIN_DSN not set", file=sys.stderr)
        return 2
    ap = argparse.ArgumentParser(
        description="Reassign existing objects to the inventory admin role (guarded upgrade path)."
    )
    ap.add_argument("--database", default=os.environ.get("INVENTORY_DB", "inventory"))
    ap.add_argument("--from-role", required=True)
    ap.add_argument("--to-role", default=os.environ.get("INVENTORY_DB_OWNER", "inventory_admin"))
    args = ap.parse_args(argv)
    try:
        result = reassign_owned(admin_dsn, args.database, args.from_role, args.to_role)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    moved = ", ".join(result["relations"]) if result["relations"] else "(none)"
    print(
        f"reassign {args.database}: {result['status']} "
        f"(objects_before={result['objects_before']}, objects_after={result['objects_after']}; "
        f"moved={moved})"
    )
    if result["status"] == "reassigned" and result["objects_after"] != 0:
        print("error: objects still owned by source role after reassign", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
