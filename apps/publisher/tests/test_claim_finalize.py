"""DB-/Integrationstests: Migration 0004, Lease-Claiming, Fencing-Finalisierung.

Treibt app.db direkt als Rolle inventory_publisher gegen echte ephemere PostgreSQL.
"""
from __future__ import annotations

import psycopg
import pytest
from app import db


def _owner(suffix="a"):
    return ("owner-" + suffix).ljust(12, "x")


# --- Migration 0004 ---------------------------------------------------------

def test_migration_0004_columns_and_constraints(outbox_db):
    with psycopg.connect(outbox_db["admin"]) as c:
        cols = {r[0] for r in c.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name='event_outbox'"
        ).fetchall()}
        assert {"claimed_at", "claim_owner"} <= cols
        cons = {r[0] for r in c.execute(
            "SELECT conname FROM pg_constraint WHERE conrelid='event_outbox'::regclass"
        ).fetchall()}
        assert {"event_outbox_claim_pair_chk", "event_outbox_published_no_claim_chk",
                "event_outbox_claim_owner_len_chk"} <= cons


def test_existing_pending_rows_remain_valid(outbox_db, seed_pending):
    eids = seed_pending(2)
    assert len(eids) == 2
    with psycopg.connect(outbox_db["admin"]) as c:
        n = c.execute("SELECT count(*) FROM event_outbox WHERE status='pending' "
                      "AND claimed_at IS NULL AND claim_owner IS NULL").fetchone()[0]
    assert n == 2  # Backfill-/Bestandszeilen unveraendert gueltig


def test_schema_check_passes_with_0004(outbox_db, pub_conn):
    db.check_schema(pub_conn())  # darf nicht werfen


# --- Claiming ---------------------------------------------------------------

def test_claim_sorts_and_limits(outbox_db, seed_pending, pub_conn):
    seed_pending(5)
    rows = db.claim_batch(pub_conn(), batch_size=3, lease_seconds=60, owner=_owner())
    assert len(rows) == 3  # auf batch_size begrenzt
    # vollstaendige Envelope-Spalten + Fencing-Felder vorhanden
    assert {"event_id", "event_type", "schema_version", "occurred_at", "source",
            "payload", "attempt_count", "claimed_at"} <= set(rows[0])


def test_claim_sets_lease_and_attempt(outbox_db, seed_pending, pub_conn):
    seed_pending(1)
    rows = db.claim_batch(pub_conn(), batch_size=5, lease_seconds=60, owner=_owner("z"))
    assert len(rows) == 1 and rows[0]["attempt_count"] == 1
    with psycopg.connect(outbox_db["admin"]) as c:
        r = c.execute("SELECT status, claim_owner, claimed_at, available_at > now() AS leased "
                      "FROM event_outbox").fetchone()
    assert r[0] == "pending" and r[1] == _owner("z") and r[2] is not None and r[3] is True


def test_active_lease_blocks_second_claim(outbox_db, seed_pending, pub_conn):
    seed_pending(1)
    first = db.claim_batch(pub_conn(), batch_size=5, lease_seconds=60, owner=_owner("1"))
    assert len(first) == 1
    second = db.claim_batch(pub_conn(), batch_size=5, lease_seconds=60, owner=_owner("2"))
    assert second == []  # available_at in Zukunft -> nicht erneut claimbar


def test_expired_lease_reclaimable(outbox_db, seed_pending, pub_conn):
    eids = seed_pending(1)
    db.claim_batch(pub_conn(), batch_size=5, lease_seconds=60, owner=_owner("1"))
    # Lease kuenstlich ablaufen lassen (Admin schiebt available_at in die Vergangenheit).
    with psycopg.connect(outbox_db["admin"], autocommit=True) as c:
        c.execute("UPDATE event_outbox SET available_at = now() - interval '1 second' "
                  "WHERE event_id=%s", (eids[0],))
    again = db.claim_batch(pub_conn(), batch_size=5, lease_seconds=60, owner=_owner("2"))
    assert len(again) == 1 and again[0]["attempt_count"] == 2  # erneut claimbar, attempt steigt


