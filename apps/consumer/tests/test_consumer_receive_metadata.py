"""Gate D2: ApproximateReceiveCount, DLQ-Tiefe und Runtime-Metriken.

Treibt SqsConsumer mit Fake-SQS-Client + echter ephemerer PostgreSQL (consumer_db).
Beweist: Receive Count wird angefordert, robust geparst, niedrig-kardinal als Metrik
gefuehrt; DLQ-Tiefe wird nur bei konfigurierter DLQ-URL gemeldet; die genannten
Metriken werden im echten Runtime-Pfad aufgerufen.
"""
from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "host=/nonexistent user=x dbname=x")
os.environ.setdefault("SQS_QUEUE_URL", "http://localhost:9324/q")

import psycopg
import pytest
from app import metrics as m
from app.consumer import SqsConsumer
from app.store import Outcome

import helpers


class FakeClient:
    def __init__(self, depths=None):
        self.deleted = []
        self.receive_kwargs = None
        self._depths = depths or {}

    def receive_message(self, **kw):
        self.receive_kwargs = kw
        return {}

    def delete_message(self, QueueUrl, ReceiptHandle):  # noqa: N803
        self.deleted.append(ReceiptHandle)

    def get_queue_attributes(self, QueueUrl, AttributeNames):  # noqa: N803
        n = self._depths.get(QueueUrl, 0)
        return {"Attributes": {"ApproximateNumberOfMessages": str(n)}}


class _OneShotReceiveClient(FakeClient):
    """receive_message records kwargs, stoppt den Loop und liefert leer."""

    def __init__(self, consumer):
        super().__init__()
        self._consumer = consumer

    def receive_message(self, **kw):
        self.receive_kwargs = kw
        self._consumer._stop.set()
        return {}


def _msg(raw: bytes, receipt="rh-1", msg_id="m-1", receive_count=None) -> dict:
    msg = {"Body": raw.decode("utf-8"), "ReceiptHandle": receipt, "MessageId": msg_id}
    if receive_count is not None:
        msg["Attributes"] = {"ApproximateReceiveCount": str(receive_count)}
    return msg


@pytest.fixture
def make_consumer(consumer_db):
    created = []

    def _make(open_pool=True, **kw):
        c = SqsConsumer(database_url=consumer_db["app"], queue_url="http://localhost/main", **kw)
        created.append(c)
        if open_pool:
            c.open_pool()
        return c

    yield _make
    for c in created:
        try:
            c.stop()
        except Exception:
            pass
        try:
            c._pool.close()
        except Exception:
            pass


# --- Receive Count ----------------------------------------------------------

def test_receive_requests_approximate_receive_count(make_consumer):
    c = make_consumer(open_pool=False)
    c.verify_schema()
    client = _OneShotReceiveClient(c)
    c._client = lambda: client
    c._run()
    assert "ApproximateReceiveCount" in client.receive_kwargs.get("AttributeNames", [])


def test_missing_receive_count_is_no_error(make_consumer, consumer_db):
    c = make_consumer()
    out = c._process_message(FakeClient(), _msg(helpers.body()))  # keine Attributes
    assert out is Outcome.APPLIED  # kein Fehler trotz fehlendem Receive Count


def test_redelivery_increments_counter_and_gauge(make_consumer):
    c = make_consumer()
    before = m.REDELIVERIES._value.get()
    c._process_message(FakeClient(), _msg(helpers.body(), receive_count=3))
    assert m.REDELIVERIES._value.get() == before + 1
    assert m.LAST_RECEIVE_COUNT._value.get() == 3


def test_first_delivery_does_not_count_as_redelivery(make_consumer):
    c = make_consumer()
    before = m.REDELIVERIES._value.get()
    c._process_message(FakeClient(), _msg(helpers.event(movement_id=99), receive_count=1))
    assert m.REDELIVERIES._value.get() == before  # rc=1 ist keine Redelivery


def test_receive_count_metrics_are_low_cardinality():
    # KEINE Labels -> keine hohe Kardinalitaet (kein event_id/message_id als Label).
    assert m.LAST_RECEIVE_COUNT._labelnames == ()
    assert m.REDELIVERIES._labelnames == ()


