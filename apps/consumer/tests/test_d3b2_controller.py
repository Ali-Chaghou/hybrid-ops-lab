"""Gate D3B2.1 (containerd-rollback-hardened): upgrade-consumer-runtime.sh mit Fakes.

Der Rollback-/Image-Identitaetsnachweis laeuft jetzt ausschliesslich ueber CRI/
containerd (volle Digests). Docker-.Id, docker tag, k3d import (fuer das Legacy-Image)
und Praefixvergleiche sind entfernt. Fakes emulieren `docker exec <node> k3s crictl
inspecti` und `k3s ctr -n k8s.io images tag`.
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
_RID = "sha256:" + "a" * 64                  # laufende containerd-Identitaet (= Pod-Digest)
_RB_REF = "docker.io/library/inventory-consumer:rollback-" + "a" * 12

_FAKE_DOCKER = r"""#!/usr/bin/env bash
echo "docker $*" >> "$CMDLOG"
[ "$1 $2" = "compose version" ] && exit 0
if [ "$1" = "exec" ]; then
  shift 2  # 'exec <node>' verwerfen
  case "$*" in
    "test -x "*crictl) [ "${FAKE_NO_CRICTL:-0}" = "1" ] && exit 1; exit 0 ;;
    "test -x "*ctr)    [ "${FAKE_NO_CTR:-0}" = "1" ] && exit 1; exit 0 ;;
    *"inspecti --help"*) [ "${FAKE_CRICTL_PROBE_FAIL:-0}" = "1" ] && exit 1; exit 0 ;;
    *"images ls -q"*) [ "${FAKE_CTR_LS_FAIL:-0}" = "1" ] && exit 1; echo "x"; exit 0 ;;
    *"images tag --help"*) [ "${FAKE_NO_TAG_SUBCMD:-0}" = "1" ] && exit 1; exit 0 ;;   # Help-Probe (kein Tag)
    *"images tag "*) touch "${CTR_TAG_MARKER}"; exit "${FAKE_CTR_RC:-0}" ;;            # echtes Tagging (mit Refs)
    *"inspecti -o json"*)
      ref="${@: -1}"
      case "$ref" in
        *:rollback-*)
          if [ "${FAKE_RB_PREEXISTS:-0}" = "1" ]; then
            printf '{"status":{"id":"%s","repoTags":["%s"],"repoDigests":[]}}\n' "${FAKE_RB_ID:-${FAKE_CRI_ID:-REPLACE_RID}}" "$ref"; exit 0
          elif [ -f "${CTR_TAG_MARKER}" ]; then
            printf '{"status":{"id":"%s","repoTags":["%s"],"repoDigests":[]}}\n' "${FAKE_CRI_ID:-REPLACE_RID}" "$ref"; exit 0
          else echo ""; exit 1; fi ;;
        *)
          [ "${FAKE_CRI_EMPTY:-0}" = "1" ] && { echo ""; exit 1; }
          printf '{"status":{"id":"%s","repoTags":["%s"],"repoDigests":["%s"]}}\n' \
            "${FAKE_CRI_ID:-REPLACE_RID}" "${FAKE_CRI_REPOTAG:-docker.io/library/inventory-consumer:dev}" "${FAKE_CRI_REPODIGEST:-}"; exit 0 ;;
      esac ;;
  esac
  exit 0
fi
if [ "$1" = "image" ] && [ "$2" = "inspect" ]; then echo "sha256:5e7000000000000000000000000000000000000000000000000000000000face"; exit 0; fi
[ "$1" = "inspect" ] && { echo "healthy"; exit 0; }
[ "$1" = "port" ] && { [ "${FAKE_NO_PORT:-0}" = "1" ] && exit 1; echo "0.0.0.0:30090"; exit 0; }
if [ "$1" = "compose" ]; then
  case "$*" in
    *" ps "*-q*) echo "cid-fake"; exit 0 ;;
    *" exec "*consumer-db*) echo "t"; exit 0 ;;
  esac
