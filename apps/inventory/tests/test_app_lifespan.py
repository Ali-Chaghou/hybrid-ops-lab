"""Echter Inventory-FastAPI-Lifespan gegen vorbereitete/unvorbereitete Schemata."""
from __future__ import annotations

import psycopg


def test_correct_schema_starts_and_inserts(make_inventory_db, run_lifespan):
    db = make_inventory_db("correct")
    res = run_lifespan(db["app"])
    assert res.returncode == 0, res.stderr
    assert "OK" in res.stdout
    assert "apps/inventory/app/db.py" in res.stdout          # echter Inventory-Code geladen
    assert "EVENT_ID" in res.stdout and "CREATED_AT" in res.stdout  # stabile event_id + created_at


def test_missing_schema_fails_and_creates_nothing(make_inventory_db, run_lifespan):
    db = make_inventory_db("missing")
    res = run_lifespan(db["app"])
    assert res.returncode != 0
    assert "nicht vorbereitet" in res.stderr                 # kontrollierte Meldung
    # keine DSN/kein Passwort in der Fehlerausgabe
    assert db["name"] not in res.stderr and "password" not in res.stderr.lower()
    # KEIN Schema automatisch erstellt
    with psycopg.connect(db["admin"]) as c:
        assert c.execute("SELECT to_regclass('public.stock_movements')").fetchone()[0] is None


def test_too_old_schema_fails(make_inventory_db, run_lifespan):
    db = make_inventory_db("too_old")
    res = run_lifespan(db["app"])
    assert res.returncode != 0
    assert "nicht vorbereitet" in res.stderr                 # erwartete Version fehlt
    assert "SchemaNotReadyError" in res.stderr


def test_unknown_newer_schema_fails(make_inventory_db, run_lifespan):
    db = make_inventory_db("unknown")
    res = run_lifespan(db["app"])
    assert res.returncode != 0
    assert "Unbekannter, neuerer Schemastand" in res.stderr   # keine Vorwaertskompatibilitaet


def test_loads_only_inventory_app(make_inventory_db, run_lifespan, path_driver):
    db = make_inventory_db("correct")
    res = run_lifespan(db["app"], driver=path_driver)
    assert res.returncode == 0, res.stderr
    assert "/apps/inventory/app/db.py" in res.stdout
    assert "/apps/inventory/app/main.py" in res.stdout
    assert "/apps/consumer/" not in res.stdout                # nie Consumer-Module
