"""Datenbankzugriff des Publishers (psycopg3).

Lease-basiertes Claiming + Fencing-Finalisierung gegen `event_outbox`. Alle
Zeitwerte stammen aus der PostgreSQL-DB-Zeit (`now()` = transaction_timestamp),
nicht aus der lokalen Systemzeit. Der Publisher verbindet als Rolle
`inventory_publisher` (Least-Privilege): SELECT auf event_outbox + spaltenweises
UPDATE der Statusfelder; KEIN INSERT/DELETE, keine DDL, kein stock_movements.

Funktionen nehmen eine psycopg-Connection entgegen (autocommit=False), damit sie
mit dem vorhandenen ephemeren PostgreSQL-Testmuster direkt testbar sind.
"""
from __future__ import annotations

import psycopg
from psycopg.rows import dict_row, scalar_row, tuple_row

REQUIRED_MIGRATION = "0004_add_outbox_claim_fields"


class SchemaNotReadyError(RuntimeError):
    """Schema ohne Publisher-Migration (0004) — der Publisher darf nicht arbeiten."""


def check_schema(conn: psycopg.Connection) -> None:
    """Verweigert Betrieb, wenn Migration 0004 nicht angewandt ist (fail closed)."""
    try:
        cur = conn.cursor(row_factory=scalar_row)
        applied = set(cur.execute("SELECT version FROM schema_migrations").fetchall())
    except psycopg.Error:
        raise SchemaNotReadyError(
            "Das Datenbankschema ist nicht vorbereitet (Publisher-Migration fehlt)."
        ) from None
    if REQUIRED_MIGRATION not in applied:
        raise SchemaNotReadyError(
            "Das Datenbankschema ist nicht vorbereitet (Publisher-Migration fehlt)."
        )


def claim_batch(
    conn: psycopg.Connection, *, batch_size: int, lease_seconds: int, owner: str
) -> list[dict]:
    """Claimt bis zu `batch_size` faellige pending-Zeilen mit Lease (DB-Zeit).

    Auswahl: status='pending' AND available_at<=now(), stabil sortiert nach
    (available_at, created_at, event_id), `FOR UPDATE SKIP LOCKED`. Setzt in DERSELBEN
    kurzen Transaktion attempt_count+1, available_at=now()+lease, claimed_at=now(),
    claim_owner=owner und committet sofort. Gibt die vollstaendigen Envelope-Spalten
    (+ attempt_count, claimed_at fuers Fencing) zurueck.
    """
    with conn.transaction():
        cur = conn.cursor(row_factory=dict_row)
        rows = cur.execute(
            """
            WITH claimable AS (
                SELECT event_id
                FROM event_outbox
                WHERE status = 'pending' AND available_at <= now()
                ORDER BY available_at, created_at, event_id
                FOR UPDATE SKIP LOCKED
                LIMIT %(batch)s
            )
            UPDATE event_outbox o
               SET attempt_count = o.attempt_count + 1,
                   available_at  = now() + make_interval(secs => %(lease)s),
                   claimed_at    = now(),
                   claim_owner   = %(owner)s
              FROM claimable c
             WHERE o.event_id = c.event_id
            RETURNING o.event_id, o.event_type, o.schema_version, o.occurred_at,
                      o.source, o.payload, o.attempt_count, o.claimed_at
            """,
            {"batch": batch_size, "lease": lease_seconds, "owner": owner},
        ).fetchall()
    return rows


def finalize_success(
    conn: psycopg.Connection, *, event_id, owner: str, claimed_at
) -> int:
    """Markiert eine Zeile NACH bestaetigtem Publish als published (mit Fencing).

    Trifft nur die Zeile, die noch pending ist UND denselben claim_owner/claimed_at
    traegt (kein stale Worker). Gibt die Anzahl betroffener Zeilen zurueck (0 =
    Fencing-Konflikt). Loescht NICHTS.
    """
    with conn.transaction():
        cur = conn.execute(
            """
            UPDATE event_outbox
               SET status       = 'published',
                   published_at = now(),
                   last_error   = NULL,
                   claimed_at   = NULL,
                   claim_owner  = NULL,
                   available_at = now()
             WHERE event_id   = %(eid)s
               AND status     = 'pending'
               AND claim_owner = %(owner)s
               AND claimed_at  = %(claimed_at)s
            """,
            {"eid": event_id, "owner": owner, "claimed_at": claimed_at},
        )
        return cur.rowcount


def finalize_failure(
    conn: psycopg.Connection,
    *,
    event_id,
    owner: str,
    claimed_at,
    backoff_seconds: float,
    error_code: str,
) -> int:
    """Gibt eine Zeile nach bestaetigtem Publish-Fehler frei (pending, Backoff, Fencing).

    status bleibt 'pending'; available_at=now()+backoff; last_error=Code (begrenzt);
    Claim-Felder werden geloescht. Fencing wie bei finalize_success. Gibt die Anzahl
    betroffener Zeilen zurueck (0 = Fencing-Konflikt).
    """
    with conn.transaction():
        cur = conn.execute(
            """
            UPDATE event_outbox
               SET available_at = now() + make_interval(secs => %(backoff)s),
                   last_error   = %(err)s,
                   claimed_at   = NULL,
                   claim_owner  = NULL
             WHERE event_id    = %(eid)s
               AND status      = 'pending'
               AND claim_owner  = %(owner)s
               AND claimed_at   = %(claimed_at)s
            """,
            {
                "backoff": backoff_seconds,
                "err": error_code,
                "eid": event_id,
                "owner": owner,
                "claimed_at": claimed_at,
            },
        )
        return cur.rowcount


def counts(conn: psycopg.Connection) -> dict:
    """Backlog-Sicht: sofort publizierbare pending-Zeilen + aktuell geclaimte Zeilen."""
    cur = conn.cursor(row_factory=scalar_row)
    available = cur.execute(
        "SELECT count(*) FROM event_outbox WHERE status='pending' AND available_at <= now()"
    ).fetchone()
    claimed = cur.execute(
        "SELECT count(*) FROM event_outbox WHERE claim_owner IS NOT NULL"
    ).fetchone()
    return {"available_pending": available, "claimed": claimed}


def oldest_available_age_seconds(conn: psycopg.Connection) -> float:
    """Alter (s) der aeltesten sofort publizierbaren Zeile; 0.0 wenn keine."""
    cur = conn.cursor(row_factory=tuple_row)
    row = cur.execute(
        """
        SELECT EXTRACT(EPOCH FROM (now() - min(created_at)))
        FROM event_outbox
        WHERE status='pending' AND available_at <= now()
        """
    ).fetchone()
    return float(row[0]) if row and row[0] is not None else 0.0