fi
exit 0
""".replace("REPLACE_RID", _RID)

_FAKE_KUBECTL = r"""#!/usr/bin/env bash
echo "kubectl $*" >> "$CMDLOG"
case "$*" in
  *"get pod"*"-o json"*)
    # Ein-Pod-Liste fuer select-consumer-pod.py: Running/Ready, nicht terminierend.
    # Pre-Deploy Image inventory-consumer:dev, post-Deploy (Marker) der Release-Tag.
    if [ -f "${DEPLOY_MARKER:-/nonexistent}" ]; then img="${FAKE_RUNTIME_TAG}"; else img="${FAKE_CAPTURE_POD_IMAGE:-inventory-consumer:dev}"; fi
    if [ -f "${DEPLOY_MARKER:-/nonexistent}" ] && [ -n "${FAKE_POD_IMAGE_ID_AFTER:-}" ]; then iid="${FAKE_POD_IMAGE_ID_AFTER}"; else iid="${FAKE_POD_IMAGE_ID:-REPLACE_RID}"; fi
    printf '{"items":[{"metadata":{"name":"inventory-consumer-x","deletionTimestamp":null,"creationTimestamp":"2026-01-01T00:00:00Z"},"spec":{"containers":[{"name":"consumer","image":"%s"}]},"status":{"phase":"Running","startTime":"2026-01-01T00:00:00Z","containerStatuses":[{"name":"consumer","ready":true,"imageID":"%s","restartCount":%s}]}}]}\n' "$img" "$iid" "${FAKE_RESTARTS:-0}"
    exit 0 ;;
  *restartCount*) echo "${FAKE_RESTARTS:-0}"; exit 0 ;;
  *imageID*) if [ -f "${DEPLOY_MARKER:-/nonexistent}" ] && [ -n "${FAKE_POD_IMAGE_ID_AFTER:-}" ]; then echo "${FAKE_POD_IMAGE_ID_AFTER}"; else echo "${FAKE_POD_IMAGE_ID:-REPLACE_RID}"; fi; exit 0 ;;
  *"annotations.deployment"*) echo "3"; exit 0 ;;
  *containers*image*) if [ -f "${DEPLOY_MARKER:-/nonexistent}" ]; then echo "${FAKE_RUNTIME_TAG}"; else echo "inventory-consumer:dev"; fi; exit 0 ;;
  *"rollout status"*) exit 0 ;;
  *"set image"*) exit 0 ;;
  *exec*) exit 0 ;;
  *metadata.name*) echo "inventory-consumer-x"; exit 0 ;;
esac
exit 0
""".replace("REPLACE_RID", _RID)

_FAKE_K3D = """#!/usr/bin/env bash
echo "k3d $*" >> "$CMDLOG"
[ "$*" = "cluster list" ] && { echo "site-cloud 1/1"; exit 0; }
exit 0
"""

_FAKE_CURL = r"""#!/usr/bin/env bash
echo "curl $*" >> "$CMDLOG"
for a in "$@"; do url="$a"; done
case "$url" in
  *Action=ListQueues*)
    # Readiness-Simulation: die DLQ erscheint erst ab dem FAKE_QUEUE_READY_AFTER-ten
    # ListQueues-Aufruf -> _verify_queue schlaegt vorher fehl (DLQ fehlt).
    c=$(cat "${FAKE_LQ_COUNTER}" 2>/dev/null || echo 0); c=$((c+1)); echo "$c" > "${FAKE_LQ_COUNTER}"
    if [ "$c" -ge "${FAKE_QUEUE_READY_AFTER:-1}" ]; then
      echo '<r><QueueUrl>http://h/000000000000/inventory-movements</QueueUrl><QueueUrl>http://h/000000000000/inventory-movements-dlq</QueueUrl></r>'
    else
      echo '<r><QueueUrl>http://h/000000000000/inventory-movements</QueueUrl></r>'
    fi
    exit 0 ;;
  *Action=GetQueueAttributes*) echo '<r><Attribute><Name>RedrivePolicy</Name><Value>{"maxReceiveCount":"5"}</Value></Attribute></r>'; exit 0 ;;
  *-/ready*) if [ "${PROM_READY_FAIL:-0}" = "1" ]; then printf '503'; else printf '200'; fi; exit 0 ;;
  *api/v1/targets*)
    if [ "${PROM_PUBLISHER:-0}" = "1" ]; then printf '%s' '{"status":"success","data":{"activeTargets":[{"labels":{"job":"consumer"},"health":"up"},{"labels":{"job":"publisher"},"health":"up"}]}}';
    elif [ "${PROM_FAIL:-0}" = "1" ]; then printf '%s' '{"status":"success","data":{"activeTargets":[]}}';
    else printf '%s' '{"status":"success","data":{"activeTargets":[{"labels":{"job":"consumer"},"health":"up"}]}}'; fi
    printf '\n%s' "${PROM_HTTP:-200}"; exit 0 ;;
  *api/v1/rules*)
    printf '%s' '{"status":"success","data":{"groups":[{"name":"consumer"},{"name":"queue"}]}}'
    printf '\n%s' "${PROM_HTTP:-200}"; exit 0 ;;
esac
echo '{}'; exit 0
"""

_FAKE_DEPLOY = r"""#!/usr/bin/env bash
echo "deploy-consumer IMAGE=$IMAGE K3D_PORT_OWNER=$K3D_PORT_OWNER $*" >> "$CMDLOG"
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
        "FAKE_CRI_ID": _RID, "FAKE_POD_IMAGE_ID": _RID,
        "DEPLOY_MARKER": str(tmp_path / "deployed.marker"),
        "CTR_TAG_MARKER": str(tmp_path / "ctr_tag.marker"),
        "D3B2_K3D_NODE": "k3d-site-cloud-server-0",
        "FAKE_LQ_COUNTER": str(tmp_path / "lq.cnt"),  # ListQueues-Aufrufzaehler (Readiness-Sim)
        "D3B2_RUNTIME_VERIFY_ATTEMPTS": "3",          # Fehlerpfade ohne reales sleep
        "D3B2_RUNTIME_VERIFY_INTERVAL": "0",
        "D3B2_MONITORING_VERIFY_ATTEMPTS": "3",       # Monitoring-Waiter ohne reales sleep
        "D3B2_MONITORING_VERIFY_INTERVAL": "0",
        "D3B2_MONITORING_VERIFY_BUDGET_SECONDS": "5",
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
    pathlib.Path(env["DEPLOY_MARKER"]).touch()
    (pathlib.Path(env["D3B2_TARGET_DIR"]) / "consumer.json").write_text("[]", encoding="utf-8")


