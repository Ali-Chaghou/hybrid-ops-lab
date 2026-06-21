"""Live-Consumer-Runtime: Verdrahtung von Poll-Loop + Pool -> bestehendem Handler.

Treibt SqsConsumer._process_message (und punktuell _run) mit einem Fake-SQS-Client
und einer echten, ephemeren PostgreSQL (consumer_db-Fixture). Beweist: Delete nur
nach loeschbarem Outcome, Idempotenz ueber event_id, kein Delete bei
Konflikt/Fehler/Injection, getrennte Pre-/Post-Commit-Fehlerfenster, Restart
zwischen Commit und Ack, Pool-Lifecycle/-Recovery, Readiness inkl. leerem/veraltetem
Poll, sowie Schema-Gate und Least-Privilege.

KEINE zweite Idempotenz-Implementierung: die Tests pruefen die Delegation an
app.handler/app.store, die andernorts eigenstaendig getestet sind.
"""
from __future__ import annotations

import os
import time

# Dummy-Env, damit app.config (von app.consumer importiert) ladebar ist. Die realen
# Werte werden pro Consumer-Instanz explizit injiziert.
os.environ.setdefault("DATABASE_URL", "host=/nonexistent user=x dbname=x")
os.environ.setdefault("SQS_QUEUE_URL", "http://localhost:9324/q")

import psycopg
import pytest
from app.config import Settings
from app.consumer import SqsConsumer
from app.failure_injection import FailureInjector
from app.handler import handle_message
from app.store import Outcome, SchemaVersionError

import helpers

_UNREACHABLE_DSN = "host=/nonexistent_socket_dir user=consumer_app dbname=nope"


class FakeClient:
    """Minimaler SQS-Stub: zaehlt geloeschte ReceiptHandles, kann Delete fehlschlagen lassen."""

    def __init__(self, delete_fails: bool = False) -> None:
        self.delete_fails = delete_fails
        self.deleted: list[str] = []

    def delete_message(self, QueueUrl, ReceiptHandle):  # noqa: N803 (boto3-Signatur)
        if self.delete_fails:
            raise RuntimeError("simulated delete failure")
        self.deleted.append(ReceiptHandle)


class EmptyPollClient:
    """receive_message liefert genau einmal leer und stoppt dann den Loop."""

    def __init__(self, consumer: SqsConsumer) -> None:
        self._consumer = consumer
        self.receive_calls = 0

    def receive_message(self, **kw):
        self.receive_calls += 1
        self._consumer._stop.set()
        return {}  # leerer Long-Poll

    def get_queue_attributes(self, **kw):
        return {"Attributes": {"ApproximateNumberOfMessages": "0"}}


class _FailingPool:
    """Fake-Pool, dessen getconn immer scheitert (echter getconn-/DB-Failure-Pfad)."""

    closed = False

    def getconn(self, timeout=None):
        raise RuntimeError("pool/getconn down")

    def putconn(self, conn):  # pragma: no cover
        pass

    def close(self):
        pass


class _ClosedConnPool:
    """Fake-Pool, der eine bereits geschlossene Verbindung liefert -> Handler DB_FAILURE."""

    closed = False

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn

    def getconn(self, timeout=None):
        conn = psycopg.connect(self._dsn, autocommit=False)
        conn.close()
        return conn

    def putconn(self, conn):
        pass

    def close(self):
        pass


def _msg(raw: bytes, receipt: str = "rh-1", msg_id: str = "m-1") -> dict:
    return {"Body": raw.decode("utf-8"), "ReceiptHandle": receipt, "MessageId": msg_id}


def _proj_count(dsn: str) -> int:
    with psycopg.connect(dsn) as c:
        return c.execute("SELECT count(*) FROM movement_projection").fetchone()[0]


def _inbox_count(dsn: str) -> int:
    with psycopg.connect(dsn) as c:
        return c.execute("SELECT count(*) FROM event_inbox").fetchone()[0]


