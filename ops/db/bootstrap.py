"""Idempotenter Rollen-Bootstrap fuer das Lab.

Erzeugt/aktualisiert die festen Datenbankrollen mit Least-Privilege. Wird mit dem
Cluster-Admin-Konto ausgefuehrt (nur fuer Rollen-/DB-Vorbereitung). Die eigentlichen
Schema-Migrationen laufen anschliessend als jeweilige *_admin-Rolle.

Sicherheit:
- Rollennamen sind feste, vertrauenswuerdige Konstanten (sichere SQL-Komposition).
- Passwoerter ausschliesslich aus Environment, nie im Repo, nie als CLI-Argument,
  nie im Log/stdout. Ohne gesetztes Passwort wird die Rolle ohne Passwort angelegt
  (Lab: trust/peer) — Base64/k8s-Secret ist kein externer Secret-Manager (Lab-Grenze).
"""
from __future__ import annotations

import os
import sys

import psycopg
from psycopg import sql

# Feste Rollen. Alle Runtime- UND Admin-Rollen sind bewusst keine Cluster-
# Superuser und duerfen keine Rollen/DBs anlegen oder RLS umgehen.
RUNTIME_ROLES = ("consumer_app", "inventory_app")
ADMIN_ROLES = ("consumer_admin", "inventory_admin")
ALL_ROLES = ADMIN_ROLES + RUNTIME_ROLES

_ATTRS = "LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS"

# Offensichtliche Platzhalter, die fuer eine reale Ausfuehrung nicht erlaubt sind.
_PLACEHOLDERS = {"change-me", "changeme", "change-me-local", "changeme123", "password", "secret"}


def _validate_password(role: str, pw: str | None) -> str:
    if pw is None or not pw.strip():
        raise ValueError(f"empty password provided for role {role}")
    if pw.strip().lower() in _PLACEHOLDERS:
        raise ValueError(f"placeholder password not allowed for role {role}")
    return pw


def ensure_roles(admin_dsn: str, passwords: dict[str, str] | None = None) -> list[str]:
    """Legt fehlende Rollen an und richtet vorhandene idempotent neu aus.

    Gibt die Liste neu erstellter Rollen zurueck. Gibt keine Secrets aus.
    Bereitgestellte Passwoerter werden validiert (kein Leerwert, kein Platzhalter).
    """
    passwords = passwords or {}
    for role, pw in passwords.items():
        _validate_password(role, pw)
    created: list[str] = []
    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        for role in ALL_ROLES:
            exists = conn.execute(
                "SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)
            ).fetchone()
            ident = sql.Identifier(role)
            pw = passwords.get(role)
            if not exists:
                stmt = sql.SQL("CREATE ROLE {} " + _ATTRS).format(ident)
                if pw:
                    stmt = sql.SQL("CREATE ROLE {} " + _ATTRS + " PASSWORD {}").format(
                        ident, sql.Literal(pw)
                    )
                conn.execute(stmt)
                created.append(role)
            else:
                # vorhandene Rolle kontrolliert auf Least-Privilege ausrichten
                conn.execute(sql.SQL("ALTER ROLE {} " + _ATTRS).format(ident))
                if pw:
                    conn.execute(
                        sql.SQL("ALTER ROLE {} PASSWORD {}").format(ident, sql.Literal(pw))
                    )
    return created


def _passwords_from_env(env: dict | None = None) -> dict[str, str]:
    src = os.environ if env is None else env
    out: dict[str, str] = {}
    for role in ALL_ROLES:
        key = role.upper() + "_PASSWORD"
        if key in src:
            # gesetzt -> uebernehmen (auch leer); ensure_roles validiert und lehnt
            # Leerwerte/Platzhalter ab. Nicht gesetzt -> kein Passwort (Lab: trust).
            out[role] = src[key]
    return out


def _main(argv: list[str]) -> int:
    admin_dsn = os.environ.get("PG_ADMIN_DSN")
    if not admin_dsn:
        # Sicherer Abbruch bei fehlender Environment-Konfiguration.
        print("error: PG_ADMIN_DSN not set", file=sys.stderr)
        return 2
    created = ensure_roles(admin_dsn, _passwords_from_env())
    # Nur Rollennamen ausgeben, niemals Secrets.
    print("roles created:", ", ".join(created) if created else "(none, already present)")
    print("roles ensured:", ", ".join(ALL_ROLES))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