pytestmark = pytest.mark.skipif(shutil.which("bash") is None or shutil.which("flock") is None,
                                reason="bash + flock benoetigt")


# --- Happy path + immutable Tag + CRI-Identitaet ---------------------------

def test_full_run_completes(tmp_path):
    env, cmdlog, state_dir = _setup(tmp_path)
    res = _run(env, "run")
    assert res.returncode == 0, res.stderr
    log = cmdlog.read_text()
    assert f"deploy-consumer IMAGE={_RUNTIME_TAG}" in log
    data = json.loads((state_dir / "state.json").read_text())
    assert data["step"] == "complete" and data["release_sha"] == _SHA


def test_no_docker_id_no_docker_tag_no_k3d_import_for_legacy(tmp_path):
    env, cmdlog, _ = _setup(tmp_path)
    assert _run(env, "run").returncode == 0
    log = cmdlog.read_text()
    assert "docker tag" not in log
    assert "image import inventory-consumer:rollback" not in log and "image import docker.io/library/inventory-consumer:rollback" not in log
    assert "rollout undo" not in log
    # Legacy-Image-Identitaet ueber containerd: ctr tag im k8s.io-Namespace.
    assert "ctr -n k8s.io images tag" in log
    assert "crictl inspecti" in log


def test_legacy_secured_before_build_and_deploy(tmp_path):
    env, cmdlog, _ = _setup(tmp_path)
    assert _run(env, "run").returncode == 0
    log = cmdlog.read_text()
    assert _i(log, "ctr -n k8s.io images tag") < _i(log, "deploy-consumer")


# --- Runtime-Tool-Erkennung (plain crictl/ctr, kein k3s) -------------------

def test_uses_plain_crictl_ctr_not_k3s_wrappers(tmp_path):
    env, cmdlog, _ = _setup(tmp_path)
    assert _run(env, "run").returncode == 0
    log = cmdlog.read_text()
    assert "/bin/crictl inspecti -o json" in log
    assert "/bin/ctr -n k8s.io images tag" in log
    assert "k3s crictl" not in log and "k3s ctr" not in log
    # k8s.io-Namespace explizit; Detection-Probes vorhanden.
    assert "test -x /bin/crictl" in log and "test -x /bin/ctr" in log
    assert "/bin/ctr -n k8s.io images ls -q" in log


def test_tag_subcommand_probe_uses_help_not_bare(tmp_path):
    env, cmdlog, _ = _setup(tmp_path)
    assert _run(env, "run").returncode == 0
    log = cmdlog.read_text()
    # Help-Probe (Exit 0, kein Tag) — KEIN nackter No-Argument-Probeaufruf.
    assert "/bin/ctr -n k8s.io images tag --help" in log
    import re
    # Es darf KEINE 'images tag'-Zeile OHNE folgende Referenz/--help geben.
    for line in log.splitlines():
        if "images tag" in line:
            assert re.search(r"images tag (--help|\S+ \S+)", line), line
    # Die Help-Probe enthaelt keine source/target-Referenz (kein docker.io/...).
    probe = [l for l in log.splitlines() if "images tag --help" in l][0]
    assert "docker.io/library/inventory-consumer" not in probe


@pytest.mark.parametrize("flag", ["FAKE_NO_CRICTL", "FAKE_NO_CTR", "FAKE_CRICTL_PROBE_FAIL",
                                  "FAKE_CTR_LS_FAIL", "FAKE_NO_TAG_SUBCMD"])
def test_tool_gate_fails_closed(tmp_path, flag):
    env, cmdlog, state_dir = _setup(tmp_path, **{flag: "1"})
    res = _run(env, "run")
    assert res.returncode != 0
    log = cmdlog.read_text()
    # Gate VOR jeder Mutation: kein Build, keine Queue-Neuerstellung, keine Migration,
    # kein Deploy, KEIN State geschrieben.
    assert "build consumer-db-bootstrap" not in log
    assert "force-recreate --no-deps sqs" not in log
    assert "run --rm --no-deps consumer-migrate" not in log
    assert "deploy-consumer" not in log
    assert not (state_dir / "state.json").exists()


def test_tool_gate_runs_in_preflight_before_state(tmp_path):
    env, _, state_dir = _setup(tmp_path, FAKE_NO_CRICTL="1")
    assert _run(env, "preflight").returncode != 0
    assert not (state_dir / "state.json").exists()


# --- NodePort-Publikation: Loadbalancer (serverlb), nicht Server-Node --------