@pytest.fixture
def make_consumer(consumer_db):
    """Factory fuer SqsConsumer gegen die migrierte Consumer-DB; raeumt Pools ab."""
    created: list[SqsConsumer] = []

    def _make(open_pool: bool = True, **kw) -> SqsConsumer:
        c = SqsConsumer(database_url=consumer_db["app"], queue_url="http://localhost/q", **kw)
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


# --- Happy path -------------------------------------------------------------

def test_applied_then_delete(make_consumer, consumer_db):
    c = make_consumer()
    client = FakeClient()
    out = c._process_message(client, _msg(helpers.body()))
    assert out is Outcome.APPLIED
    assert client.deleted == ["rh-1"]
    assert _proj_count(consumer_db["app"]) == 1
    assert c._last_success > 0


# --- Duplikate --------------------------------------------------------------

def test_transport_duplicate_deletes_no_second_projection(make_consumer, consumer_db):
    raw = helpers.body()
    assert make_consumer()._process_message(FakeClient(), _msg(raw)) is Outcome.APPLIED
    client = FakeClient()
    out = make_consumer()._process_message(client, _msg(raw, receipt="rh-2"))
    assert out is Outcome.TRANSPORT_DUPLICATE
    assert client.deleted == ["rh-2"]
    assert _proj_count(consumer_db["app"]) == 1


def test_business_duplicate_no_second_projection(make_consumer, consumer_db):
    # Gleiches Movement, identische Fachdaten, andere event_id -> Business-Duplicate.
    c = make_consumer()
    assert c._process_message(FakeClient(), _msg(helpers.event(movement_id=7))) is Outcome.APPLIED
    client = FakeClient()
    out = c._process_message(client, _msg(helpers.event(movement_id=7), receipt="rh-2"))
    assert out is Outcome.BUSINESS_DUPLICATE
    assert client.deleted == ["rh-2"]  # bewusst loeschbar (siehe docs/idempotency.md)
    assert _proj_count(consumer_db["app"]) == 1


# --- Konflikte (kein Delete) ------------------------------------------------

def test_event_id_conflict_not_deleted(make_consumer):
    eid = helpers.new_uuid()
    c = make_consumer()
    assert c._process_message(FakeClient(), _msg(helpers.event(event_id=eid, movement_id=1))) is Outcome.APPLIED
    client = FakeClient()
    out = c._process_message(client, _msg(helpers.event(event_id=eid, movement_id=2), receipt="rh-2"))
    assert out is Outcome.EVENT_ID_CONFLICT
    assert client.deleted == []


def test_business_conflict_not_deleted(make_consumer):
    c = make_consumer()
    assert c._process_message(FakeClient(), _msg(helpers.event(movement_id=5, sku="AAA"))) is Outcome.APPLIED
    client = FakeClient()
    out = c._process_message(client, _msg(helpers.event(movement_id=5, sku="BBB"), receipt="rh-2"))
    assert out is Outcome.BUSINESS_CONFLICT
    assert client.deleted == []


# --- Validierung / DB-Fehler (kein Delete) ----------------------------------

def test_invalid_envelope_not_deleted(make_consumer, consumer_db):
    client = FakeClient()
    out = make_consumer()._process_message(client, _msg(helpers.body(event_id="not-a-uuid")))
    assert out is Outcome.VALIDATION_ERROR
    assert client.deleted == []
    assert _proj_count(consumer_db["app"]) == 0


def test_getconn_failure_yields_db_failure_no_delete(make_consumer):
    # Echter getconn-Failure-Pfad ueber einen Fake-Pool (nicht nur _database_url tauschen,
    # das den bereits erzeugten Pool gar nicht beeinflusst).
    c = make_consumer()
    c._pool.close()
    c._pool = _FailingPool()
    client = FakeClient()
    out = c._process_message(client, _msg(helpers.body()))
    assert out is Outcome.DB_FAILURE
    assert client.deleted == []
    assert c._db_ok is False


