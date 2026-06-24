"""Gate D3B2.1 (adversarial-hardened): upgrade-consumer-runtime.sh mit Fakes.

Deckt zusaetzlich ab: Release-SHA-Bindung, immutabler Runtime-Tag, deterministisches
image-id-verifiziertes Rollback, an die Restartzahl gebundenes Acknowledgement,
run/resume-Semantik.
"""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess

import pytest

REPO = pathlib.Path(__file__).resolve().parents[3]
SCRIPT = REPO / "ops/deploy/upgrade-consumer-runtime.sh"

_SENTINEL = "SENTINEL_CONSUMER_PW_zzz"
_SHA = "0123456789abcdef0123456789abcdef01234567"
_RUNTIME_TAG = "inventory-consumer:0123456789ab"
_OLD_ID = "sha256:" + "1" * 64

_FAKE_DOCKER = r"""#!/usr/bin/env bash
echo "docker $*" >> "$CMDLOG"
[ "$1 $2" = "compose version" ] && exit 0
if [ "$1" = "image" ] && [ "$2" = "inspect" ]; then
  for a in "$@"; do ref="$a"; done
  case "$ref" in
    hol-consumer:dev) echo "sha256:5e70005e70005e70005e70005e70005e70005e70005e70005e70005e70005e70"; exit 0 ;;
    inventory-consumer:dev) if [ -n "${FAKE_OLD_IMAGE_ID:-}" ]; then echo "$FAKE_OLD_IMAGE_ID"; exit 0; else exit 1; fi ;;
    *) echo "sha256:dead"; exit 0 ;;
  esac
fi
[ "$1" = "inspect" ] && { echo "healthy"; exit 0; }
[ "$1" = "port" ] && exit 0
[ "$1" = "tag" ] && exit 0
if [ "$1" = "compose" ]; then
  case "$*" in
    *" ps "*-q*) echo "cid-fake"; exit 0 ;;
    *" exec "*consumer-db*) echo "t"; exit 0 ;;
  esac
fi
exit 0
"""

_FAKE_KUBECTL = r"""#!/usr/bin/env bash
echo "kubectl $*" >> "$CMDLOG"
case "$*" in
  *restartCount*) echo "${FAKE_RESTARTS:-0}"; exit 0 ;;
  *imageID*) echo "${FAKE_POD_IMAGE_ID:-sha256:1111111111111111111111111111111111111111111111111111111111111111}"; exit 0 ;;
  *"annotations.deployment"*) echo "3"; exit 0 ;;
  *containers*image*|*containers\[0\].image*) if [ -f "${DEPLOY_MARKER:-/nonexistent}" ]; then echo "${FAKE_RUNTIME_TAG}"; else echo "inventory-consumer:dev"; fi; exit 0 ;;
  *"rollout status"*) exit 0 ;;
  *"set image"*) exit 0 ;;
  *exec*) exit 0 ;;
  *metadata.name*) echo "inventory-consumer-x"; exit 0 ;;
esac
exit 0
"""

_FAKE_K3D = r"""#!/usr/bin/env bash
echo "k3d $*" >> "$CMDLOG"
[ "$*" = "cluster list" ] && { echo "site-cloud 1/1"; exit 0; }
exit 0
"""

_FAKE_CURL = r"""#!/usr/bin/env bash
echo "curl $*" >> "$CMDLOG"
for a in "$@"; do url="$a"; done
case "$url" in
  *Action=ListQueues*) echo '<r><QueueUrl>http://h/000000000000/inventory-movements</QueueUrl><QueueUrl>http://h/000000000000/inventory-movements-dlq</QueueUrl></r>'; exit 0 ;;
  *Action=GetQueueAttributes*) echo '<r><Attribute><Name>RedrivePolicy</Name><Value>{"maxReceiveCount":"5"}</Value></Attribute></r>'; exit 0 ;;
  *api/v1/targets*) if [ "${PROM_FAIL:-0}" = "1" ]; then echo '{"data":{"activeTargets":[]}}'; else echo '{"data":{"activeTargets":[{"labels":{"job":"consumer"},"health":"up"}]}}'; fi; exit 0 ;;
  *api/v1/rules*) echo '{"data":{"groups":[{"name":"consumer"},{"name":"queue"}]}}'; exit 0 ;;
esac
echo '{}'; exit 0
"""