def test_nodeport_check_uses_serverlb_not_server_node(tmp_path):
    env, cmdlog, _ = _setup(tmp_path)
    assert _run(env, "run").returncode == 0
    log = cmdlog.read_text()
    assert "docker port k3d-site-cloud-serverlb 30090/tcp" in log
    assert "docker port k3d-site-cloud-server-0" not in log   # NICHT der Server-Node
    # CRI/containerd laufen weiterhin auf dem Server-Node, NICHT auf dem Loadbalancer.
    assert "docker exec k3d-site-cloud-server-0 /bin/crictl" in log
    assert "exec k3d-site-cloud-serverlb" not in log


def test_nodeport_missing_on_lb_fails_closed(tmp_path):
    env, cmdlog, state_dir = _setup(tmp_path, FAKE_NO_PORT="1")
    res = _run(env, "run")
    assert res.returncode != 0
    log = cmdlog.read_text()
    assert "build consumer-db-bootstrap" not in log and "deploy-consumer" not in log
    assert not (state_dir / "state.json").exists()


def test_invalid_port_owner_rejected(tmp_path):
    env, _, state_dir = _setup(tmp_path, D3B2_K3D_PORT_OWNER="bad name!")
    assert _run(env, "run").returncode != 0
    assert not (state_dir / "state.json").exists()


def test_custom_port_owner_used_in_check_and_forwarded_to_deploy(tmp_path):
    env, cmdlog, _ = _setup(tmp_path, D3B2_K3D_PORT_OWNER="k3d-custom-serverlb")
    assert _run(env, "run").returncode == 0
    log = cmdlog.read_text()
    # Preflight-Publikations-Check nutzt den custom Port-Owner.
    assert "docker port k3d-custom-serverlb 30090/tcp" in log
    # An den Deploy-Befehl als K3D_PORT_OWNER weitergereicht.
    assert "deploy-consumer IMAGE=" in log and "K3D_PORT_OWNER=k3d-custom-serverlb" in log
    # NICHT fuer CRI/containerd; CRI nutzt weiterhin den Server-Node.
    assert "exec k3d-custom-serverlb" not in log
    assert "docker exec k3d-site-cloud-server-0 /bin/crictl" in log


def test_no_queue_mutation_no_sitedc_no_secret_leak(tmp_path):
    env, cmdlog, _ = _setup(tmp_path)
    assert _run(env, "run").returncode == 0
    log = cmdlog.read_text()
    for bad in ("SendMessage", "DeleteMessage", "PurgeQueue", "ReceiveMessage"):
        assert bad not in log
    assert "sites/dc" not in log and _SENTINEL not in log


# --- CRI-Identitaetsgate (Erfassung) ---------------------------------------

def test_capture_aborts_when_cri_cannot_confirm(tmp_path):
    env, cmdlog, _ = _setup(tmp_path, FAKE_CRI_EMPTY="1")
    res = _run(env, "run")
    assert res.returncode != 0
    assert "deploy-consumer" not in cmdlog.read_text()


def test_capture_aborts_on_cri_digest_mismatch(tmp_path):
    # Pod-Digest weicht von der CRI-status.id ab -> helper exit 3 -> fail closed.
    env, cmdlog, _ = _setup(tmp_path, FAKE_POD_IMAGE_ID="sha256:" + "9" * 64)
    res = _run(env, "run")
    assert res.returncode != 0
    assert "deploy-consumer" not in cmdlog.read_text()


def test_existing_rollback_tag_same_identity_reused(tmp_path):
    env, _, _ = _setup(tmp_path, FAKE_RB_PREEXISTS="1", FAKE_RB_ID=_RID)
    assert _run(env, "run").returncode == 0
    # Kein ECHTES Tagging (Marker bleibt aus); der Detection-No-op zaehlt nicht.
    assert not pathlib.Path(env["CTR_TAG_MARKER"]).exists()


def test_existing_rollback_tag_divergent_identity_aborts(tmp_path):
    env, cmdlog, _ = _setup(tmp_path, FAKE_RB_PREEXISTS="1", FAKE_RB_ID="sha256:" + "c" * 64)
    res = _run(env, "run")
    assert res.returncode != 0
    assert "deploy-consumer" not in cmdlog.read_text()


def test_rollback_state_stores_full_digest_not_docker_id(tmp_path):
    env, _, state_dir = _setup(tmp_path)
    assert _run(env, "run").returncode == 0
    rb = json.loads((state_dir / "rollback.json").read_text())
    assert rb["runtime_id"] == _RID and rb["rollback_tag"] == _RB_REF
    assert "old_image_id" not in rb  # keine Docker-.Id mehr


def test_capture_rejects_pod_not_matching_deployment_image(tmp_path):
    # Bei der Rollback-Zielerfassung muss der gewaehlte Pod EXAKT zum gelesenen
    # Deployment-Image gehoeren. Hier laeuft der Pod mit einem ABWEICHENDEN Image ->
    # kein kohaerenter Kandidat -> fail closed, kein Deploy.
    env, cmdlog, state_dir = _setup(tmp_path, FAKE_CAPTURE_POD_IMAGE="inventory-consumer:STALE")
    res = _run(env, "run")
    assert res.returncode != 0
    log = cmdlog.read_text()
    assert "deploy-consumer" not in log
    # Bis consumer-schema-ready gekommen, Capture in consumer-deployed scheitert.
    assert json.loads((state_dir / "state.json").read_text())["step"] == "consumer-schema-ready"


