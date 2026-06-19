"""Eng begrenzte, transaktionale Eigentums-Uebertragung von Inventory-Objekten.

Upgrade-Pfad-Werkzeug (Cluster-Admin). Eine ALTE Installation kann Tabellen,
Sequenzen und Indizes besitzen, die einer Alt-Rolle gehoeren (z. B. dem frueheren
Bootstrap-Superuser `inventory`). Die versionierten Migrationen laufen als
`inventory_admin`; `ALTER TABLE`/`GRANT`/`REVOKE` setzen aber Objekt-Ownership
voraus. `db-prepare` aendert NUR den Datenbank-Owner. Dieses Werkzeug uebertraegt
die ALLOWLISTETEN Inventory-Objekte der Alt-Rolle GEZIELT an `inventory_admin`,
BEVOR `migrate` laeuft.

WARUM KEIN ``REASSIGN OWNED``:
    Ist die Alt-Rolle zugleich der Cluster-Bootstrap-Superuser, besitzt sie auch
    geteilte/cluster-weite Objekte (`postgres`, `template0`, `template1` sowie die
    Tablespaces `pg_default`/`pg_global`). ``REASSIGN OWNED BY`` wirkt cluster-weit
    auch auf diese geteilten Objekte und schlaegt fehl
    (``DependentObjectsStillExist``). Dieses Modul fuehrt ``REASSIGN OWNED`` daher
    NICHT aus, sondern ausschliesslich gezielte ``ALTER TABLE ... OWNER TO`` /
    ``ALTER SEQUENCE ... OWNER TO`` fuer die wenigen erwarteten Inventory-Objekte.

Sicherheit:
- Laeuft ausschliesslich in der Ziel-Datenbank (dbname wird gesetzt).
- Enumeriert ALLE Objekte der Alt-Rolle in der Ziel-DB und bricht VOR jeder Mutation
  ab, wenn ein unerwartetes Schema, ein unerwarteter Name oder ein unerwarteter
  Relationstyp auftaucht.
- Inspiziert geteilte Objekte (Datenbanken/Tablespaces) der Alt-Rolle nur zur
  Diagnose; veraendert sie NIE. Bricht ab, wenn die Alt-Rolle ein unerwartetes
  geteiltes Objekt besitzt.
- Fuehrt die gezielten ALTERs in EINER Transaktion aus, verifiziert vor dem Commit
  und rollt bei Verifikationsfehler zurueck.
- Idempotent: bereits korrekt -> No-op; fehlende Alt-Rolle -> No-op.
- Keine Connection-/Passwort-Ausgabe (nur Status/Objektnamen/Zahlen).
"""
from __future__ import annotations

import argparse
import os
import re
import sys

import psycopg
from psycopg import sql

# Erwartete Alt-Objekte eines Pre-2B-Standes: die Basistabelle plus ihre implizit
# miterzeugten Objekte (Primary-Key-Index, Identity-Sequenz) — alle mit Praefix
# "stock_movements" in Schema "public".
DEFAULT_REL_PATTERNS = (r"^stock_movements",)
DEFAULT_SCHEMAS = ("public",)
# Erlaubte Relationstypen: Tabelle (r), partitionierte Tabelle (p), Sequenz (S),
# Index (i). Alles andere -> Abbruch vor jeder Mutation.
ALLOWED_RELKINDS = ("r", "p", "S", "i")
# Per gezieltem ALTER behandelte Typen. Indizes (i) gehoeren IMPLIZIT dem Tabellen-
# Owner und werden NICHT direkt geaendert (ihr Owner folgt der Tabelle).
_TABLE_KINDS = ("r", "p")
_SEQUENCE_KINDS = ("S",)

# Vom Bootstrap-Superuser legitim besessene, geteilte Objekte. Diese werden nur
# inspiziert, niemals veraendert.
ALLOWED_SHARED_DATABASES = ("postgres", "template0", "template1")
ALLOWED_SHARED_TABLESPACES = ("pg_default", "pg_global")


def _role_exists(conn: psycopg.Connection, role: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)
    ).fetchone() is not None


