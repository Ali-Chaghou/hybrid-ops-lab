"""Transaktionaler Idempotency-Store (Inbox + Projektion).

Atomare Grenze: fachlicher Effekt (`movement_projection`) und Idempotenznachweis
(`event_inbox`) liegen in DERSELBEN PostgreSQL-Transaktion. Die Queue-Nachricht
darf erst nach erfolgreichem Commit geloescht werden (das macht der Consumer-Loop
in Phase 2 — hier wird nur das DB-Ergebnis ermittelt).

Constraints, nicht die Anwendung, entscheiden Parallelrennen:
  - event_inbox.event_id PRIMARY KEY
  - movement_projection (source, source_movement_id) PRIMARY KEY
  - movement_projection.source_event_id UNIQUE + FK (DEFERRABLE INITIALLY DEFERRED)

Die Runtime-Rolle braucht nur SELECT/INSERT — kein UPDATE/DELETE.
"""
from __future__ import annotations

import enum

import psycopg
from psycopg import errors

from app.envelope import EVENT_TYPE, SCHEMA_VERSION, SOURCE, Envelope, fingerprint

# Dem Consumer bekannte Migrationen, in Reihenfolge. EXPECTED = der erwartete Endstand.
KNOWN_MIGRATIONS = ("0001_init",)
EXPECTED_MIGRATION = KNOWN_MIGRATIONS[-1]


class Outcome(str, enum.Enum):
    # Ergebnisse der DB-Verarbeitung (process_event):
    APPLIED = "applied"
    TRANSPORT_DUPLICATE = "transport_duplicate"
    BUSINESS_DUPLICATE = "business_duplicate"
    EVENT_ID_CONFLICT = "event_id_conflict"
    BUSINESS_CONFLICT = "business_conflict"
    DB_FAILURE = "db_failure"
    # Ergebnisse der Per-Message-Handler-Grenze:
    VALIDATION_ERROR = "validation_error"
    FAILURE_INJECTED = "failure_injected"
    DELETE_FAILURE = "delete_failure"


# Nur erfolgreich abgeschlossene Verarbeitungen duerfen die Queue-Nachricht loeschen.
_DELETABLE = {Outcome.APPLIED, Outcome.TRANSPORT_DUPLICATE, Outcome.BUSINESS_DUPLICATE}


def should_delete(outcome: Outcome) -> bool:
    return outcome in _DELETABLE


class SchemaVersionError(RuntimeError):
    """Erwartete Migration nicht angewandt — die Runtime-App darf so nicht starten."""


def check_schema_version(
    conn: psycopg.Connection,
    expected: str = EXPECTED_MIGRATION,
    known: tuple[str, ...] = KNOWN_MIGRATIONS,
) -> None:
    """Verweigert den Start bei nicht vorbereitetem ODER unbekannt neuerem Schema.

    Keine DDL, keine automatische Migration, keine Connection-URL in der Meldung.
    """
    try:
        rows = conn.execute("SELECT version FROM schema_migrations").fetchall()
    except psycopg.Error:
        raise SchemaVersionError(
            "Das Datenbankschema ist nicht vorbereitet. Fuehre die versionierten "
            "Migrationen vor dem Start der Anwendung aus."
        ) from None
    applied = {r[0] for r in rows}
    if expected not in applied:
        raise SchemaVersionError(
            "Das Datenbankschema ist nicht vorbereitet. Fuehre die versionierten "
            "Migrationen vor dem Start der Anwendung aus."
        )
    unknown = applied - set(known)
    if unknown:
        raise SchemaVersionError(
            "Unbekannter, neuerer Schemastand erkannt: "
            + ", ".join(sorted(unknown))
            + ". Die Anwendung ist fuer diesen Stand nicht freigegeben."
        )


def _classify_known_event_id(conn: psycopg.Connection, event_id, fp: str) -> Outcome:
    """Rollt die Transaktion zurueck (kein Teilzustand) und klassifiziert eine
    bereits bekannte event_id per Fingerprint: gleich -> Transport-Duplicate,
    verschieden -> Event-ID Integrity Conflict."""
    conn.rollback()
    prior = conn.execute(
        "SELECT fingerprint FROM event_inbox WHERE event_id = %s", (event_id,)
    ).fetchone()
    conn.rollback()  # Lese-Transaktion sauber beenden
    if prior is not None and prior[0] == fp:
        return Outcome.TRANSPORT_DUPLICATE
    return Outcome.EVENT_ID_CONFLICT