def test_missing_pod_selector_fails_closed_in_preflight(tmp_path):
    # select-consumer-pod.py ist Pflichtabhaengigkeit: fehlt sie, bricht der Preflight
    # VOR jedem State-Schreiben/Mutation ab (nie als 'kein Pod'/'0 Restarts').
    env, cmdlog, state_dir = _setup(tmp_path,
                                    D3B2_POD_SELECT_HELPER=str(tmp_path / "nope.py"))
    res = _run(env, "preflight")
    assert res.returncode != 0
    assert not (state_dir / "state.json").exists()
    assert "build consumer-db-bootstrap" not in cmdlog.read_text()


def test_broken_pod_selector_fails_closed_in_preflight(tmp_path):
    bad = tmp_path / "broken-selector.py"
    bad.write_text("this is not valid python :::\n", encoding="utf-8")
    env, _, state_dir = _setup(tmp_path, D3B2_POD_SELECT_HELPER=str(bad))
    assert _run(env, "preflight").returncode != 0
    assert not (state_dir / "state.json").exists()


# --- Runtime-Verify-Konfiguration im Preflight validiert (Grenzwerte) -------

@pytest.mark.parametrize("var,bad", [
    ("D3B2_RUNTIME_VERIFY_ATTEMPTS", "0"),
    ("D3B2_RUNTIME_VERIFY_ATTEMPTS", "-1"),
    ("D3B2_RUNTIME_VERIFY_ATTEMPTS", "x"),
    ("D3B2_RUNTIME_VERIFY_ATTEMPTS", "1001"),     # > Obergrenze 1000
    ("D3B2_RUNTIME_VERIFY_INTERVAL", "-1"),
    ("D3B2_RUNTIME_VERIFY_INTERVAL", "x"),
    ("D3B2_RUNTIME_VERIFY_INTERVAL", "61"),       # > Obergrenze 60
    ("D3B2_RUNTIME_VERIFY_BUDGET_SECONDS", "0"),
    ("D3B2_RUNTIME_VERIFY_BUDGET_SECONDS", "-5"),
    ("D3B2_RUNTIME_VERIFY_BUDGET_SECONDS", "x"),
    ("D3B2_RUNTIME_VERIFY_BUDGET_SECONDS", "601"),  # > Obergrenze 600
])
def test_runtime_verify_config_validated_in_preflight(tmp_path, var, bad):
    env, cmdlog, state_dir = _setup(tmp_path, **{var: bad})
    res = _run(env, "preflight")
    assert res.returncode != 0
    assert not (state_dir / "state.json").exists()          # vor State-Schreiben
    assert "build consumer-db-bootstrap" not in cmdlog.read_text()  # vor jeder Mutation


def test_runtime_verify_config_upper_bounds_accepted(tmp_path):
    # Werte exakt an der Obergrenze sind gueltig (preflight schreibt State).
    env, _, state_dir = _setup(tmp_path, D3B2_RUNTIME_VERIFY_ATTEMPTS="1000",
                               D3B2_RUNTIME_VERIFY_INTERVAL="60",
                               D3B2_RUNTIME_VERIFY_BUDGET_SECONDS="600")
    assert _run(env, "preflight").returncode == 0
    assert json.loads((state_dir / "state.json").read_text())["step"] == "preflight"


# --- Rollback (CRI-verifiziert) --------------------------------------------

def test_deploy_failure_rolls_back_via_containerd_and_verifies(tmp_path):
    env, cmdlog, state_dir = _setup(tmp_path, FAKE_DEPLOY_RC="1")
    res = _run(env, "run")
    assert res.returncode != 0
    log = cmdlog.read_text()
    assert f"set image deploy/inventory-consumer consumer={_RB_REF}" in log
    assert "rollout undo" not in log
    assert json.loads((state_dir / "state.json").read_text())["step"] == "consumer-schema-ready"


# --- Neuer Release: CRI-Identitaet, nicht nur Spec-Tag ----------------------

def test_release_runtime_image_cri_verified(tmp_path):
    env, cmdlog, _ = _setup(tmp_path)
    assert _run(env, "run").returncode == 0
    log = cmdlog.read_text()
    # Release-Tag wird ueber CRI inspiziert (Praesenz + laufende Identitaet).
    assert f"crictl inspecti -o json {_RUNTIME_TAG}" in log


def test_release_verify_fails_if_running_digest_not_release_identity(tmp_path):
    # Capture (vor Deploy) matcht; NACH dem Deploy weicht die laufende Pod-Identitaet
    # von der CRI-Identitaet des Release-Tags ab -> bloss korrektes Spec-Image reicht
    # nicht -> fail closed.
    env, cmdlog, state_dir = _setup(tmp_path, FAKE_POD_IMAGE_ID_AFTER="sha256:" + "d" * 64)
    res = _run(env, "run")
    assert res.returncode != 0
    assert "deploy-consumer" in cmdlog.read_text()   # Deploy lief, Verifikation scheiterte
    assert json.loads((state_dir / "state.json").read_text())["step"] == "consumer-schema-ready"


