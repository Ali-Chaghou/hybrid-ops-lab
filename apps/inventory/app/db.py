"""Postgres-Zugriff via psycopg3 mit Connection-Pool (synchron).

Synchrone Handler laufen in FastAPIs Threadpool — bewusst einfach.
Das Schema wird NICHT mehr aus der Runtime erzeugt; es kommt ausschliesslich aus
den versionierten Migrationen (apps/inventory/migrations). Beim Start prueft die
App nur die Schema-Version (check_schema) und fuehrt keine DDL aus.
"""
from __future__ import annotations

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from app.config import settings

pool = ConnectionPool(
    conninfo=settings.database_url,
    min_size=settings.pool_min_size,
    max_size=settings.pool_max_size,
    kwargs={"row_factory": dict_row},
    open=False,
)

# Dem Inventory-Service bekannte Migrationen; EXPECTED = erwarteter Endstand.
KNOWN_MIGRATIONS = ("0001_create_stock_movements", "0002_add_stable_event_id")
EXPECTED_MIGRATION = KNOWN_MIGRATIONS[-1]


class SchemaNotReadyError(RuntimeError):
    """Schema nicht vorbereitet oder unbekannt neu — die App darf nicht starten."""


def check_schema() -> None:
    """Reine Schema-Pruefung beim Start — KEINE DDL, keine Reparatur, keine DSN-Ausgabe.

    Verweigert den Start bei fehlendem, zu altem oder unbekannt neuerem Schema.
    Die versionierten Migrationen muessen vorher ausgefuehrt worden sein.
    """
    with pool.connection() as conn:
        try:
            rows = conn.execute("SELECT version FROM schema_migrations").fetchall()
        except Exception:
            raise SchemaNotReadyError(
                "Das Datenbankschema ist nicht vorbereitet. Fuehre die versionierten "
                "Migrationen vor dem Start der Anwendung aus."
            ) from None
        applied = {r["version"] for r in rows}
        if EXPECTED_MIGRATION not in applied:
            raise SchemaNotReadyError(
                "Das Datenbankschema ist nicht vorbereitet. Fuehre die versionierten "
                "Migrationen vor dem Start der Anwendung aus."
            )
        unknown = applied - set(KNOWN_MIGRATIONS)
        if unknown:
            raise SchemaNotReadyError(
                "Unbekannter, neuerer Schemastand erkannt: " + ", ".join(sorted(unknown))
                + ". Die Anwendung ist fuer diesen Stand nicht freigegeben."
            )


def insert_movement(sku: str, quantity: int, warehouse: str) -> dict:
    # event_id ist additiv (neues Feld, persistiert per Default); bestehende Felder
    # bleiben unveraendert. Noch KEIN Event-Publishing — nur Rueckgabe der Zeile.
    with pool.connection() as conn:
        return conn.execute(
            """
            INSERT INTO stock_movements (sku, quantity, warehouse)
            VALUES (%s, %s, %s)
            RETURNING id, sku, quantity, warehouse, created_at, event_id
            """,
            (sku, quantity, warehouse),
        ).fetchone()


def list_movements(limit: int) -> list[dict]:
    with pool.connection() as conn:
        return conn.execute(
            """
            SELECT id, sku, quantity, warehouse, created_at, event_id
            FROM stock_movements
            ORDER BY id DESC
            LIMIT %s
            """,
            (limit,),
        ).fetchall()


def check_db() -> bool:
    try:
        with pool.connection() as conn:
            conn.execute("SELECT 1")
        return True
    except Exception:
        return False