def test_handler_db_failure_sets_db_not_ok_no_delete(make_consumer, consumer_db):
    # getconn liefert eine GESCHLOSSENE Verbindung -> Handler faengt psycopg.Error ->
    # DB_FAILURE; _db_ok MUSS False werden (nicht pauschal True nach handle_message).
    c = make_consumer()
    real_pool = c._pool
    c._pool = _ClosedConnPool(consumer_db["app"])
    client = FakeClient()
    out = c._process_message(client, _msg(helpers.body()))
    assert out is Outcome.DB_FAILURE
    assert client.deleted == []
    assert c._db_ok is False
    real_pool.close()


# --- Fehlerfenster 1: VOR Commit --------------------------------------------

def test_pre_commit_failure_leaves_no_partial_state(consumer_db):
    # Eine kaputte Verbindung -> store scheitert VOR dem Commit -> kein Teilzustand.
    broken = psycopg.connect(consumer_db["app"], autocommit=False)
    broken.close()
    deleted = []
    out = handle_message(helpers.body(), broken, lambda env: deleted.append(env.event_id))
    assert out is Outcome.DB_FAILURE
    assert deleted == []
    assert _inbox_count(consumer_db["app"]) == 0     # keine Inbox-Zeile
    assert _proj_count(consumer_db["app"]) == 0       # keine Projection
    # Spaetere saubere Zustellung wird ganz normal angewandt.
    with psycopg.connect(consumer_db["app"], autocommit=False) as good:
        assert handle_message(helpers.body(), good, lambda env: None) is Outcome.APPLIED


# --- Fehlerfenster 2: NACH Commit, VOR Delete -------------------------------

def test_post_commit_failure_injection_then_idempotent(make_consumer, consumer_db):
    inj = FailureInjector(enabled=True)
    c = make_consumer(injector=inj)
    client = FakeClient()
    out = c._process_message(client, _msg(helpers.body()))
    assert out is Outcome.FAILURE_INJECTED
    assert client.deleted == []                       # Commit ja, Delete nein
    assert _inbox_count(consumer_db["app"]) == 1
    assert _proj_count(consumer_db["app"]) == 1
    # Redelivery: idempotent als Transport-Duplikat, jetzt loeschbar.
    client2 = FakeClient()
    out2 = c._process_message(client2, _msg(helpers.body(), receipt="rh-2"))
    assert out2 is Outcome.TRANSPORT_DUPLICATE
    assert client2.deleted == ["rh-2"]
    assert _proj_count(consumer_db["app"]) == 1


def test_commit_succeeds_but_delete_fails_then_redelivery_is_duplicate(make_consumer, consumer_db):
    c = make_consumer()
    failing = FakeClient(delete_fails=True)
    out = c._process_message(failing, _msg(helpers.body()))
    assert out is Outcome.DELETE_FAILURE          # Commit erfolgt, Ack nicht
    assert failing.deleted == []
    assert _proj_count(consumer_db["app"]) == 1
    ok = FakeClient()
    out2 = c._process_message(ok, _msg(helpers.body(), receipt="rh-2"))
    assert out2 is Outcome.TRANSPORT_DUPLICATE
    assert ok.deleted == ["rh-2"]
    assert _proj_count(consumer_db["app"]) == 1


def test_restart_between_commit_and_ack(make_consumer, consumer_db):
    first = make_consumer()
    assert first._process_message(FakeClient(delete_fails=True), _msg(helpers.body())) is Outcome.DELETE_FAILURE
    # "Neustart" = komplett neue Instanz (neuer Pool/Injector) verarbeitet die Redelivery.
    restarted = make_consumer()
    client = FakeClient()
    out = restarted._process_message(client, _msg(helpers.body(), receipt="rh-2"))
    assert out is Outcome.TRANSPORT_DUPLICATE
    assert client.deleted == ["rh-2"]
    assert _proj_count(consumer_db["app"]) == 1


# --- Pool-Lifecycle / Recovery ----------------------------------------------

def test_pool_open_and_close(make_consumer):
    c = make_consumer()
    assert c._pool.closed is False
    with c._pool.connection() as conn:
        assert conn.execute("SELECT 1").fetchone()[0] == 1
    c.stop()
    assert c._pool.closed is True


