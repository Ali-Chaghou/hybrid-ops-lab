"""Runtime-Tests: Disabled-Verhalten, Publish-Flow (Fake-SQS), Lifecycle, Health, Logs."""
from __future__ import annotations

import json
import logging

import psycopg
import pytest
from app import metrics as m
from app.publisher import Publisher, publisher


class FakeSQS:
    def __init__(self, mode="ok"):
        self.mode = mode
        self.sent = []

    def send_message(self, QueueUrl, MessageBody):  # noqa: N803 (boto3-Signatur)
        if self.mode == "ok" or self.mode == "lost":
            self.sent.append(MessageBody)
        if self.mode == "timeout":
            raise TimeoutError("read timed out")
        if self.mode == "endpoint":
            raise ConnectionError("endpoint unreachable")
        if self.mode == "lost":
            # Queue hat angenommen, aber der Client sieht einen Fehler.
            raise ConnectionResetError("response lost")
        return {"MessageId": "fake"}


@pytest.fixture
def make_publisher(make_settings):
    built = []

    def make(**overrides):
        p = Publisher(config=make_settings(**overrides))
        built.append(p)
        return p

    yield make
    for p in built:
        try:
            p.stop()
        except Exception:
            pass
        if p._pool is not None:
            try:
                p._pool.close()
            except Exception:
                pass


def _status(outbox_db):
    with psycopg.connect(outbox_db["admin"]) as c:
        return c.execute("SELECT status, last_error, available_at > now() AS backed FROM event_outbox").fetchone()


# --- Default & Disabled -----------------------------------------------------

def test_module_publisher_disabled_by_default():
    assert publisher.enabled is False


def test_disabled_does_not_claim_or_connect(make_publisher, outbox_db, seed_pending, monkeypatch):
    seed_pending(1)
    p = make_publisher(enabled=False)
    # Falls trotzdem ein SQS-Client gebaut wuerde -> Test schlaegt fehl.
    monkeypatch.setattr(p, "_sqs_client", lambda: (_ for _ in ()).throw(AssertionError("client built")))
    p.start()  # No-op
    assert p._pool is None and p._thread is None
    assert p.enabled is False and p.live is True and p.ready is True
    # Outbox unveraendert pending.
    assert _status(outbox_db)[0] == "pending"


# --- Publish-Flow (Fake-SQS) ------------------------------------------------

def test_publish_success_marks_published(make_publisher, outbox_db, seed_pending):
    eids = seed_pending(1)
    p = make_publisher()
    p.verify_schema()
    client = FakeSQS("ok")
    p._poll_once(client)
    assert len(client.sent) == 1
    assert json.loads(client.sent[0])["event_id"] == eids[0]  # keine neue event_id
    assert _status(outbox_db)[0] == "published"


def test_publish_timeout_keeps_pending_with_backoff(make_publisher, outbox_db, seed_pending):
    seed_pending(1)
    p = make_publisher()
    p.verify_schema()
    p._poll_once(FakeSQS("timeout"))
    st = _status(outbox_db)
    assert st[0] == "pending" and st[1] == "TimeoutError" and st[2] is True


def test_publish_endpoint_error_keeps_pending(make_publisher, outbox_db, seed_pending):
    seed_pending(1)
    p = make_publisher()
    p.verify_schema()
    p._poll_once(FakeSQS("endpoint"))
    assert _status(outbox_db)[0] == "pending"


def test_lost_response_then_republish_same_envelope(make_publisher, outbox_db, seed_pending):
    eids = seed_pending(1)
    p = make_publisher()
    p.verify_schema()
    lost = FakeSQS("lost")
    p._poll_once(lost)                      # Queue nahm an, Client sah Fehler
    assert _status(outbox_db)[0] == "pending"   # bleibt retryfaehig
    # Lease/Backoff ablaufen lassen, dann erneut pollen -> Republish DESSELBEN Envelope.
    with psycopg.connect(outbox_db["admin"], autocommit=True) as c:
        c.execute("UPDATE event_outbox SET available_at = now() - interval '1 second'")
    ok = FakeSQS("ok")
    p._poll_once(ok)
    assert len(ok.sent) == 1
    assert json.loads(ok.sent[0])["event_id"] == eids[0]   # identische event_id
    assert _status(outbox_db)[0] == "published"


# --- Lifecycle / Health -----------------------------------------------------

def test_enabled_lifecycle_start_stop(make_publisher):
    p = make_publisher()  # leere DB -> Loop claimt nichts, idlet
    p.start()
    assert p.live is True
    p.stop()
    assert p.live is False and p._thread is None
    assert p._pool.closed is True


def test_ready_requires_schema(make_publisher):
    p = make_publisher()
    assert p.ready is False      # Schema noch nicht verifiziert
    p.verify_schema()
    assert p.ready is True


def test_double_start_no_second_poller(make_publisher):
    p = make_publisher()
    p.start()
    first = p._thread
    p.start()
    assert p._thread is first
    p.stop()


# --- Logs ohne Secrets ------------------------------------------------------

def test_logs_contain_no_payload_or_queue_url(make_publisher, outbox_db, seed_pending, caplog):
    seed_pending(1)
    p = make_publisher()
    p.verify_schema()
    with caplog.at_level(logging.INFO, logger="publisher"):
        p._poll_once(FakeSQS("ok"))
    text = caplog.text
    assert "SKU-" not in text                       # keine Payload-Fachdaten
    assert "http://localhost/queue" not in text     # keine Queue-URL
    assert "password" not in text.lower() and "dbname=" not in text  # keine DSN/Secrets


# --- Metriken im Runtime-Pfad ----------------------------------------------

def test_publish_success_metric_incremented(make_publisher, seed_pending):
    seed_pending(1)
    p = make_publisher()
    p.verify_schema()
    before = m.PUBLISH_SUCCESS._value.get()
    p._poll_once(FakeSQS("ok"))
    assert m.PUBLISH_SUCCESS._value.get() == before + 1