def list_owned(conn: psycopg.Connection, role: str) -> dict:
    """Listet die der Rolle gehoerenden Relationen und Schemata in der AKTUELLEN DB."""
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


def list_shared_owned(conn: psycopg.Connection, role: str) -> dict:
    """Listet GETEILTE (cluster-weite) Objekte der Rolle: Datenbanken + Tablespaces.

    Rein lesend; dient nur der Diagnose/Absicherung. Es wird nichts veraendert.
    """
    dbs = conn.execute(
        "SELECT datname FROM pg_database WHERE pg_get_userbyid(datdba) = %s ORDER BY 1",
        (role,),
    ).fetchall()
    tbs = conn.execute(
        "SELECT spcname FROM pg_tablespace WHERE pg_get_userbyid(spcowner) = %s ORDER BY 1",
        (role,),
    ).fetchall()
    return {"databases": [d for (d,) in dbs], "tablespaces": [t for (t,) in tbs]}


def list_allowlisted_relations(
    conn: psycopg.Connection,
    rel_patterns: tuple[str, ...],
    allowed_schemas: tuple[str, ...],
) -> list[tuple[str, str, str, str]]:
    """Alle Relationen (beliebiger Owner) der Ziel-DB, die der Allowlist entsprechen.

    Rueckgabe: Liste (schema, name, relkind, owner). Fuer die Verifikation nach dem
    Owner-Wechsel (inkl. mitgezogener Indizes)."""
    rows = conn.execute(
        """
        SELECT n.nspname, c.relname, c.relkind, pg_get_userbyid(c.relowner)
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
          AND n.nspname !~ '^pg_toast'
        ORDER BY 1, 2
        """
    ).fetchall()
    compiled = [re.compile(p) for p in rel_patterns]
    return [
        (nsp, name, kind, owner)
        for nsp, name, kind, owner in rows
        if nsp in allowed_schemas and any(rx.match(name) for rx in compiled)
    ]


def _assert_relations_allowed(
    owned: dict,
    from_role: str,
    rel_patterns: tuple[str, ...],
    allowed_schemas: tuple[str, ...],
) -> None:
    compiled = [re.compile(p) for p in rel_patterns]
    for nsp, name, kind in owned["relations"]:
        if nsp not in allowed_schemas:
            raise RuntimeError(
                f"abort: {from_role} owns relation in unexpected schema {nsp}.{name} ({kind})"
            )
        if kind not in ALLOWED_RELKINDS:
            raise RuntimeError(
                f"abort: {from_role} owns object of unexpected relkind {nsp}.{name} ({kind})"
            )
        if not any(rx.match(name) for rx in compiled):
            raise RuntimeError(
                f"abort: {from_role} owns unexpected object {nsp}.{name} ({kind})"
            )
    for nsp in owned["schemas"]:
        if nsp not in allowed_schemas:
            raise RuntimeError(f"abort: {from_role} owns unexpected schema {nsp}")


def _assert_shared_allowed(shared: dict, from_role: str) -> None:
    for db in shared["databases"]:
        if db not in ALLOWED_SHARED_DATABASES:
            raise RuntimeError(
                f"abort: {from_role} owns unexpected shared database {db}"
            )
    for ts in shared["tablespaces"]:
        if ts not in ALLOWED_SHARED_TABLESPACES:
            raise RuntimeError(
                f"abort: {from_role} owns unexpected tablespace {ts}"
            )


