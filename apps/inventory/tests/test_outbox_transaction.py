"""Atomare Movement+Outbox-Transaktion ueber die ECHTE Inventory-Runtime.

Treibt app.db.insert_movement / app.main.create_movement in Subprozessen (mit der
inventory_app-Rolle) und verifiziert den DB-Zustand im Testprozess ueber rohes SQL
als inventory_admin. Die Inventory-Runtime wird NIE in den Testprozess importiert.
"""
from __future__ import annotations

# --- Treiber (laufen im Subprozess mit Inventory-PYTHONPATH) -------------------

# Ein normaler Runtime-Insert: erzeugt Movement + Outbox-Event gemeinsam.
_INSERT_DRIVER = r"""
import asyncio
from app.main import app, lifespan
from app import db
async def main():
    async with lifespan(app):
        rec = db.insert_movement("SKU-9", 5, "WH-9")
        print("ID", rec["id"])
        print("EVENT_ID", rec["event_id"])
        print("CREATED_AT", rec["created_at"].isoformat())
asyncio.run(main())
"""

# Fault-Hook-Treiber: wirft an genau einem Punkt der Transaktion (sys.argv[1]).
_FAULT_DRIVER = r"""
import sys, asyncio
from app.main import app, lifespan
from app import db
POINT = sys.argv[1]
def hook(point):
    if point == POINT:
        raise RuntimeError("injected fault")
db._fault_hook = hook
async def main():
    async with lifespan(app):
        try:
            db.insert_movement("SKU-RB", 7, "WH-RB")
        except RuntimeError:
            print("RAISED")
            return
        print("NO_RAISE")
asyncio.run(main())
"""

# Beweist, dass der Request-Pfad keine Publish-/Queue-Funktion zieht.
_NO_PUBLISH_DRIVER = r"""
import sys
import app.main as main
assert "app.events" not in sys.modules, "events module imported by request path"
assert not hasattr(main, "publish_movement"), "publish_movement referenced in main"
print("NO_PUBLISH_OK")
"""

# Erfolgsmetriken nur nach Commit; Fehler-Metrik bei Rollback.
_METRICS_DRIVER = r"""
import asyncio
from app.main import app, lifespan, create_movement, MovementIn
from app import db
from app.metrics import MOVEMENTS_CREATED, OUTBOX_EVENTS_WRITTEN, MOVEMENT_TX_FAILURES
def val(c):
    return c._value.get()
async def main():
    async with lifespan(app):
        # 1. erzwungener Rollback -> keine Erfolgsmetrik, aber Fehler-Metrik.
        db._fault_hook = lambda p: (_ for _ in ()).throw(RuntimeError("x")) \
            if p == "after_outbox_insert" else None
        try:
            create_movement(MovementIn(sku="A", quantity=1, warehouse="W"))
        except Exception:
            pass
        assert val(MOVEMENTS_CREATED) == 0, "created bumped on rollback"
        assert val(OUTBOX_EVENTS_WRITTEN) == 0, "outbox bumped on rollback"
        assert val(MOVEMENT_TX_FAILURES) == 1, "failure metric not bumped"
        # 2. erfolgreicher Commit -> beide Erfolgsmetriken, keine weitere Fehler-Metrik.
        db._fault_hook = None
        create_movement(MovementIn(sku="B", quantity=2, warehouse="W"))
        assert val(MOVEMENTS_CREATED) == 1
        assert val(OUTBOX_EVENTS_WRITTEN) == 1
        assert val(MOVEMENT_TX_FAILURES) == 1
    print("METRICS_OK")
asyncio.run(main())
"""

# Niedrige Kardinalitaet: die neuen Outbox-Metriken haben keine Labels.
_LABELS_DRIVER = r"""
from app.metrics import MOVEMENTS_CREATED, OUTBOX_EVENTS_WRITTEN, MOVEMENT_TX_FAILURES
for m in (MOVEMENTS_CREATED, OUTBOX_EVENTS_WRITTEN, MOVEMENT_TX_FAILURES):
    assert m._labelnames == (), (m._name, m._labelnames)
print("LABELS_OK")
"""


