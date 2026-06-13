"""Postgres-Zugriff via psycopg3 mit Connection-Pool (synchron).

Synchrone Handler laufen in FastAPIs Threadpool — bewusst einfach.
Schema wird idempotent beim Start angelegt; produktiv uebernaehmen das
Migrationen (Flyway/alembic) — fuer das Lab reicht CREATE TABLE IF NOT EXISTS.
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

SCHEMA = """
CREATE TABLE IF NOT EXISTS stock_movements (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    sku         TEXT        NOT NULL,
    quantity    INTEGER     NOT NULL,
    warehouse   TEXT        NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


def ensure_schema() -> None:
    with pool.connection() as conn:
        conn.execute(SCHEMA)


def insert_movement(sku: str, quantity: int, warehouse: str) -> dict:
    with pool.connection() as conn:
        return conn.execute(
            """
            INSERT INTO stock_movements (sku, quantity, warehouse)
            VALUES (%s, %s, %s)
            RETURNING id, sku, quantity, warehouse, created_at
            """,
            (sku, quantity, warehouse),
        ).fetchone()


def list_movements(limit: int) -> list[dict]:
    with pool.connection() as conn:
        return conn.execute(
            """
            SELECT id, sku, quantity, warehouse, created_at
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
