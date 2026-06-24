"""Gate D3B1: ops/deploy/upgrade-phase-3-runtime.sh mit Fake-docker.

Verifiziert Ablauf/State/Resume/Fencing OHNE Live-Zugriff: Fake-`docker` protokolliert
alle Aufrufe und liefert Erfolg. Geprueft werden Phasenreihenfolge (Variante B:
Inventory-Stop VOR Migration, Bootstrap VOR Migration), genau eine Migration, kein
Publisher-Enable, keine Queue-Operation, Secret-Isolation, State-Validitaet, Resume und
fail-closed bei korruptem State.
"""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess

import pytest

REPO = pathlib.Path(__file__).resolve().parents[3]
SCRIPT = REPO / "ops/deploy/upgrade-phase-3-runtime.sh"
CHECKER = REPO / "ops/deploy/check-phase-3-runtime-state.py"

_FAKE_DOCKER = r"""#!/usr/bin/env bash
echo "docker $*" >> "$DOCKER_LOG"
if [ "$1" = "compose" ] && [ "$2" = "version" ]; then exit 0; fi
if [ "$1" = "inspect" ]; then echo "healthy"; exit 0; fi
if [ "$1" = "compose" ]; then
  case "$*" in
    *" ps "*--services*) printf 'db\ndb-bootstrap\ndb-prepare\ninventory-migrate\ninventory\npublisher\n'; exit 0 ;;
    *" ps "*-q*) echo "cid-fake-0001"; exit 0 ;;
  esac
fi
exit 0
"""

_SENTINEL_PW = "SENTINEL_PUB_PW_zzz"


def _env_file(tmp_path, mode=0o600):
    p = tmp_path / "dc.env"
    p.write_text(
        "POSTGRES_USER=hol_admin\nPOSTGRES_PASSWORD=x\nPOSTGRES_DB=postgres\n"
        "INVENTORY_DB=inventory\nINVENTORY_ADMIN_PASSWORD=a\nINVENTORY_APP_PASSWORD=b\n"
        f"INVENTORY_PUBLISHER_PASSWORD={_SENTINEL_PW}\n"
        "PUBLISHER_SQS_ENDPOINT_URL=\nPUBLISHER_SQS_QUEUE_URL=\nPUBLISHER_AWS_REGION=eu-central-1\n"
        "PUBLISHER_HOST_PORT=8001\n",
        encoding="utf-8",
    )
    os.chmod(p, mode)
    return p


def _setup(tmp_path):
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    (fakebin / "docker").write_text(_FAKE_DOCKER, encoding="utf-8")
    os.chmod(fakebin / "docker", 0o755)
    log = tmp_path / "docker.log"
    log.write_text("", encoding="utf-8")
    state_dir = tmp_path / "state"
    env = dict(os.environ)
    env["PATH"] = f"{fakebin}:{env['PATH']}"
    env["DOCKER_LOG"] = str(log)
    env["ENV_FILE"] = str(_env_file(tmp_path))
    env["PHASE3_STATE_DIR"] = str(state_dir)
    return env, log, state_dir


def _run(env, cmd):
    return subprocess.run(["bash", str(SCRIPT), cmd], env=env, capture_output=True, text=True, timeout=120)


def _idx(lines, needle):
    for i, ln in enumerate(lines):
        if needle in ln:
            return i
    return -1


pytestmark = pytest.mark.skipif(shutil.which("bash") is None or shutil.which("flock") is None,
                                reason="bash + flock benoetigt")


def test_full_run_phase_order_and_state(tmp_path):
    env, log, state_dir = _setup(tmp_path)
    res = _run(env, "run")
    assert res.returncode == 0, res.stderr
    lines = log.read_text().splitlines()
    i_bootstrap = _idx(lines, "run --rm --no-deps db-bootstrap")
    i_stop = _idx(lines, "stop inventory")
    i_migrate = _idx(lines, "run --rm --no-deps inventory-migrate")
    i_inv_up = _idx(lines, "up -d inventory")
    i_pub_up = _idx(lines, "up -d publisher")
    assert 0 <= i_bootstrap < i_stop < i_migrate < i_inv_up < i_pub_up  # Variante B
    # State complete + valide.
    rc = subprocess.run([os.sys.executable, str(CHECKER), str(state_dir / "state.json")]).returncode
    assert rc == 0
    data = json.loads((state_dir / "state.json").read_text())
    assert data["step"] == "complete" and data["complete"] is True and data["publisher_enabled"] is False


def test_no_enable_and_no_queue_ops_and_no_secret_leak(tmp_path):
    env, log, _ = _setup(tmp_path)
    assert _run(env, "run").returncode == 0
    text = log.read_text()
    assert "PUBLISHER_ENABLED=true" not in text
    assert "send_message" not in text and "receive-message" not in text and "sqs" not in text.lower()
    assert _SENTINEL_PW not in text  # Passwort NIE in docker-Argumenten


def test_resume_from_migration_complete_skips_migrate_and_stop(tmp_path):
    env, log, state_dir = _setup(tmp_path)
    state_dir.mkdir()
    (state_dir / "state.json").write_text(json.dumps({
        "schema_version": 1, "gate": "D3B1", "step": "migration-complete",
        "complete": False, "publisher_enabled": False, "updated_at": "x"}), encoding="utf-8")
    res = _run(env, "resume")
    assert res.returncode == 0, res.stderr
    text = log.read_text()
    assert "run --rm --no-deps inventory-migrate" not in text  # nicht erneut migrieren
    assert "stop inventory" not in text
    assert "up -d inventory" in text and "up -d publisher" in text
    assert json.loads((state_dir / "state.json").read_text())["step"] == "complete"


def test_resume_corrupt_state_fails_closed(tmp_path):
    env, _, state_dir = _setup(tmp_path)
    state_dir.mkdir()
    (state_dir / "state.json").write_text("{corrupt", encoding="utf-8")
    res = _run(env, "resume")
    assert res.returncode != 0
    assert "korrupt" in res.stderr.lower() or "abbruch" in res.stderr.lower()


def test_preflight_ok_with_secure_env(tmp_path):
    env, _, _ = _setup(tmp_path)
    assert _run(env, "preflight").returncode == 0


def test_preflight_rejects_world_readable_env(tmp_path):
    env, _, _ = _setup(tmp_path)
    os.chmod(env["ENV_FILE"], 0o644)
    res = _run(env, "preflight")
    assert res.returncode != 0
    assert "world-readable" in res.stderr.lower()


def test_preflight_rejects_missing_publisher_password(tmp_path):
    env, _, _ = _setup(tmp_path)
    # Passwort leeren -> Fail closed.
    p = pathlib.Path(env["ENV_FILE"])
    p.write_text(p.read_text().replace(f"INVENTORY_PUBLISHER_PASSWORD={_SENTINEL_PW}",
                                       "INVENTORY_PUBLISHER_PASSWORD="), encoding="utf-8")
    os.chmod(p, 0o600)
    assert _run(env, "preflight").returncode != 0


def test_rejects_activating_override(tmp_path):
    env, _, _ = _setup(tmp_path)
    override = REPO / "sites/dc/docker-compose.override.yml"  # gitignored
    override.write_text("services:\n  publisher:\n    environment:\n      PUBLISHER_ENABLED: \"true\"\n",
                        encoding="utf-8")
    try:
        res = _run(env, "run")
        assert res.returncode != 0
        assert "override" in res.stderr.lower()
    finally:
        override.unlink(missing_ok=True)