def _counts(conn):
    mv = conn.execute("SELECT count(*) FROM stock_movements").fetchone()[0]
    ob = conn.execute("SELECT count(*) FROM event_outbox").fetchone()[0]
    return mv, ob


# --- Tests --------------------------------------------------------------------


def test_runtime_insert_writes_exactly_one_movement_and_one_event(
    make_inventory_db, run_lifespan, admin_conn
):
    db = make_inventory_db("correct")
    res = run_lifespan(db["app"], driver=_INSERT_DRIVER)
    assert res.returncode == 0, res.stderr
    with admin_conn(db) as c:
        mv, ob = _counts(c)
        assert mv == 1 and ob == 1  # genau ein Movement, genau ein Event
        row = c.execute(
            "SELECT m.id, m.event_id, m.created_at, o.movement_id, o.event_id AS o_eid, "
            "o.occurred_at, o.status, o.attempt_count, o.published_at, o.last_error "
            "FROM stock_movements m JOIN event_outbox o ON o.movement_id = m.id"
        ).fetchone()
    assert row[1] == row[4]              # gleiche event_id (Movement == Outbox)
    assert row[3] == row[0]              # movement_id == stock_movements.id
    assert row[5] == row[2]              # occurred_at == created_at
    assert row[6] == "pending"
    assert row[7] == 0
    assert row[8] is None and row[9] is None  # published_at / last_error


def test_fault_after_movement_insert_rolls_back_both(
    make_inventory_db, run_lifespan, admin_conn
):
    db = make_inventory_db("correct")
    res = run_lifespan(db["app"], driver=_FAULT_DRIVER, args=("after_movement_insert",))
    assert res.returncode == 0, res.stderr
    assert "RAISED" in res.stdout
    with admin_conn(db) as c:
        assert _counts(c) == (0, 0)  # vollstaendiger Rollback


def test_fault_after_outbox_insert_rolls_back_both(
    make_inventory_db, run_lifespan, admin_conn
):
    db = make_inventory_db("correct")
    res = run_lifespan(db["app"], driver=_FAULT_DRIVER, args=("after_outbox_insert",))
    assert res.returncode == 0, res.stderr
    assert "RAISED" in res.stdout
    with admin_conn(db) as c:
        assert _counts(c) == (0, 0)  # vollstaendiger Rollback


def test_request_path_does_not_pull_in_publish(make_inventory_db, run_lifespan):
    db = make_inventory_db("correct")
    res = run_lifespan(db["app"], driver=_NO_PUBLISH_DRIVER)
    assert res.returncode == 0, res.stderr
    assert "NO_PUBLISH_OK" in res.stdout


def test_success_metrics_only_after_commit(make_inventory_db, run_lifespan):
    db = make_inventory_db("correct")
    res = run_lifespan(db["app"], driver=_METRICS_DRIVER)
    assert res.returncode == 0, res.stderr
    assert "METRICS_OK" in res.stdout


def test_outbox_metrics_are_low_cardinality(make_inventory_db, run_lifespan):
    db = make_inventory_db("correct")
    res = run_lifespan(db["app"], driver=_LABELS_DRIVER)
    assert res.returncode == 0, res.stderr
    assert "LABELS_OK" in res.stdout


def test_rollback_output_leaks_no_payload_or_dsn(make_inventory_db, run_lifespan):
    db = make_inventory_db("correct")
    res = run_lifespan(db["app"], driver=_FAULT_DRIVER, args=("after_outbox_insert",))
    blob = res.stdout + res.stderr
    assert db["name"] not in blob              # kein DB-/DSN-Bestandteil
    assert "password" not in blob.lower()
    assert "SKU-RB" not in blob and "WH-RB" not in blob  # keine Payload-Werte