def test_db_outage_degrades_then_new_instance_recovers(make_consumer, consumer_db):
    # NEUSTART-Recovery: nach Pool-Ausfall uebernimmt eine NEUE Consumer-Instanz.
    c = make_consumer()
    assert c._process_message(FakeClient(), _msg(helpers.body())) is Outcome.APPLIED
    c._pool.close()  # Ausfall -> kein Crash, kein Delete, DB_FAILURE
    down = FakeClient()
    assert c._process_message(down, _msg(helpers.body(), receipt="rh-2")) is Outcome.DB_FAILURE
    assert down.deleted == []
    restarted = make_consumer()  # frische Instanz/neuer Pool
    ok = FakeClient()
    assert restarted._process_message(ok, _msg(helpers.body(), receipt="rh-3")) is Outcome.TRANSPORT_DUPLICATE
    assert ok.deleted == ["rh-3"]
    assert _proj_count(consumer_db["app"]) == 1


def test_same_runtime_recovery_via_ready_gate(make_consumer):
    # SAME-RUNTIME-Recovery: nach DB_FAILURE (_db_ok=False) erholt sich derselbe
    # Prozess/Pool, sobald _db_ping() gegen die wieder erreichbare DB klappt.
    c = make_consumer()
    c.verify_schema()
    c._db_ok = False                       # vorangegangener DB-Fehler
    assert c._ready_to_receive() is True    # Gate pingt die (gesunde) DB -> erlaubt Poll
    assert c._db_ok is True


def test_loop_gate_blocks_until_db_ping_ok(make_consumer):
    # _ready_to_receive ist das Gate vor jedem Receive im Loop.
    c = make_consumer()
    c.verify_schema()
    c._db_ok = False
    c._pool.close()                         # DB/Pool weg -> Ping scheitert
    assert c._ready_to_receive() is False    # kein weiteres Polling
    assert c._db_ok is False


# --- Startup-Gate / Schema --------------------------------------------------

def test_verify_schema_passes_after_migration(make_consumer):
    make_consumer(open_pool=False).verify_schema()  # darf nicht werfen


def test_verify_schema_fails_without_migration(db_factory):
    _admin, app_dsn, _name = db_factory("consumer_admin", "consumer_app")
    c = SqsConsumer(database_url=app_dsn, queue_url="q")
    try:
        with pytest.raises(SchemaVersionError):
            c.verify_schema()
    finally:
        c._pool.close()


def test_verify_schema_rejects_unknown_newer(make_consumer, consumer_db):
    with psycopg.connect(consumer_db["admin"], autocommit=True) as c:
        c.execute("INSERT INTO schema_migrations (version) VALUES ('0099_future')")
    with pytest.raises(SchemaVersionError):
        make_consumer(open_pool=False).verify_schema()


def test_verify_schema_fails_when_db_unreachable():
    c = SqsConsumer(database_url=_UNREACHABLE_DSN, queue_url="q")
    try:
        with pytest.raises(Exception):  # PoolTimeout/OperationalError -> fail closed
            c.verify_schema()
    finally:
        c._pool.close()


# --- Readiness --------------------------------------------------------------

def test_not_ready_before_schema_validated(make_consumer):
    c = make_consumer()
    assert c.ready is False  # Schema noch nicht validiert


def test_ready_after_schema_and_poll(make_consumer):
    c = make_consumer(open_pool=False)
    c.verify_schema()
    c._last_poll_ok = time.time()
    assert c.ready is True


def test_empty_poll_keeps_fresh_and_ready(make_consumer):
    c = make_consumer(open_pool=False)
    c.verify_schema()
    client = EmptyPollClient(c)
    c._client = lambda: client      # SQS-Client-Seam fuer den Loop
    c._run()                        # genau eine Iteration, dann Stop
    assert client.receive_calls == 1
    assert c._last_poll_ok > 0      # leerer Poll zaehlt als frisch
    assert c.ready is True          # leerer Poll setzt Readiness NICHT auf 503


def test_stale_poll_makes_not_ready(make_consumer):
    c = make_consumer(open_pool=False)
    c.verify_schema()
    c._last_poll_ok = time.time() - 10_000  # lange kein erfolgreicher Poll
    assert c.ready is False


