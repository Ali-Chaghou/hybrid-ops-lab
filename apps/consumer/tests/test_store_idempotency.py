"""Transaktionale Idempotenz gegen echtes PostgreSQL (Integration)."""
from __future__ import annotations

import threading

import psycopg
import pytest
from app.envelope import validate
from app.store import Outcome, process_event

import helpers


def deliver(dsn: str, raw: bytes) -> Outcome:
    """Eine unabhaengige Zustellung: frische Connection (= wie ein neuer Receive)."""
    with psycopg.connect(dsn, autocommit=False) as conn:
        return process_event(conn, validate(raw))


def counts(dsn: str) -> dict:
    with psycopg.connect(dsn) as c:
        inbox = c.execute("SELECT count(*) FROM event_inbox").fetchone()[0]
        applied = c.execute("SELECT count(*) FROM event_inbox WHERE disposition='applied'").fetchone()[0]
        bdup = c.execute("SELECT count(*) FROM event_inbox WHERE disposition='business_duplicate'").fetchone()[0]
        proj = c.execute("SELECT count(*) FROM movement_projection").fetchone()[0]
    return {"inbox": inbox, "applied": applied, "business_duplicate": bdup, "projection": proj}


def test_normal_apply(consumer_db):
    dsn = consumer_db["app"]
    assert deliver(dsn, helpers.body()) is Outcome.APPLIED
    assert counts(dsn) == {"inbox": 1, "applied": 1, "business_duplicate": 0, "projection": 1}


def test_transport_duplicate(consumer_db):
    dsn = consumer_db["app"]
    raw = helpers.body()
    assert deliver(dsn, raw) is Outcome.APPLIED
    assert deliver(dsn, raw) is Outcome.TRANSPORT_DUPLICATE
    # genau ein Effekt, keine zweite Inbox-Zeile
    assert counts(dsn) == {"inbox": 1, "applied": 1, "business_duplicate": 0, "projection": 1}


def test_restart_persistence(consumer_db):
    dsn = consumer_db["app"]
    raw = helpers.body()
    assert deliver(dsn, raw) is Outcome.APPLIED
    # "Neustart": jede deliver() oeffnet ohnehin eine frische Connection.
    assert deliver(dsn, raw) is Outcome.TRANSPORT_DUPLICATE
    assert counts(dsn)["projection"] == 1


def test_business_duplicate(consumer_db):
    dsn = consumer_db["app"]
    e1 = helpers.body(event_id=helpers.new_uuid())                 # movement 123
    e2 = helpers.body(event_id=helpers.new_uuid())                 # andere ID, gleiches Movement + Daten
    assert deliver(dsn, e1) is Outcome.APPLIED
    assert deliver(dsn, e2) is Outcome.BUSINESS_DUPLICATE
    c = counts(dsn)
    assert c == {"inbox": 2, "applied": 1, "business_duplicate": 1, "projection": 1}
    # business_duplicate referenziert das angewandte Event
    with psycopg.connect(dsn) as conn:
        canon = conn.execute(
            "SELECT canonical_event_id FROM event_inbox WHERE disposition='business_duplicate'"
        ).fetchone()[0]
        applied_id = conn.execute(
            "SELECT event_id FROM event_inbox WHERE disposition='applied'"
        ).fetchone()[0]
    assert canon == applied_id


def test_event_id_conflict(consumer_db):
    # gleiche event_id fuer ein ANDERES Movement -> Fingerprint unterscheidet sich.
    dsn = consumer_db["app"]
    same_id = helpers.new_uuid()
    first = helpers.body(event_id=same_id)                                  # movement 123
    second = helpers.body_with_payload(movement_id=999, sku="ZZZ")
    import json
    second_obj = json.loads(second); second_obj["event_id"] = same_id
    second = json.dumps(second_obj).encode()
    assert deliver(dsn, first) is Outcome.APPLIED
    assert deliver(dsn, second) is Outcome.EVENT_ID_CONFLICT
    # kein Effekt, keine neue Zeile (Rollback laesst nichts zurueck)
    assert counts(dsn) == {"inbox": 1, "applied": 1, "business_duplicate": 0, "projection": 1}


def test_business_conflict(consumer_db):
    # andere event_id, gleiches Movement, VERAENDERTE Fachdaten -> kein Overwrite.
    dsn = consumer_db["app"]
    e1 = helpers.body(event_id=helpers.new_uuid())                          # movement 123, qty 5
    e2_raw = helpers.body_with_payload(quantity=999)
    import json
    e2_obj = json.loads(e2_raw); e2_obj["event_id"] = helpers.new_uuid()
    e2 = json.dumps(e2_obj).encode()
    assert deliver(dsn, e1) is Outcome.APPLIED
    assert deliver(dsn, e2) is Outcome.BUSINESS_CONFLICT
    assert counts(dsn) == {"inbox": 1, "applied": 1, "business_duplicate": 0, "projection": 1}
    with psycopg.connect(dsn) as conn:
        qty = conn.execute("SELECT quantity FROM movement_projection").fetchone()[0]
    assert qty == 5  # keine Ueberschreibung


def _run_parallel(dsn: str, bodies: list[bytes]) -> list[Outcome]:
    barrier = threading.Barrier(len(bodies))
    results: list[Outcome | None] = [None] * len(bodies)

    def worker(i: int, raw: bytes):
        with psycopg.connect(dsn, autocommit=False) as conn:
            barrier.wait()  # gemeinsamer Startpunkt -> echtes Rennen
            results[i] = process_event(conn, validate(raw))

    threads = [threading.Thread(target=worker, args=(i, b)) for i, b in enumerate(bodies)]
    for t in threads: t.start()
    for t in threads: t.join(timeout=30)
    return results  # type: ignore[return-value]


def test_parallel_same_event_id(consumer_db):
    dsn = consumer_db["app"]
    raw = helpers.body()
    outcomes = _run_parallel(dsn, [raw, raw])
    assert outcomes.count(Outcome.APPLIED) == 1
    assert outcomes.count(Outcome.TRANSPORT_DUPLICATE) == 1
    assert counts(dsn) == {"inbox": 1, "applied": 1, "business_duplicate": 0, "projection": 1}


def test_parallel_diff_event_id_same_movement(consumer_db):
    dsn = consumer_db["app"]
    a = helpers.body(event_id=helpers.new_uuid())
    b = helpers.body(event_id=helpers.new_uuid())
    outcomes = _run_parallel(dsn, [a, b])
    assert outcomes.count(Outcome.APPLIED) == 1
    assert outcomes.count(Outcome.BUSINESS_DUPLICATE) == 1
    assert counts(dsn) == {"inbox": 2, "applied": 1, "business_duplicate": 1, "projection": 1}


def test_db_failure_returns_error_and_no_state(consumer_db):
    # Verbindung zu nicht existenter DB -> OperationalError; kein Effekt im echten Schema.
    bad = consumer_db["app"].replace(consumer_db["name"], consumer_db["name"] + "_missing")
    with pytest.raises(psycopg.OperationalError):
        deliver(bad, helpers.body())
    assert counts(consumer_db["app"]) == {"inbox": 0, "applied": 0, "business_duplicate": 0, "projection": 0}
