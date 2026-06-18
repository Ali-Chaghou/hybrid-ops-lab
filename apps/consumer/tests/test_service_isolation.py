"""Service-Isolation: ein Testlauf laedt ausschliesslich die Module SEINES Service.

Beweis ueber getrennte Subprozesse mit getrenntem PYTHONPATH. Es gibt keinen
sys.modules-Trick — die Isolation entsteht durch getrennte Prozesse/Importpfade.
"""
from __future__ import annotations

import os
import pathlib
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parents[3]


def _run(service_dir: str, code: str):
    # cwd=REPO: dort existiert kein Top-Level-`app`-Paket, sodass NUR PYTHONPATH
    # entscheidet, welcher Service geladen wird (python -c legt cwd an sys.path[0]).
    env = {**os.environ, "PYTHONPATH": str(REPO / service_dir),
           "SQS_QUEUE_URL": "http://localhost:9324/x/q"}
    return subprocess.run([sys.executable, "-c", code], env=env, cwd=str(REPO),
                          capture_output=True, text=True)


def test_consumer_path_loads_consumer_modules():
    r = _run("apps/consumer", "import app.store as s; print(s.__file__)")
    assert r.returncode == 0, r.stderr
    assert "/apps/consumer/app/store.py" in r.stdout
    assert "/apps/inventory/" not in r.stdout


def test_inventory_path_does_not_expose_consumer_store():
    # apps/inventory hat kein app.store -> kontrollierter ModuleNotFoundError,
    # NICHT versehentlich der Consumer-Store.
    r = _run("apps/inventory", "import app.store")
    assert r.returncode != 0
    assert "No module named 'app.store'" in r.stderr


def test_isolation_is_order_independent():
    c1 = _run("apps/consumer", "import app.store as s; print(s.__file__)")
    inv = _run("apps/inventory", "import app as a; print(a.__file__)")
    c2 = _run("apps/consumer", "import app.store as s; print(s.__file__)")
    assert "/apps/consumer/app/store.py" in c1.stdout
    assert "/apps/inventory/app/__init__.py" in inv.stdout
    assert c1.stdout == c2.stdout            # Reihenfolge beeinflusst nichts