# --- Release-Bindung / run-resume / Restart-Gate (Regression) --------------

def test_malformed_release_sha_rejected(tmp_path):
    env, _, _ = _setup(tmp_path, D3B2_RELEASE_SHA="nothex")
    assert _run(env, "run").returncode != 0


def test_resume_same_release_completes(tmp_path):
    env, cmdlog, state_dir = _setup(tmp_path)
    _simulate_prior_deploy(env)
    _seed_state(state_dir, "consumer-deployed", release=_SHA)
    res = _run(env, "resume")
    assert res.returncode == 0, res.stderr
    assert "deploy-consumer" not in cmdlog.read_text()
    assert json.loads((state_dir / "state.json").read_text())["step"] == "complete"


def test_resume_different_release_rejected(tmp_path):
    env, _, state_dir = _setup(tmp_path)
    _seed_state(state_dir, "consumer-deployed", release="f" * 40)
    assert _run(env, "resume").returncode != 0


def test_run_on_incomplete_state_aborts_to_resume(tmp_path):
    env, _, state_dir = _setup(tmp_path)
    _seed_state(state_dir, "queue-config-ready")
    res = _run(env, "run")
    assert res.returncode != 0 and "resume" in res.stderr.lower()


def test_run_on_complete_state_no_mutation(tmp_path):
    env, cmdlog, state_dir = _setup(tmp_path)
    _seed_state(state_dir, "complete", complete=True)
    assert _run(env, "run").returncode == 0
    log = cmdlog.read_text()
    assert "build consumer-db-bootstrap" not in log and "force-recreate" not in log
    assert not pathlib.Path(env["CTR_TAG_MARKER"]).exists()   # kein echtes Tagging


def test_resume_without_state_aborts(tmp_path):
    env, _, _ = _setup(tmp_path)
    assert _run(env, "resume").returncode != 0


def test_queue_ready_succeeds_on_later_retry(tmp_path):
    # ElasticMQ ist erst ab dem 3. ListQueues-Aufruf bereit (DLQ erscheint spaeter).
    env, cmdlog, state_dir = _setup(tmp_path, FAKE_QUEUE_READY_AFTER="3",
                                    D3B2_QUEUE_READY_ATTEMPTS="10", D3B2_QUEUE_READY_INTERVAL="0")
    res = _run(env, "run")
    assert res.returncode == 0, res.stderr
    log = cmdlog.read_text()
    # sqs-Neuerstellung GENAU EINMAL (ausserhalb der Retry-Schleife).
    assert log.count("up -d --force-recreate --no-deps sqs") == 1
    assert json.loads((state_dir / "state.json").read_text())["step"] == "complete"


def test_queue_readiness_timeout_fails_closed_at_images_built(tmp_path):
    # DLQ erscheint nie -> Readiness-Timeout -> fail closed.
    env, cmdlog, state_dir = _setup(tmp_path, FAKE_QUEUE_READY_AFTER="9999",
                                    D3B2_QUEUE_READY_ATTEMPTS="3", D3B2_QUEUE_READY_INTERVAL="0")
    res = _run(env, "run")
    assert res.returncode != 0
    log = cmdlog.read_text()
    # sqs nur einmal neu erstellt; danach nur read-only Retries.
    assert log.count("up -d --force-recreate --no-deps sqs") == 1
    # Keine DB-Setup-/Migrations-/Deploy-Phase betreten (Image-Build in 'images-built'
    # davor ist erlaubt; gemeint ist die DB-Kette ab consumer-db-ready).
    assert "run --rm --no-deps consumer-db-bootstrap" not in log
    assert "run --rm --no-deps consumer-migrate" not in log
    assert "deploy-consumer" not in log
    # State bleibt bei images-built (queue-config-ready NICHT geschrieben).
    assert json.loads((state_dir / "state.json").read_text())["step"] == "images-built"


def test_queue_readiness_retries_are_read_only(tmp_path):
    env, cmdlog, _ = _setup(tmp_path, FAKE_QUEUE_READY_AFTER="9999",
                            D3B2_QUEUE_READY_ATTEMPTS="3", D3B2_QUEUE_READY_INTERVAL="0")
    assert _run(env, "run").returncode != 0
    log = cmdlog.read_text()
    for bad in ("SendMessage", "DeleteMessage", "PurgeQueue", "ReceiveMessage", "redrive"):
        assert bad not in log


def test_queue_ready_immediate_success_still_works(tmp_path):
    # Default FAKE_QUEUE_READY_AFTER=1 -> sofortiger Erfolg, kein sleep noetig.
    env, cmdlog, state_dir = _setup(tmp_path)
    assert _run(env, "run").returncode == 0
    assert json.loads((state_dir / "state.json").read_text())["step"] == "complete"
    assert cmdlog.read_text().count("up -d --force-recreate --no-deps sqs") == 1