# --- DLQ-Tiefe --------------------------------------------------------------

def test_dlq_depth_reported_only_when_configured(make_consumer):
    depths = {"http://localhost/main": 4, "http://localhost/dlq": 2}
    # ohne DLQ-URL: nur Main-Tiefe
    c1 = make_consumer()
    c1._update_depth(FakeClient(depths=depths))
    assert m.QUEUE_DEPTH._value.get() == 4
    # mit DLQ-URL: zusaetzlich DLQ-Tiefe
    c2 = make_consumer(dlq_queue_url="http://localhost/dlq")
    c2._update_depth(FakeClient(depths=depths))
    assert m.DLQ_DEPTH._value.get() == 2


# --- GetQueueAttributes: entkoppelt, begrenzt, fehlertolerant ----------------

class _RaisingAttrsClient(FakeClient):
    def get_queue_attributes(self, QueueUrl, AttributeNames):  # noqa: N803
        raise RuntimeError("elasticmq unreachable")


def test_queue_attr_error_does_not_raise_and_keeps_last_gauge(make_consumer):
    c = make_consumer()
    # erst gueltige Tiefe setzen ...
    c._update_depth(FakeClient(depths={"http://localhost/main": 7}))
    assert m.QUEUE_DEPTH._value.get() == 7
    before_err = m.QUEUE_ATTR_ERRORS._value.get()
    # ... dann Fehler: kein Exception, Fehler-Metrik +1, letzter Gauge-Wert bleibt.
    c._update_depth(_RaisingAttrsClient())
    assert m.QUEUE_ATTR_ERRORS._value.get() == before_err + 1
    assert m.QUEUE_DEPTH._value.get() == 7


def test_metrics_endpoint_ok_even_if_queue_attrs_fail(monkeypatch):
    import app.main as main_mod

    class _Fake:
        healthy = True
        ready = True

    monkeypatch.setattr(main_mod, "consumer", _Fake())
    # /metrics ruft GetQueueAttributes NICHT auf (entkoppelt vom Poll-Loop) -> 200.
    assert main_mod.metrics().status_code == 200


def test_attrs_client_uses_short_timeouts(make_consumer):
    c = make_consumer(open_pool=False)
    client = c._attrs_client()
    assert client.meta.config.connect_timeout == 2
    assert client.meta.config.read_timeout == 3


# --- Metriken im Runtime-Pfad -----------------------------------------------

def test_runtime_metrics_called_on_apply(make_consumer):
    c = make_consumer()
    applied_before = m.EVENTS_APPLIED._value.get()
    c._process_message(FakeClient(), _msg(helpers.event(movement_id=4242)))
    assert m.EVENTS_APPLIED._value.get() == applied_before + 1
    assert c._last_success > 0


def test_live_ready_gauges_set_by_metrics_endpoint(monkeypatch):
    import app.main as main_mod

    class _Fake:
        healthy = True
        ready = False

    monkeypatch.setattr(main_mod, "consumer", _Fake())
    resp = main_mod.metrics()
    assert resp.status_code == 200
    assert m.CONSUMER_LIVE._value.get() == 1
    assert m.CONSUMER_READY._value.get() == 0


# --- Policy bleibt erhalten (kein Delete bei Fehlern/Konflikten) -------------

def test_validation_error_not_deleted(make_consumer):
    client = FakeClient()
    out = make_consumer()._process_message(client, _msg(helpers.body(event_id="bad")))
    assert out is Outcome.VALIDATION_ERROR
    assert client.deleted == []


def test_business_conflict_not_deleted(make_consumer):
    c = make_consumer()
    assert c._process_message(FakeClient(), _msg(helpers.event(movement_id=5, sku="AAA"))) is Outcome.APPLIED
    client = FakeClient()
    out = c._process_message(client, _msg(helpers.event(movement_id=5, sku="BBB"), receipt="rh-2"))
    assert out is Outcome.BUSINESS_CONFLICT
    assert client.deleted == []