_FAKE_DEPLOY = r"""#!/usr/bin/env bash
echo "deploy-consumer IMAGE=$IMAGE $*" >> "$CMDLOG"
printf '[]' > "$D3B2_TARGET_DIR/consumer.json"
touch "$DEPLOY_MARKER"
exit ${FAKE_DEPLOY_RC:-0}
"""


def _w(p, c, mode=0o755):
    p.write_text(c, encoding="utf-8"); os.chmod(p, mode)


def _setup(tmp_path, **over):
    fb = tmp_path / "bin"; fb.mkdir()
    _w(fb / "docker", _FAKE_DOCKER); _w(fb / "kubectl", _FAKE_KUBECTL)
    _w(fb / "k3d", _FAKE_K3D); _w(fb / "curl", _FAKE_CURL)
    deploy = tmp_path / "fake-deploy.sh"; _w(deploy, _FAKE_DEPLOY)
    envf = tmp_path / "cloud.env"
    envf.write_text(f"CONSUMER_DB=consumer\nCONSUMER_APP_PASSWORD={_SENTINEL}\nPOSTGRES_USER=u\nPOSTGRES_PASSWORD=p\n", encoding="utf-8")
    os.chmod(envf, 0o600)
    cmdlog = tmp_path / "cmd.log"; cmdlog.write_text("", encoding="utf-8")
    target = tmp_path / "targets"; target.mkdir()
    env = dict(os.environ)
    env.update({
        "PATH": f"{fb}:{env['PATH']}", "CMDLOG": str(cmdlog), "ENV_FILE": str(envf),
        "D3B2_STATE_DIR": str(tmp_path / "state"), "D3B2_TARGET_DIR": str(target),
        "DEPLOY_CONSUMER_CMD": str(deploy), "QUEUE_GATE_CMD": "true",
        "D3B2_RELEASE_SHA": _SHA, "FAKE_RUNTIME_TAG": _RUNTIME_TAG,
        "FAKE_OLD_IMAGE_ID": _OLD_ID, "FAKE_POD_IMAGE_ID": _OLD_ID,
        "DEPLOY_MARKER": str(tmp_path / "deployed.marker"),
    })
    env.update(over)
    return env, cmdlog, (tmp_path / "state")


def _run(env, cmd):
    return subprocess.run(["bash", str(SCRIPT), cmd], env=env, capture_output=True, text=True, timeout=120)


def _seed_state(state_dir, step, release=_SHA, complete=False):
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "state.json").write_text(json.dumps({
        "schema_version": 1, "gate": "D3B2.1", "step": step, "complete": complete,
        "release_sha": release, "runtime_image_tag": _RUNTIME_TAG, "updated_at": "x"}), encoding="utf-8")


def _i(t, n):
    j = t.find(n); return j if j >= 0 else 10**9


def _simulate_prior_deploy(env):
    """Artefakte eines bereits erfolgten Consumer-Deploys (fuer Resume ab consumer-deployed)."""
    pathlib.Path(env["DEPLOY_MARKER"]).touch()
    (pathlib.Path(env["D3B2_TARGET_DIR"]) / "consumer.json").write_text("[]", encoding="utf-8")


pytestmark = pytest.mark.skipif(shutil.which("bash") is None or shutil.which("flock") is None,
                                reason="bash + flock benoetigt")


# --- Happy path + immutable Tag --------------------------------------------

def test_full_run_completes_with_release_and_immutable_tag(tmp_path):
    env, cmdlog, state_dir = _setup(tmp_path)
    res = _run(env, "run")
    assert res.returncode == 0, res.stderr
    log = cmdlog.read_text()
    # Deploy nutzt den immutablen Release-Tag.
    assert f"deploy-consumer IMAGE={_RUNTIME_TAG}" in log
    # Pod-Spec-Image wird gegen den Release-Tag verifiziert (get deploy ... image).
    data = json.loads((state_dir / "state.json").read_text())
    assert data["step"] == "complete" and data["release_sha"] == _SHA
    assert data["runtime_image_tag"] == _RUNTIME_TAG