def process_event(conn: psycopg.Connection, env: Envelope) -> Outcome:
    """Verarbeitet ein bereits validiertes Event atomar. Erwartet autocommit=False.

    Reihenfolge exakt nach Vorgabe: Projektion -> Disposition -> Inbox -> Commit.
    Jeder Nicht-Erfolgspfad rollt die GESAMTE Transaktion zurueck (kein Teilzustand).
    """
    fp = fingerprint(env)
    try:
        # 4. Projektion einfuegen. Der ON-CONFLICT-Arbiter deckt nur den PK
        #    (source, source_movement_id) ab; die zweite UNIQUE (source_event_id)
        #    kann bei erneuter Zustellung DERSELBEN event_id zuschlagen — das ist
        #    ein event_id-Konflikt (sequenziell ODER parallel) und wird wie ein
        #    Inbox-Konflikt per Fingerprint klassifiziert.
        try:
            row = conn.execute(
                """
                INSERT INTO movement_projection
                    (source, source_movement_id, source_event_id, sku, quantity, warehouse, occurred_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (source, source_movement_id) DO NOTHING
                RETURNING source_event_id
                """,
                (env.source, env.movement_id, env.event_id, env.sku, env.quantity, env.warehouse, env.occurred_at),
            ).fetchone()
        except errors.UniqueViolation:
            return _classify_known_event_id(conn, env.event_id, fp)

        if row is not None:
            # 5. Projektion neu -> dieses Event erzeugt den Effekt.
            disposition, canonical = "applied", None
        else:
            # 6. Projektion existiert: bestehende lesen und fachlich vergleichen.
            existing = conn.execute(
                """
                SELECT source_event_id, sku, quantity, warehouse, occurred_at
                FROM movement_projection WHERE source = %s AND source_movement_id = %s
                """,
                (env.source, env.movement_id),
            ).fetchone()
            same = (
                existing[1] == env.sku
                and existing[2] == env.quantity
                and existing[3] == env.warehouse
                and existing[4] == env.occurred_at
            )
            if same:
                # 7. gleiches Movement, identische Fachdaten -> Business-Duplicate.
                disposition, canonical = "business_duplicate", existing[0]
            else:
                # 8. veraenderte Fachdaten -> Business Integrity Conflict, fail closed.
                conn.rollback()
                return Outcome.BUSINESS_CONFLICT

        # 9. Inbox-Zeile mit endgueltiger Disposition (PK event_id). source_movement_id
        #    wird mitgefuehrt; der zusammengesetzte FK erzwingt fuer business_duplicate,
        #    dass canonical_event_id auf die Projektion DESSELBEN Movements zeigt.
        ins = conn.execute(
            """
            INSERT INTO event_inbox
                (event_id, event_type, source, source_movement_id, schema_version,
                 fingerprint, disposition, canonical_event_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (event_id) DO NOTHING
            RETURNING event_id
            """,
            (env.event_id, EVENT_TYPE, SOURCE, env.movement_id, SCHEMA_VERSION, fp, disposition, canonical),
        ).fetchone()

        if ins is None:
            # 10. event_id existiert bereits -> gesamte Transaktion zuruecksetzen
            #     (entfernt auch eine evtl. neu eingefuegte Projektion), dann klassifizieren.
            return _classify_known_event_id(conn, env.event_id, fp)

        # 11./12. Projektion und Inbox konsistent -> Commit (deferred FK wird hier geprueft).
        conn.commit()
        return Outcome.APPLIED if disposition == "applied" else Outcome.BUSINESS_DUPLICATE

    except psycopg.Error:
        # 13. DB-Fehler: kein Erfolg, kein Teilzustand. Caller behandelt als DB_FAILURE.
        try:
            conn.rollback()
        except psycopg.Error:
            pass
        raise