def transfer_ownership(
    admin_dsn: str,
    database: str,
    from_role: str,
    to_role: str = "inventory_admin",
    rel_patterns: tuple[str, ...] = DEFAULT_REL_PATTERNS,
    allowed_schemas: tuple[str, ...] = DEFAULT_SCHEMAS,
) -> dict:
    """Uebertraegt die allowlisteten Inventory-Objekte gezielt von `from_role` auf
    `to_role` in `database`. Idempotent. KEIN ``REASSIGN OWNED``.

    Schritte: Pre-Checks (read-only) -> gezielte ALTERs in EINER Transaktion ->
    Verifikation vor Commit -> Rollback bei Verifikationsfehler.
    """
    if not database:
        raise ValueError("database must be set")
    if not from_role or not to_role:
        raise ValueError("from_role and to_role must be set")

    with psycopg.connect(admin_dsn, dbname=database, autocommit=True) as conn:
        # --- Pre-Checks (rein lesend) -----------------------------------------
        if not _role_exists(conn, to_role):
            raise RuntimeError(f"target role {to_role!r} does not exist (run bootstrap first)")
        if from_role == to_role:
            return {"status": "noop-same-role", "transferred": [], "shared": {"databases": [], "tablespaces": []}}
        if not _role_exists(conn, from_role):
            return {"status": "skipped-missing-source", "transferred": [], "shared": {"databases": [], "tablespaces": []}}

        # Geteilte Objekte nur inspizieren (Diagnose) — niemals veraendern.
        shared = list_shared_owned(conn, from_role)
        _assert_shared_allowed(shared, from_role)

        owned = list_owned(conn, from_role)
        _assert_relations_allowed(owned, from_role, rel_patterns, allowed_schemas)

        # Nur Tabellen/Sequenzen erhalten ein gezieltes ALTER; Indizes folgen der Tabelle.
        targets = [(nsp, name, kind) for nsp, name, kind in owned["relations"]
                   if kind in _TABLE_KINDS + _SEQUENCE_KINDS]

        # --- Mutation + Verifikation in EINER Transaktion ---------------------
        transferred: list[str] = []
        with conn.transaction():  # commit am Blockende; Exception -> Rollback
            for nsp, name, kind in targets:
                ident = sql.Identifier(nsp, name)
                role_ident = sql.Identifier(to_role)
                if kind in _TABLE_KINDS:
                    conn.execute(sql.SQL("ALTER TABLE {} OWNER TO {}").format(ident, role_ident))
                    transferred.append(f"ALTER TABLE {nsp}.{name} OWNER TO {to_role}")
                else:  # Sequenz
                    conn.execute(sql.SQL("ALTER SEQUENCE {} OWNER TO {}").format(ident, role_ident))
                    transferred.append(f"ALTER SEQUENCE {nsp}.{name} OWNER TO {to_role}")

            # Verifikation VOR Commit: keine allowlistete Relation mehr bei from_role,
            # und ALLE allowlisteten Relationen (inkl. mitgezogenem Index) bei to_role.
            allow = list_allowlisted_relations(conn, rel_patterns, allowed_schemas)
            still_source = [f"{n}.{nm}({k})" for n, nm, k, o in allow if o == from_role]
            not_target = [f"{n}.{nm}({k})->{o}" for n, nm, k, o in allow if o != to_role]
            if still_source or not_target:
                # -> Transaktion wird zurueckgerollt (Block verlassen mit Exception).
                raise RuntimeError(
                    "ownership verification failed (rolled back): "
                    f"still_source={still_source or '[]'}, not_target={not_target or '[]'}"
                )

        status = "transferred" if targets else "noop-already-correct"
        return {"status": status, "transferred": transferred, "shared": shared}


def _main(argv: list[str]) -> int:
    admin_dsn = os.environ.get("PG_ADMIN_DSN")
    if not admin_dsn:
        print("error: PG_ADMIN_DSN not set", file=sys.stderr)
        return 2
    ap = argparse.ArgumentParser(
        description="Targeted ownership transfer of inventory objects (no REASSIGN OWNED)."
    )
    ap.add_argument("--database", default=os.environ.get("INVENTORY_DB", "inventory"))
    ap.add_argument("--from-role", required=True)
    ap.add_argument("--to-role", default=os.environ.get("INVENTORY_DB_OWNER", "inventory_admin"))
    args = ap.parse_args(argv)
    try:
        result = transfer_ownership(admin_dsn, args.database, args.from_role, args.to_role)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    moved = "; ".join(result["transferred"]) if result["transferred"] else "(none)"
    shared = result.get("shared", {})
    shared_str = (
        "dbs=" + (",".join(shared.get("databases", [])) or "-")
        + " tablespaces=" + (",".join(shared.get("tablespaces", [])) or "-")
    )
    print(f"ownership-transfer {args.database}: {result['status']}; transferred=[{moved}]")
    print(f"shared objects inspected (untouched): {shared_str}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