def test_build_before_migration_and_bootstrap(tmp_path):
    env, cmdlog, _ = _setup(tmp_path)
    assert _run(env, "run").returncode == 0
    log = cmdlog.read_text()
    assert _i(log, "build consumer-db-bootstrap") < _i(log, "run --rm --no-deps consumer-migrate")
    assert (_i(log, "run --rm --no-deps consumer-db-bootstrap")
            < _i(log, "run --rm --no-deps consumer-db-prepare")
            < _i(log, "run --rm --no-deps consumer-migrate"))


def test_no_queue_mutation_no_sitedc_no_secret_leak(tmp_path):
    env, cmdlog, _ = _setup(tmp_path)
    assert _run(env, "run").returncode == 0
    log = cmdlog.read_text()
    for bad in ("SendMessage", "DeleteMessage", "PurgeQueue", "ReceiveMessage"):
        assert bad not in log
    assert "sites/dc" not in log and _SENTINEL not in log


# --- Release-Bindung --------------------------------------------------------

def test_malformed_release_sha_rejected(tmp_path):
    env, _, _ = _setup(tmp_path, D3B2_RELEASE_SHA="nothex")
    assert _run(env, "run").returncode != 0


def test_resume_same_release_completes(tmp_path):
    env, cmdlog, state_dir = _setup(tmp_path)
    # Consumer bereits deployed (Artefakte + Marker), State release-gebunden.
    _simulate_prior_deploy(env)
    _seed_state(state_dir, "consumer-deployed", release=_SHA)
    res = _run(env, "resume")
    assert res.returncode == 0, res.stderr
    assert "deploy-consumer" not in cmdlog.read_text()   # kein erneuter Deploy
    assert json.loads((state_dir / "state.json").read_text())["step"] == "complete"


def test_resume_different_release_rejected(tmp_path):
    env, _, state_dir = _setup(tmp_path)
    _seed_state(state_dir, "consumer-deployed", release="f" * 40)
    res = _run(env, "resume")
    assert res.returncode != 0
    assert "mismatch" in res.stderr.lower() or "release" in res.stderr.lower()


def test_resume_state_without_release_rejected(tmp_path):
    env, _, state_dir = _setup(tmp_path)
    state_dir.mkdir(parents=True)
    (state_dir / "state.json").write_text(json.dumps({
        "schema_version": 1, "gate": "D3B2.1", "step": "consumer-deployed",
        "complete": False, "updated_at": "x"}), encoding="utf-8")  # KEIN release_sha
    assert _run(env, "resume").returncode != 0


# --- run/resume-Semantik ----------------------------------------------------

def test_run_on_incomplete_state_aborts_to_resume(tmp_path):
    env, _, state_dir = _setup(tmp_path)
    _seed_state(state_dir, "queue-config-ready")
    res = _run(env, "run")
    assert res.returncode != 0
    assert "resume" in res.stderr.lower()


def test_run_on_complete_state_no_mutation(tmp_path):
    env, cmdlog, state_dir = _setup(tmp_path)
    _seed_state(state_dir, "complete", complete=True)
    res = _run(env, "run")
    assert res.returncode == 0
    log = cmdlog.read_text()
    assert "build consumer-db-bootstrap" not in log and "force-recreate" not in log


def test_resume_without_state_aborts(tmp_path):
    env, _, _ = _setup(tmp_path)
    assert _run(env, "resume").returncode != 0


def test_corrupt_state_resume_fails_closed(tmp_path):
    env, _, state_dir = _setup(tmp_path)
    state_dir.mkdir(parents=True)
    (state_dir / "state.json").write_text("{corrupt", encoding="utf-8")
    assert _run(env, "resume").returncode != 0


# --- Deterministisches Rollback --------------------------------------------