def test_queue_gate_failure_aborts_before_recreate(tmp_path):
    env, cmdlog, state_dir = _setup(tmp_path, QUEUE_GATE_CMD="false")
    assert _run(env, "run").returncode != 0
    assert "force-recreate --no-deps sqs" not in cmdlog.read_text()
    assert json.loads((state_dir / "state.json").read_text())["step"] == "images-built"


def test_unacked_restarts_fail_closed(tmp_path):
    env, _, _ = _setup(tmp_path, FAKE_RESTARTS="8")
    assert _run(env, "run").returncode != 0


def test_acked_restarts_proceed(tmp_path):
    env, _, state_dir = _setup(tmp_path, FAKE_RESTARTS="8", D3B2_ACK_CONSUMER_RESTARTS="1")
    assert _run(env, "run").returncode == 0
    assert json.loads((state_dir / "restart-ack.json").read_text())["acked_restart_count"] == 8


def test_build_before_migration(tmp_path):
    env, cmdlog, _ = _setup(tmp_path)
    assert _run(env, "run").returncode == 0
    log = cmdlog.read_text()
    assert _i(log, "build consumer-db-bootstrap") < _i(log, "run --rm --no-deps consumer-migrate")
    assert (_i(log, "run --rm --no-deps consumer-db-bootstrap")
            < _i(log, "run --rm --no-deps consumer-db-prepare")
            < _i(log, "run --rm --no-deps consumer-migrate"))


def test_monitoring_failure_resumable(tmp_path):
    env, cmdlog, state_dir = _setup(tmp_path, PROM_FAIL="1")
    assert _run(env, "run").returncode != 0
    assert json.loads((state_dir / "state.json").read_text())["step"] == "consumer-deployed"
    env2 = dict(env); env2["PROM_FAIL"] = "0"; cmdlog.write_text("", encoding="utf-8")
    assert _run(env2, "resume").returncode == 0
    assert "deploy-consumer" not in cmdlog.read_text()
    assert json.loads((state_dir / "state.json").read_text())["step"] == "complete"


def test_corrupt_state_resume_fails_closed(tmp_path):
    env, _, state_dir = _setup(tmp_path)
    state_dir.mkdir(parents=True)
    (state_dir / "state.json").write_text("{corrupt", encoding="utf-8")
    assert _run(env, "resume").returncode != 0


# --- Monitoring-Readiness (bounded, read-only, fail closed) -----------------

def test_monitoring_recreate_exactly_once_on_success(tmp_path):
    env, cmdlog, state_dir = _setup(tmp_path)
    assert _run(env, "run").returncode == 0
    log = cmdlog.read_text()
    assert log.count("up -d --force-recreate --no-deps prometheus") == 1
    assert json.loads((state_dir / "state.json").read_text())["step"] == "complete"


def test_monitoring_recreate_exactly_once_even_with_retries(tmp_path):
    # 0/0 Targets -> Waiter retryt; der Force-Recreate liegt AUSSERHALB der Schleife und
    # darf trotz mehrerer Verifikationsversuche genau einmal erfolgen.
    env, cmdlog, state_dir = _setup(tmp_path, PROM_FAIL="1")
    assert _run(env, "run").returncode != 0
    log = cmdlog.read_text()
    assert log.count("up -d --force-recreate --no-deps prometheus") == 1
    assert json.loads((state_dir / "state.json").read_text())["step"] == "consumer-deployed"


def test_monitoring_no_mutation_inside_retry(tmp_path):
    # Im Monitoring-Retry darf nichts neu erstellt/gestartet werden. Jede Mutation darf
    # nur GENAU EINMAL in ihrer eigenen Phase auftreten — der Retry verdoppelt nichts.
    env, cmdlog, _ = _setup(tmp_path, PROM_FAIL="1")
    assert _run(env, "run").returncode != 0
    log = cmdlog.read_text()
    assert log.count("up -d --force-recreate --no-deps prometheus") == 1   # nicht je Versuch
    assert log.count("up -d --force-recreate --no-deps sqs") == 1          # Queue-Phase, einmal
    assert "set image" not in log and "rollout restart" not in log


def test_monitoring_failure_leaves_state_consumer_deployed(tmp_path):
    env, _, state_dir = _setup(tmp_path, PROM_FAIL="1")
    assert _run(env, "run").returncode != 0
    assert json.loads((state_dir / "state.json").read_text())["step"] == "consumer-deployed"


def test_monitoring_success_writes_monitoring_ready(tmp_path):
    # Resume ab consumer-deployed: Monitoring wird verifiziert und der State durchlaeuft
    # monitoring-ready -> verified -> complete (nur bei bewiesenem Monitoring).
    env, cmdlog, state_dir = _setup(tmp_path)
    _simulate_prior_deploy(env)
    _seed_state(state_dir, "consumer-deployed")
    assert _run(env, "resume").returncode == 0
    assert json.loads((state_dir / "state.json").read_text())["step"] == "complete"


