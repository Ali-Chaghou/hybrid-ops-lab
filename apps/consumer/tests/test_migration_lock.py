"""Advisory-Lock: zwei konkurrierende Migration-Runner gegen dieselbe DB."""
from __future__ import annotations

import pathlib
import threading

import psycopg
import pytest
from ops.db import migrate

CONSUMER_MIGRATIONS = pathlib.Path(__file__).resolve().parents[1] / "migrations"


def test_two_parallel_runners_apply_exactly_once(db_factory):
    admin, _app, _name = db_factory()
    barrier = threading.Barrier(2)
    results: list = [None, None]
    errors: list = [None, None]

    def worker(i: int):
        try:
            barrier.wait()  # echter gleichzeitiger Start
            results[i] = migrate.run(admin, CONSUMER_MIGRATIONS)
        except Exception as exc:  # noqa: BLE001 - Testdiagnose
            errors[i] = exc

    ts = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
    for t in ts: t.start()
    for t in ts: t.join(timeout=30)

    assert errors == [None, None], f"unerwartete Fehler: {errors}"   # kein 'already exists'-Laerm
    # Genau ein Runner hat die Migration angewandt, der andere fand nichts zu tun.
    applied_counts = sorted(len(r) for r in results)
    assert applied_counts == [0, 1]
    # In schema_migrations steht 0001_init genau einmal.
    with psycopg.connect(admin) as c:
        n = c.execute("SELECT count(*) FROM schema_migrations WHERE version='0001_init'").fetchone()[0]
    assert n == 1


def test_lock_released_after_failed_migration(db_factory, tmp_path):
    admin, _app, _name = db_factory()
    # Runner A: fehlerhafte Test-Migration -> Abbruch.
    (tmp_path / "0001_bad.sql").write_text("SELECT * FROM does_not_exist;", encoding="utf-8")
    with pytest.raises(RuntimeError):
        migrate.run(admin, tmp_path)
    # Kein halb angewandter Stand, kein schema_migrations-Eintrag, KEIN haengender Lock.
    with psycopg.connect(admin) as c:
        held = c.execute(
            "SELECT count(*) FROM pg_locks WHERE locktype='advisory'"
        ).fetchone()[0]
        marked = c.execute(
            "SELECT count(*) FROM schema_migrations WHERE version='0001_bad'"
        ).fetchone()[0]
    assert held == 0 and marked == 0
    # Runner B: korrigierte Migration -> erfolgreich (Lock war frei).
    (tmp_path / "0001_bad.sql").unlink()
    (tmp_path / "0001_good.sql").write_text("CREATE TABLE good (id int);", encoding="utf-8")
    assert migrate.run(admin, tmp_path) == ["0001_good"]