def test_capture_aborts_when_old_image_unavailable(tmp_path):
    # docker image inspect inventory-consumer:dev schlaegt fehl -> vor Mutation Abbruch.
    env, cmdlog, _ = _setup(tmp_path, FAKE_OLD_IMAGE_ID="")
    res = _run(env, "run")
    assert res.returncode != 0
    log = cmdlog.read_text()
    # Kein neuer Deploy, da Rollback-Erfassung vorher fail closed.
    assert "deploy-consumer" not in log


def test_capture_aborts_on_pod_image_id_mismatch(tmp_path):
    env, cmdlog, _ = _setup(tmp_path, FAKE_POD_IMAGE_ID="sha256:" + "9" * 64)
    res = _run(env, "run")
    assert res.returncode != 0
    assert "deploy-consumer" not in cmdlog.read_text()


def test_deploy_failure_rolls_back_to_saved_tag_and_verifies(tmp_path):
    env, cmdlog, state_dir = _setup(tmp_path, FAKE_DEPLOY_RC="1")
    res = _run(env, "run")
    assert res.returncode != 0
    log = cmdlog.read_text()
    # Rollback per 'set image' auf den gesicherten Rollback-Tag, NICHT nur 'rollout undo'.
    assert "set image deploy/inventory-consumer consumer=inventory-consumer:rollback-" in log
    assert "rollout undo" not in log
    assert json.loads((state_dir / "state.json").read_text())["step"] == "consumer-schema-ready"


def test_rollback_tag_imported_into_k3d_before_build(tmp_path):
    env, cmdlog, _ = _setup(tmp_path)
    assert _run(env, "run").returncode == 0
    log = cmdlog.read_text()
    assert "image import inventory-consumer:rollback-" in log


# --- Restart-Acknowledgement (an Restartzahl gebunden) ----------------------

def test_unacked_restarts_fail_closed(tmp_path):
    env, _, _ = _setup(tmp_path, FAKE_RESTARTS="8")
    assert _run(env, "run").returncode != 0


def test_acked_restarts_proceed_and_record_count(tmp_path):
    env, _, state_dir = _setup(tmp_path, FAKE_RESTARTS="8", D3B2_ACK_CONSUMER_RESTARTS="1")
    assert _run(env, "run").returncode == 0
    audit = json.loads((state_dir / "restart-ack.json").read_text())
    assert audit["acked_restart_count"] == 8


def test_ack_for_8_does_not_auto_accept_9(tmp_path):
    env, _, state_dir = _setup(tmp_path)
    _seed_state(state_dir, "consumer-deployed")
    _simulate_prior_deploy(env)
    (state_dir / "restart-ack.json").write_text(json.dumps({"acked_restart_count": 8, "at": "x"}), encoding="utf-8")
    # Restartzahl 9 ohne neues Ack -> fail closed.
    env_fail = dict(env); env_fail["FAKE_RESTARTS"] = "9"; env_fail["D3B2_ACK_CONSUMER_RESTARTS"] = "0"
    assert _run(env_fail, "resume").returncode != 0
    # Gleiche, bereits bestaetigte Zahl 8 ohne Ack -> erlaubt.
    env_ok = dict(env); env_ok["FAKE_RESTARTS"] = "8"; env_ok["D3B2_ACK_CONSUMER_RESTARTS"] = "0"
    assert _run(env_ok, "resume").returncode == 0


# --- Monitoring-Resume ------------------------------------------------------

def test_monitoring_failure_resumable(tmp_path):
    env, cmdlog, state_dir = _setup(tmp_path, PROM_FAIL="1")
    assert _run(env, "run").returncode != 0
    assert json.loads((state_dir / "state.json").read_text())["step"] == "consumer-deployed"
    env2 = dict(env); env2["PROM_FAIL"] = "0"; cmdlog.write_text("", encoding="utf-8")
    assert _run(env2, "resume").returncode == 0
    assert "deploy-consumer" not in cmdlog.read_text()
    assert json.loads((state_dir / "state.json").read_text())["step"] == "complete"