def test_skip_locked_avoids_double_claim(outbox_db, seed_pending):
    seed_pending(2)
    # conn1 sperrt EINE Zeile (offene Transaktion), conn2 darf sie nicht mit-claimen.
    c1 = psycopg.connect(outbox_db["publisher"], autocommit=False)
    c2 = psycopg.connect(outbox_db["publisher"], autocommit=False, row_factory=psycopg.rows.dict_row)
    try:
        c1.execute("BEGIN")
        locked = c1.execute(
            "SELECT event_id FROM event_outbox WHERE status='pending' "
            "ORDER BY available_at, created_at, event_id FOR UPDATE SKIP LOCKED LIMIT 1"
        ).fetchone()[0]
        rows = db.claim_batch(c2, batch_size=5, lease_seconds=60, owner=_owner("2"))
        got = {r["event_id"] for r in rows}
        assert str(locked) not in {str(x) for x in got}  # gesperrte Zeile uebersprungen
        assert len(rows) == 1
    finally:
        c1.rollback(); c1.close(); c2.close()


# --- Finalisierung + Fencing ------------------------------------------------

def test_finalize_success(outbox_db, seed_pending, pub_conn):
    seed_pending(1)
    conn = pub_conn()
    row = db.claim_batch(conn, batch_size=1, lease_seconds=60, owner=_owner("ok"))[0]
    n = db.finalize_success(conn, event_id=row["event_id"], owner=_owner("ok"),
                            claimed_at=row["claimed_at"])
    assert n == 1
    with psycopg.connect(outbox_db["admin"]) as c:
        r = c.execute("SELECT status, published_at, claimed_at, claim_owner, last_error "
                      "FROM event_outbox").fetchone()
    assert r[0] == "published" and r[1] is not None and r[2] is None and r[3] is None and r[4] is None


def test_finalize_failure_backoff(outbox_db, seed_pending, pub_conn):
    seed_pending(1)
    conn = pub_conn()
    row = db.claim_batch(conn, batch_size=1, lease_seconds=60, owner=_owner("f"))[0]
    n = db.finalize_failure(conn, event_id=row["event_id"], owner=_owner("f"),
                            claimed_at=row["claimed_at"], backoff_seconds=120, error_code="ClientError")
    assert n == 1
    with psycopg.connect(outbox_db["admin"]) as c:
        r = c.execute("SELECT status, last_error, claim_owner, claimed_at, available_at > now() AS backed "
                      "FROM event_outbox").fetchone()
    assert r[0] == "pending" and r[1] == "ClientError" and r[2] is None and r[3] is None and r[4] is True


def test_fencing_stale_finalize_changes_nothing(outbox_db, seed_pending, pub_conn):
    # Publish ok, aber Finalisierung mit falschem owner/claimed_at -> 0 Zeilen, Zeile bleibt retryfaehig.
    seed_pending(1)
    conn = pub_conn()
    row = db.claim_batch(conn, batch_size=1, lease_seconds=60, owner=_owner("real"))[0]
    n = db.finalize_success(conn, event_id=row["event_id"], owner=_owner("stale"),
                            claimed_at=row["claimed_at"])
    assert n == 0  # Fencing-Konflikt
    with psycopg.connect(outbox_db["admin"]) as c:
        status = c.execute("SELECT status FROM event_outbox").fetchone()[0]
    assert status == "pending"  # nicht ueberschrieben -> spaeter erneut claimbar


def test_reclaim_then_stale_owner_cannot_finalize(outbox_db, seed_pending, pub_conn):
    # Owner A claimt, Lease laeuft ab, Owner B re-claimt; A darf NICHT finalisieren.
    eids = seed_pending(1)
    cA = pub_conn()
    rowA = db.claim_batch(cA, batch_size=1, lease_seconds=60, owner=_owner("A"))[0]
    with psycopg.connect(outbox_db["admin"], autocommit=True) as c:
        c.execute("UPDATE event_outbox SET available_at = now() - interval '1 second' WHERE event_id=%s", (eids[0],))
    cB = pub_conn()
    db.claim_batch(cB, batch_size=1, lease_seconds=60, owner=_owner("B"))
    n = db.finalize_success(cA, event_id=rowA["event_id"], owner=_owner("A"), claimed_at=rowA["claimed_at"])
    assert n == 0  # A ist gefenced


def test_counts_and_oldest(outbox_db, seed_pending, pub_conn):
    seed_pending(3)
    conn = pub_conn()
    c = db.counts(conn)
    assert c["available_pending"] == 3 and c["claimed"] == 0
    assert db.oldest_available_age_seconds(conn) >= 0.0
