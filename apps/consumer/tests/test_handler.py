"""Per-Message-Handler: Outcomes, Failure-Injection, Delete-Fehler, Logs, Metriken."""
from __future__ import annotations

import logging

import psycopg
import pytest
from app import metrics as metrics_mod
from app.failure_injection import FailureInjector
from app.handler import handle_message
from app.store import Outcome

import helpers


class FakeMetrics:
    def __init__(self):
        self.calls = []
    def __getattr__(self, name):
        def rec(*args):
            self.calls.append((name, args))
        return rec


class Deleter:
    def __init__(self, fail=False):
        self.fail = fail
        self.deleted = []
    def __call__(self, env):
        if self.fail:
            raise RuntimeError("simulated delete failure")
        self.deleted.append(env.event_id)


def conn(dsn):
    return psycopg.connect(dsn, autocommit=False)


def test_apply_and_delete(consumer_db):
    m, d = FakeMetrics(), Deleter()
    with conn(consumer_db["app"]) as c:
        out = handle_message(helpers.body(), c, d, metrics=m)
    assert out is Outcome.APPLIED
    assert len(d.deleted) == 1
    names = [n for n, _ in m.calls]
    assert "received" in names and "applied" in names


def test_validation_error_no_delete(consumer_db):
    m, d = FakeMetrics(), Deleter()
    with conn(consumer_db["app"]) as c:
        out = handle_message(helpers.body(event_id="bad"), c, d, metrics=m)
    assert out is Outcome.VALIDATION_ERROR
    assert d.deleted == []
    assert ("validation_failure", ("bad_event_id",)) in m.calls


def test_transport_duplicate_deletes(consumer_db):
    raw = helpers.body()
    with conn(consumer_db["app"]) as c:
        handle_message(raw, c, Deleter())
    d = Deleter()
    with conn(consumer_db["app"]) as c:
        out = handle_message(raw, c, d, metrics=FakeMetrics())
    assert out is Outcome.TRANSPORT_DUPLICATE
    assert len(d.deleted) == 1


def test_failure_injection_after_commit(consumer_db):
    inj = FailureInjector(enabled=True)
    m, d = FakeMetrics(), Deleter()
    with conn(consumer_db["app"]) as c:
        out = handle_message(helpers.body(), c, d, injector=inj, metrics=m)
    assert out is Outcome.FAILURE_INJECTED
    assert d.deleted == []                      # NICHT geloescht
    assert ("failure_injection", ()) in m.calls
    # Effekt ist committed -> Projektion existiert (Commit vor Injection)
    with psycopg.connect(consumer_db["app"]) as c:
        assert c.execute("SELECT count(*) FROM movement_projection").fetchone()[0] == 1
    # Redelivery erkennt Duplicate, kein zweiter Effekt, jetzt loeschbar
    d2 = Deleter()
    with conn(consumer_db["app"]) as c:
        out2 = handle_message(helpers.body(), c, d2, injector=inj, metrics=FakeMetrics())
    assert out2 is Outcome.TRANSPORT_DUPLICATE and len(d2.deleted) == 1


def test_failure_injection_only_once_per_process(consumer_db):
    inj = FailureInjector(enabled=True)
    # zwei UNTERSCHIEDLICHE, je erstmalig angewandte Events (je frische event_id)
    with conn(consumer_db["app"]) as c:
        o1 = handle_message(helpers.event(movement_id=1), c, Deleter(), injector=inj)
    with conn(consumer_db["app"]) as c:
        o2 = handle_message(helpers.event(movement_id=2), c, Deleter(), injector=inj)
    assert o1 is Outcome.FAILURE_INJECTED          # nur das erste
    assert o2 is Outcome.APPLIED                    # one-shot verbraucht


def test_failure_injection_not_consumed_by_duplicate(consumer_db):
    # Vorab anwenden OHNE Injection
    with conn(consumer_db["app"]) as c:
        handle_message(helpers.body(), c, Deleter())
    inj = FailureInjector(enabled=True)
    d = Deleter()
    with conn(consumer_db["app"]) as c:
        out = handle_message(helpers.body(), c, d, injector=inj)  # Duplicate
    assert out is Outcome.TRANSPORT_DUPLICATE and len(d.deleted) == 1
    # Injection noch scharf -> erstes echtes APPLIED loest sie aus (frische event_id)
    with conn(consumer_db["app"]) as c:
        out2 = handle_message(helpers.event(movement_id=42), c, Deleter(), injector=inj)
    assert out2 is Outcome.FAILURE_INJECTED


def test_delete_failure_after_commit(consumer_db):
    m = FakeMetrics()
    with conn(consumer_db["app"]) as c:
        out = handle_message(helpers.body(), c, Deleter(fail=True), metrics=m)
    assert out is Outcome.DELETE_FAILURE
    assert ("delete_failure", ()) in m.calls
    # Effekt persistiert; spaetere Redelivery ist idempotent
    with psycopg.connect(consumer_db["app"]) as c:
        assert c.execute("SELECT count(*) FROM movement_projection").fetchone()[0] == 1
    with conn(consumer_db["app"]) as c:
        out2 = handle_message(helpers.body(), c, Deleter())
    assert out2 is Outcome.TRANSPORT_DUPLICATE


def test_db_failure_no_delete(consumer_db):
    m, d = FakeMetrics(), Deleter()
    c = conn(consumer_db["app"])
    c.close()  # kaputte Verbindung
    out = handle_message(helpers.body(), c, d, metrics=m)
    assert out is Outcome.DB_FAILURE
    assert d.deleted == []
    assert ("database_failure", ()) in m.calls


def test_bad_message_does_not_block_next(consumer_db):
    # eine problematische Nachricht, danach eine gueltige — unabhaengig.
    with conn(consumer_db["app"]) as c:
        bad = handle_message(b"{not json", c, Deleter())
    with conn(consumer_db["app"]) as c:
        good = handle_message(helpers.body(), c, Deleter())
    assert bad is Outcome.VALIDATION_ERROR and good is Outcome.APPLIED


def test_metric_labels_are_low_cardinality():
    # Keine event_id/movement_id/correlation_id/payload als Label.
    assert metrics_mod.INTEGRITY_CONFLICTS._labelnames == ("kind",)
    assert metrics_mod.VALIDATION_FAILURES._labelnames == ("reason",)
    for counter in (metrics_mod.EVENTS_RECEIVED, metrics_mod.EVENTS_APPLIED,
                    metrics_mod.TRANSPORT_DUPLICATES, metrics_mod.BUSINESS_DUPLICATES,
                    metrics_mod.DATABASE_FAILURES, metrics_mod.FAILURE_INJECTIONS):
        assert counter._labelnames == ()


def test_logs_contain_no_payload(consumer_db, caplog):
    with caplog.at_level(logging.INFO, logger="consumer.handler"):
        with conn(consumer_db["app"]) as c:
            handle_message(helpers.body(), c, Deleter())
    text = caplog.text
    assert "ABC-123" not in text and "WH-01" not in text   # kein Payload
    assert "password" not in text.lower() and "dbname=" not in text  # keine Secrets/DSN