def test_publisher_target_fails_monitoring_closed(tmp_path):
    # Publisher-Target = Policy-Verletzung -> sofort fail closed, State bleibt stehen.
    env, out_dir, state_dir = _setup(tmp_path, PROM_PUBLISHER="1")
    res = _run(env, "run")
    assert res.returncode != 0
    assert json.loads((state_dir / "state.json").read_text())["step"] == "consumer-deployed"


def test_resume_from_consumer_deployed_skips_earlier_phases(tmp_path):
    env, cmdlog, state_dir = _setup(tmp_path)
    _simulate_prior_deploy(env)
    _seed_state(state_dir, "consumer-deployed")
    assert _run(env, "resume").returncode == 0
    log = cmdlog.read_text()
    # Keine Queue-/DB-/Migration-/Consumer-Deploy-Phase erneut.
    assert "force-recreate --no-deps sqs" not in log
    assert "run --rm --no-deps consumer-db-bootstrap" not in log
    assert "run --rm --no-deps consumer-migrate" not in log
    assert "deploy-consumer" not in log
    assert json.loads((state_dir / "state.json").read_text())["step"] == "complete"


@pytest.mark.parametrize("var,bad", [
    ("D3B2_MONITORING_VERIFY_ATTEMPTS", "0"),
    ("D3B2_MONITORING_VERIFY_ATTEMPTS", "-1"),
    ("D3B2_MONITORING_VERIFY_ATTEMPTS", "x"),
    ("D3B2_MONITORING_VERIFY_ATTEMPTS", "1001"),
    ("D3B2_MONITORING_VERIFY_INTERVAL", "-1"),
    ("D3B2_MONITORING_VERIFY_INTERVAL", "x"),
    ("D3B2_MONITORING_VERIFY_INTERVAL", "61"),
    ("D3B2_MONITORING_VERIFY_BUDGET_SECONDS", "0"),
    ("D3B2_MONITORING_VERIFY_BUDGET_SECONDS", "x"),
    ("D3B2_MONITORING_VERIFY_BUDGET_SECONDS", "601"),
])
def test_invalid_monitoring_config_fails_before_state(tmp_path, var, bad):
    env, cmdlog, state_dir = _setup(tmp_path, **{var: bad})
    res = _run(env, "preflight")
    assert res.returncode != 0
    assert not (state_dir / "state.json").exists()                 # vor State-Schreiben
    assert "build consumer-db-bootstrap" not in cmdlog.read_text()  # vor jeder Mutation


def test_monitoring_logs_no_raw_api_json(tmp_path):
    env, _, _ = _setup(tmp_path, PROM_FAIL="1")
    res = _run(env, "run")
    assert res.returncode != 0
    blob = res.stdout + res.stderr
    for leak in ("activeTargets", '"groups"', '"labels"', '"health"', '"data"'):
        assert leak not in blob


# --- Blocker 1: Resume prueft Voraussetzungen VOR jeder Mutation ------------

def test_resume_invalid_monitoring_config_fails_before_mutation(tmp_path):
    # Resume ab consumer-deployed mit ungueltiger Monitoring-Konfig -> Abbruch in
    # _validate_verify_prerequisites VOR mon-up/Force-Recreate; State unveraendert.
    env, cmdlog, state_dir = _setup(tmp_path, D3B2_MONITORING_VERIFY_BUDGET_SECONDS="0")
    _simulate_prior_deploy(env)
    _seed_state(state_dir, "consumer-deployed")
    res = _run(env, "resume")
    assert res.returncode != 0
    log = cmdlog.read_text()
    assert "mon up" not in log.replace("docker compose", "mon")  # defensiv
    assert "up -d --force-recreate --no-deps prometheus" not in log
    assert "force-recreate" not in log
    assert json.loads((state_dir / "state.json").read_text())["step"] == "consumer-deployed"


def test_resume_broken_kill_after_fails_before_mutation(tmp_path):
    # Resume mit nicht funktionierendem `timeout --kill-after` -> _ensure_kill_after in
    # den Voraussetzungen bricht VOR jeder Monitoring-Mutation ab (kein Force-Recreate).
    env, cmdlog, state_dir = _setup(tmp_path)
    fb = tmp_path / "bin"
    real_timeout = shutil.which("timeout") or "/usr/bin/timeout"
    # Fake timeout OHNE --kill-after-Unterstuetzung (Probe schlaegt fehl).
    (fb / "timeout").write_text(
        "#!/usr/bin/env bash\n"
        'case "$1" in --kill-after*) echo "timeout: unrecognized option" >&2; exit 125;; esac\n'
        f'exec {real_timeout} "$@"\n', encoding="utf-8")
    os.chmod(fb / "timeout", 0o755)
    _simulate_prior_deploy(env)
    _seed_state(state_dir, "consumer-deployed")
    res = _run(env, "resume")
    assert res.returncode != 0
    log = cmdlog.read_text()
    assert "up -d --force-recreate --no-deps prometheus" not in log
    assert "force-recreate" not in log
    assert json.loads((state_dir / "state.json").read_text())["step"] == "consumer-deployed"