def test_not_ready_when_db_down_at_readiness(make_consumer):
    c = make_consumer(open_pool=False)
    c.verify_schema()
    c._last_poll_ok = time.time()
    c._pool.close()                 # DB/Pool weg
    assert c.ready is False


# --- Prozessmodell: genau ein Poller ----------------------------------------

def test_double_start_does_not_spawn_second_poller(make_consumer):
    c = make_consumer()
    c._client = lambda: _BlockingClient(c)
    c.start()
    first = c._thread
    c.start()  # zweiter Aufruf -> No-op
    assert c._thread is first
    c.stop()


class _BlockingClient:
    def __init__(self, consumer):
        self._consumer = consumer

    def receive_message(self, **kw):
        # Blockiert bis Shutdown, gibt dann leer zurueck (simuliert Long-Poll).
        self._consumer._stop.wait(kw.get("WaitTimeSeconds", 1))
        return {}

    def get_queue_attributes(self, **kw):
        return {"Attributes": {"ApproximateNumberOfMessages": "0"}}


# --- Config-/Port-Aufloesung ------------------------------------------------

def test_settings_resolve_pool_and_database_url(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "host=h port=5433 user=consumer_app dbname=consumer")
    monkeypatch.setenv("SQS_QUEUE_URL", "http://localhost:9324/q")
    monkeypatch.setenv("DB_POOL_MIN_SIZE", "2")
    monkeypatch.setenv("DB_POOL_MAX_SIZE", "6")
    s = Settings.from_env()
    assert "port=5433" in s.database_url
    assert s.pool_min_size == 2 and s.pool_max_size == 6


# --- Prozessmodell: Thread-Shutdown -----------------------------------------

def test_thread_dead_after_stop(make_consumer):
    c = make_consumer()
    c._client = lambda: _BlockingClient(c)
    c.start()
    assert c.healthy is True
    c.stop()
    assert c.healthy is False        # Poller-Thread beendet
    assert c._thread is None
    assert c._pool.closed is True


# --- HTTP-Endpoints (Liveness/Readiness getrennt) ---------------------------

import app.main as main_mod  # noqa: E402  (Import nach Dummy-Env oben)


class _FakePoller:
    def __init__(self, healthy=False, ready=False):
        self._h, self._r = healthy, ready

    @property
    def healthy(self):
        return self._h

    @property
    def ready(self):
        return self._r


def test_healthz_503_when_poller_dead(monkeypatch):
    monkeypatch.setattr(main_mod, "consumer", _FakePoller(healthy=False))
    assert main_mod.healthz().status_code == 503


def test_healthz_200_when_poller_alive(monkeypatch):
    monkeypatch.setattr(main_mod, "consumer", _FakePoller(healthy=True))
    assert main_mod.healthz().status_code == 200


def test_readyz_reflects_readiness(monkeypatch):
    monkeypatch.setattr(main_mod, "consumer", _FakePoller(ready=False))
    assert main_mod.readyz().status_code == 503
    monkeypatch.setattr(main_mod, "consumer", _FakePoller(ready=True))
    assert main_mod.readyz().status_code == 200


# --- Lifespan-Cleanup bei Exception -----------------------------------------

class _LifecycleRecorder:
    def __init__(self):
        self.calls = []

    def verify_schema(self):
        self.calls.append("verify_schema")

    def start(self):
        self.calls.append("start")

    def stop(self):
        self.calls.append("stop")


def test_lifespan_calls_stop_on_exception(monkeypatch):
    import asyncio
    fake = _LifecycleRecorder()
    monkeypatch.setattr(main_mod, "consumer", fake)

    async def drive():
        with pytest.raises(RuntimeError):
            async with main_mod.lifespan(main_mod.app):
                raise RuntimeError("boom during lifecycle")

    asyncio.run(drive())
    assert fake.calls == ["verify_schema", "start", "stop"]


# --- Least Privilege der Runtime-Rolle --------------------------------------

def test_runtime_role_cannot_ddl(consumer_db):
    with psycopg.connect(consumer_db["app"], autocommit=True) as c:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute("CREATE TABLE evil (i int)")
