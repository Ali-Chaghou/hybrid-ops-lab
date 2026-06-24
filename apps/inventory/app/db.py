"""Postgres-Zugriff via psycopg3 mit Connection-Pool (synchron).

Synchrone Handler laufen in FastAPIs Threadpool — bewusst einfach.
Das Schema wird NICHT mehr aus der Runtime erzeugt; es kommt ausschliesslich aus
den versionierten Migrationen (apps/inventory/migrations). Beim Start prueft die
App nur die Schema-Version (check_schema) und fuehrt keine DDL aus.
"""
from __future__ import annotations

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from app.config import settings
from app.outbox import build_event

pool = ConnectionPool(
    conninfo=settings.database_url,
    min_size=settings.pool_min_size,
    max_size=settings.pool_max_size,
    kwargs={"row_factory": dict_row},
    open=False,
)

# Dem Inventory-Service bekannte Migrationen, in Reihenfolge.
KNOWN_MIGRATIONS = (
    "0001_create_stock_movements",
    "0002_add_stable_event_id",
    "0003_create_event_outbox",
    "0004_add_outbox_claim_fields",
)

# Interne Test-Seam (NUR fuer Integrationstests): ein optionaler Fault-Hook, der an
# definierten Punkten der Transaktion gezielt eine Exception werfen darf, um den
# atomaren Rollback nachzuweisen. Standardmaessig deaktiviert (None). Wird
# AUSSCHLIESSLICH von Tests gesetzt und ist vom HTTP-Endpunkt nicht erreichbar oder
# konfigurierbar.
_fault_hook = None  # callable(point: str) -> None | None


def _fire(point: str) -> None:
    if _fault_hook is not None:
        _fault_hook(point)


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
        # Es muessen ALLE bekannten Migrationen vorhanden sein, nicht nur die letzte —
        # fehlt irgendeine, ist das Schema unvollstaendig und der Start scheitert.
        missing = set(KNOWN_MIGRATIONS) - applied
        if missing:
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
    """Persistiert Movement UND passendes Outbox-Event in EINER Transaktion.

    Atomare Grenze: stock_movements und event_outbox committen gemeinsam oder gar
    nicht. Bei jedem Fehler vor dem Commit werden beide Inserts vollstaendig
    zurueckgerollt. KEIN Event-Publishing, KEINE Netzwerkoperation, KEINE DDL.
    Gibt das Movement in der bisherigen API-Struktur zurueck.
    """
    with pool.connection() as conn:
        with conn.transaction():  # commit am Block-Ende; jede Exception -> Rollback
            movement = conn.execute(
                """
                INSERT INTO stock_movements (sku, quantity, warehouse)
                VALUES (%s, %s, %s)
                RETURNING id, sku, quantity, warehouse, created_at, event_id
                """,
                (sku, quantity, warehouse),
            ).fetchone()

            _fire("after_movement_insert")  # Test-Seam (sonst No-op)

            event = build_event(movement)
            # occurred_at wird als der Movement-created_at-Zeitstempel selbst gespeichert
            # (kein String-Round-Trip) -> event_outbox.occurred_at == stock_movements.created_at.
            conn.execute(
                """
                INSERT INTO event_outbox
                    (event_id, movement_id, event_type, schema_version,
                     occurred_at, source, payload)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    event["event_id"],
                    movement["id"],
                    event["event_type"],
                    event["schema_version"],
                    movement["created_at"],
                    event["source"],
                    Jsonb(event["payload"]),
                ),
            )

            _fire("after_outbox_insert")  # Test-Seam (sonst No-op)
    return movement


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
