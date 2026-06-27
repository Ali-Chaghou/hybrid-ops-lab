"""Deterministische, read-only Consumer-Pod-Auswahl + Runtime-Verifikation (D3B2.1).

A) Unit-Tests von ops/deploy/select-consumer-pod.py (JSON ueber stdin), inkl. strenger
   Struktur-/Typvalidierung und Container-Auswahl OHNE Index-0-Fallback.
B) Bash-Source-Tests der vereinheitlichten _verify_consumer_runtime (EIN gemeinsames
   Zeitbudget fuer Spec-Image/Selektion/CRI-Identitaet/Health/Readiness). cri_inspect
   und die Runtime-Tool-Erkennung werden im Runner gestubbt; Fake-kubectl liefert
   Pod-Liste/Health/Readiness.
C) Bash-Source-Tests des restart_gate (Maximum, fail closed).
"""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[3]
SELECT = REPO / "ops/deploy/select-consumer-pod.py"
SCRIPT = REPO / "ops/deploy/upgrade-consumer-runtime.sh"

_TAG = "inventory-consumer:abc123def456"
_RID = "sha256:" + "a" * 64

# Exit-Codes des Selektors (vgl. select-consumer-pod.py).
_EXIT_INPUT = 2     # ungueltige/leere/malformed Eingabe / unerwartete Struktur
_EXIT_NOMATCH = 3   # kein passender Pod
_EXIT_ARGS = 4      # ungueltige CLI-Argumente


# ============================ A) Selektor-Unit-Tests ========================

def _pod(name, *, image=_TAG, ready=True, phase="Running", image_id=_RID,
         restart=0, terminating=False, start="2026-01-01T00:00:00Z", cname="consumer"):
    md = {"name": name, "creationTimestamp": start}
    md["deletionTimestamp"] = "2026-01-02T00:00:00Z" if terminating else None
    return {
        "metadata": md,
        "spec": {"containers": [{"name": cname, "image": image}]},
        "status": {"phase": phase, "startTime": start,
                   "containerStatuses": [{"name": cname, "ready": ready,
                                          "imageID": image_id, "restartCount": restart}]},
    }


def _multi_pod(name, *, consumer_image=_TAG, consumer_ready=True, consumer_image_id=_RID,
               consumer_restart=0, terminating=False, start="2026-01-01T00:00:00Z"):
    """Sidecar an Index 0, 'consumer' DAHINTER — prueft, dass nicht Index 0 genommen wird."""
    md = {"name": name, "creationTimestamp": start}
    md["deletionTimestamp"] = "2026-01-02T00:00:00Z" if terminating else None
    return {
        "metadata": md,
        "spec": {"containers": [
            {"name": "istio-proxy", "image": "proxy:dev"},
            {"name": "consumer", "image": consumer_image},
        ]},
        "status": {"phase": "Running", "startTime": start, "containerStatuses": [
            {"name": "istio-proxy", "ready": True, "imageID": "sha256:" + "b" * 64, "restartCount": 0},
            {"name": "consumer", "ready": consumer_ready, "imageID": consumer_image_id,
             "restartCount": consumer_restart},
        ]},
    }


def _sel(items, *args, raw=None):
    payload = raw if raw is not None else json.dumps({"items": items})
    r = subprocess.run([sys.executable, str(SELECT), *args], input=payload,
                       capture_output=True, text=True)
    return r.returncode, r.stdout.strip()


def _sel_full(items, *args, raw=None):
    payload = raw if raw is not None else json.dumps({"items": items})
    r = subprocess.run([sys.executable, str(SELECT), *args], input=payload,
                       capture_output=True, text=True)
    return r.returncode, r.stdout, r.stderr


def test_terminating_plus_new_ready_picks_new():
    old = _pod("consumer-OLD", terminating=True, start="2026-01-01T00:00:00Z")
    new = _pod("consumer-NEW", start="2026-01-02T00:00:00Z")
    rc, out = _sel([old, new], "--require-running", "--require-ready", "--print", "name")
    assert rc == 0 and out == "consumer-NEW"


def test_list_order_does_not_matter():
    old = _pod("consumer-OLD", terminating=True, start="2026-01-01T00:00:00Z")
    new = _pod("consumer-NEW", start="2026-01-02T00:00:00Z")
    rc1, o1 = _sel([old, new], "--require-ready", "--print", "name")
    rc2, o2 = _sel([new, old], "--require-ready", "--print", "name")
    assert rc1 == 0 and rc2 == 0 and o1 == o2 == "consumer-NEW"


def test_only_terminating_fails():
    rc, out = _sel([_pod("c", terminating=True)], "--print", "name")
    assert rc == _EXIT_NOMATCH and out == ""


def test_wrong_image_with_expect_fails():
    rc, _ = _sel([_pod("c", image="inventory-consumer:dev")], "--expect-image", _TAG, "--print", "name")
    assert rc == _EXIT_NOMATCH


def test_not_ready_with_require_ready_fails():
    rc, _ = _sel([_pod("c", ready=False)], "--require-ready", "--print", "name")
    assert rc == _EXIT_NOMATCH


def test_empty_image_id_with_require_image_id_fails():
    rc, _ = _sel([_pod("c", image_id="")], "--require-image-id", "--print", "imageID")
    assert rc == _EXIT_NOMATCH


def test_print_restart_count_zero_is_valid():
    rc, out = _sel([_pod("c", restart=0)], "--print", "restartCount")
    assert rc == 0 and out == "0"


def test_exit_codes_are_distinct():
    assert _sel(None, "--print", "name", raw="{not json")[0] == _EXIT_INPUT
    assert _sel(None, "--print", "name", raw="")[0] == _EXIT_INPUT
    assert _sel(None, "--print", "name", raw='{"items":"nope"}')[0] == _EXIT_INPUT
    assert _sel([_pod("c", terminating=True)], "--print", "name")[0] == _EXIT_NOMATCH
    assert _sel([], "--print", "name")[0] == _EXIT_NOMATCH
    assert _sel([], "--print", "bogus-field")[0] == _EXIT_ARGS


def test_youngest_among_valid_chosen():
    a = _pod("c-a", start="2026-01-01T00:00:00Z")
    b = _pod("c-b", start="2026-01-03T00:00:00Z")
    c = _pod("c-c", start="2026-01-02T00:00:00Z")
    rc, out = _sel([a, b, c], "--require-ready", "--print", "name")
    assert rc == 0 and out == "c-b"


def test_tie_break_is_deterministic_by_name():
    a = _pod("c-a", start="2026-01-01T00:00:00Z")
    b = _pod("c-b", start="2026-01-01T00:00:00Z")
    r1 = _sel([a, b], "--require-ready", "--print", "name")
    r2 = _sel([b, a], "--require-ready", "--print", "name")
    assert r1 == r2 == (0, "c-b")


# ---- Container-Auswahl OHNE Fallback ---------------------------------------

def test_single_misnamed_container_rejected():
    pod = _pod("c", cname="sidecar")
    assert _sel([pod], "--print", "name")[0] == _EXIT_NOMATCH


def test_single_unnamed_container_rejected():
    pod = {
        "metadata": {"name": "c", "deletionTimestamp": None, "creationTimestamp": "2026-01-01T00:00:00Z"},
        "spec": {"containers": [{"image": _TAG}]},
        "status": {"phase": "Running", "startTime": "2026-01-01T00:00:00Z",
                   "containerStatuses": [{"ready": True, "imageID": _RID, "restartCount": 0}]},
    }
    assert _sel([pod], "--print", "name")[0] == _EXIT_NOMATCH


def test_exact_consumer_accepted():
    rc, out = _sel([_pod("c", cname="consumer")], "--require-running", "--require-ready", "--print", "name")
    assert rc == 0 and out == "c"


def test_consumer_behind_sidecar_index1_selected():
    pod = _multi_pod("c-multi")
    rc, name = _sel([pod], "--expect-image", _TAG, "--require-running", "--require-ready",
                    "--require-image-id", "--print", "name")
    assert rc == 0 and name == "c-multi"
    rc2, iid = _sel([pod], "--print", "imageID")
    assert rc2 == 0 and iid == _RID  # consumer-imageID, nicht der Sidecar


def test_custom_container_flag_respected():
    pod = _pod("c", cname="worker")
    assert _sel([pod], "--print", "name")[0] == _EXIT_NOMATCH
    rc, out = _sel([pod], "--container", "worker", "--print", "name")
    assert rc == 0 and out == "c"


# ---- Strenge Struktur-/Typvalidierung -> Exit 2, kein Traceback ------------

def _raw(pod_or_root):
    return pod_or_root if isinstance(pod_or_root, str) else json.dumps(pod_or_root)


_BAD_INPUTS = {
    "root_not_object": "[]",
    "items_not_list": '{"items":{}}',
    "pod_not_object": '{"items":[1]}',
    "metadata_not_object": '{"items":[{"metadata":[],"spec":{"containers":[]},"status":{}}]}',
    "spec_not_object": '{"items":[{"metadata":{"name":"p"},"spec":[],"status":{}}]}',
    "status_not_object": '{"items":[{"metadata":{"name":"p"},"spec":{"containers":[]},"status":[]}]}',
    "containers_not_list": '{"items":[{"metadata":{"name":"p"},"spec":{"containers":{}},"status":{}}]}',
    "containerStatuses_not_list": '{"items":[{"metadata":{"name":"p"},"spec":{"containers":[]},"status":{"containerStatuses":{}}}]}',
    "pod_name_not_string": '{"items":[{"metadata":{"name":123},"spec":{"containers":[]},"status":{}}]}',
    "pod_name_empty": '{"items":[{"metadata":{"name":""},"spec":{"containers":[]},"status":{}}]}',
    "container_not_object": '{"items":[{"metadata":{"name":"p"},"spec":{"containers":[7]},"status":{}}]}',
    "container_name_not_string": '{"items":[{"metadata":{"name":"p"},"spec":{"containers":[{"name":5,"image":"i"}]},"status":{}}]}',
    "image_not_string": '{"items":[{"metadata":{"name":"p"},"spec":{"containers":[{"name":"consumer","image":9}]},"status":{}}]}',
    "imageid_not_string": '{"items":[{"metadata":{"name":"p"},"spec":{"containers":[{"name":"consumer","image":"i"}]},"status":{"containerStatuses":[{"name":"consumer","imageID":9}]}}]}',
    "ready_not_bool": '{"items":[{"metadata":{"name":"p"},"spec":{"containers":[{"name":"consumer","image":"i"}]},"status":{"containerStatuses":[{"name":"consumer","ready":"yes"}]}}]}',
    "restart_negative": '{"items":[{"metadata":{"name":"p"},"spec":{"containers":[{"name":"consumer","image":"i"}]},"status":{"containerStatuses":[{"name":"consumer","restartCount":-1}]}}]}',
    "restart_bool": '{"items":[{"metadata":{"name":"p"},"spec":{"containers":[{"name":"consumer","image":"i"}]},"status":{"containerStatuses":[{"name":"consumer","restartCount":true}]}}]}',
    "restart_text": '{"items":[{"metadata":{"name":"p"},"spec":{"containers":[{"name":"consumer","image":"i"}]},"status":{"containerStatuses":[{"name":"consumer","restartCount":"3"}]}}]}',
    "start_time_not_string": '{"items":[{"metadata":{"name":"p"},"spec":{"containers":[]},"status":{"startTime":42}}]}',
    "dup_spec_consumer": '{"items":[{"metadata":{"name":"p","deletionTimestamp":null},"spec":{"containers":[{"name":"consumer","image":"a"},{"name":"consumer","image":"b"}]},"status":{"containerStatuses":[{"name":"consumer","ready":true,"imageID":"i","restartCount":0}]}}]}',
    "dup_status_consumer": '{"items":[{"metadata":{"name":"p","deletionTimestamp":null},"spec":{"containers":[{"name":"consumer","image":"a"}]},"status":{"containerStatuses":[{"name":"consumer","imageID":"i","restartCount":0},{"name":"consumer","imageID":"j","restartCount":1}]}}]}',
    "broken_json": "{not json",
    "empty": "",
}


@pytest.mark.parametrize("key", sorted(_BAD_INPUTS))
@pytest.mark.parametrize("field", ["name", "imageID", "max-restart-count", "identity"])
def test_invalid_structures_exit_2_no_traceback(key, field):
    rc, out, err = _sel_full(None, "--print", field, raw=_BAD_INPUTS[key])
    assert rc == _EXIT_INPUT, (key, field, rc, err)
    assert out.strip() == ""
    assert "Traceback" not in err
    # Keine Pod-Rohdaten/Strukturschluessel im Fehlerkanal.
    for leak in ("containerStatuses", "deletionTimestamp", "restartCount", "imageID"):
        assert leak not in err


def test_duplicate_consumer_not_first_chosen():
    # Doppelter Consumer (Spec) wird abgelehnt, NICHT der erste Eintrag genommen.
    rc, out = _sel(None, "--print", "image", raw=_BAD_INPUTS["dup_spec_consumer"])
    assert rc == _EXIT_INPUT and out == ""


# ---- Restart-Gate-Aggregat -------------------------------------------------

def test_max_restart_across_pods_returns_highest():
    pods = [_pod("c-a", restart=0), _pod("c-b", restart=5), _pod("c-c", restart=2)]
    rc, out = _sel(pods, "--print", "max-restart-count")
    assert rc == 0 and out == "5"


def test_max_restart_excludes_terminating():
    pods = [_pod("c-old", restart=9, terminating=True), _pod("c-new", restart=1)]
    rc, out = _sel(pods, "--print", "max-restart-count")
    assert rc == 0 and out == "1"


def test_max_restart_no_pods_is_zero():
    assert _sel([], "--print", "max-restart-count") == (0, "0")
    assert _sel([_pod("c", terminating=True, restart=7)], "--print", "max-restart-count") == (0, "0")


def test_max_restart_multi_container_uses_consumer_count():
    pod = _multi_pod("c-multi", consumer_restart=4)  # Sidecar restart=0
    rc, out = _sel([pod], "--print", "max-restart-count")
    assert rc == 0 and out == "4"


def test_max_restart_fails_closed_on_broken_json():
    assert _sel(None, "--print", "max-restart-count", raw="{not json") == (_EXIT_INPUT, "")
    assert _sel(None, "--print", "max-restart-count", raw="") == (_EXIT_INPUT, "")


# ---- max-restart-count: Strukturstrenge (#6) -------------------------------

def test_max_restart_nonterminating_only_sidecar_exit2():
    pod = {
        "metadata": {"name": "p", "deletionTimestamp": None},
        "spec": {"containers": [{"name": "sidecar", "image": "x"}]},
        "status": {"containerStatuses": [{"name": "sidecar", "restartCount": 0}]},
    }
    assert _sel([pod], "--print", "max-restart-count") == (_EXIT_INPUT, "")


def test_max_restart_unnamed_single_container_exit2():
    pod = {
        "metadata": {"name": "p", "deletionTimestamp": None},
        "spec": {"containers": [{"image": "x"}]},
        "status": {"containerStatuses": [{"restartCount": 0}]},
    }
    assert _sel([pod], "--print", "max-restart-count") == (_EXIT_INPUT, "")


def test_max_restart_consumer_spec_without_containerstatuses_is_zero():
    # Dokumentiert: gueltiger Consumer-Spec, containerStatuses fehlt ganz -> 0 (fresh).
    pod = {
        "metadata": {"name": "p", "deletionTimestamp": None},
        "spec": {"containers": [{"name": "consumer", "image": "x"}]},
        "status": {},
    }
    assert _sel([pod], "--print", "max-restart-count") == (0, "0")


def test_max_restart_consumer_spec_statuses_present_no_consumer_status_is_zero():
    # Dokumentiert: containerStatuses vorhanden, aber Consumer-Status (noch) nicht ->
    # frisch startender Pod, uebrige Struktur konsistent -> 0.
    pod = {
        "metadata": {"name": "p", "deletionTimestamp": None},
        "spec": {"containers": [{"name": "consumer", "image": "x"}, {"name": "side", "image": "y"}]},
        "status": {"containerStatuses": [{"name": "side", "restartCount": 0}]},
    }
    assert _sel([pod], "--print", "max-restart-count") == (0, "0")


def test_max_restart_consumer_status_five_seen():
    pod = _pod("p", restart=5)
    assert _sel([pod], "--print", "max-restart-count") == (0, "5")


def test_max_restart_terminating_structurally_old_pod_not_source():
    # Terminierender, strukturell alter Pod (nur sidecar, restart 9) wird ausgeschlossen
    # und loest KEINEN Exit 2 aus; verbleibender Consumer bestimmt das Maximum.
    old = {
        "metadata": {"name": "old", "deletionTimestamp": "2026-01-02T00:00:00Z"},
        "spec": {"containers": [{"name": "sidecar", "image": "x"}]},
        "status": {"containerStatuses": [{"name": "sidecar", "restartCount": 9}]},
    }
    new = _pod("new", restart=1)
    assert _sel([old, new], "--print", "max-restart-count") == (0, "1")


# ---- identity-Mehrfeldformat ----------------------------------------------

def test_identity_outputs_name_and_imageid_only():
    secret = "SENTINEL_POD_SECRET_zzz"
    pod = _pod("c-secret")
    pod["metadata"]["annotations"] = {"leak": secret}
    rc, out = _sel([pod], "--require-running", "--require-ready", "--require-image-id", "--print", "identity")
    assert rc == 0
    obj = json.loads(out)
    assert obj == {"name": "c-secret", "imageID": _RID}
    assert secret not in out


def test_identity_requires_nonempty_imageid():
    rc, _ = _sel([_pod("c", image_id="")], "--print", "identity")
    assert rc == _EXIT_NOMATCH


# ============================ B) Vereinheitlichte Runtime-Verifikation ======

_FAKE_KUBECTL = r"""#!/usr/bin/env bash
echo "kubectl $*" >> "$KLOG"
case "$*" in
  *"get deploy"*)
    [ -n "${SPEC_SLEEP:-}" ] && { if [ "${SPEC_IGNORE_TERM:-0}" = "1" ]; then trap "" TERM; fi; sleep "${SPEC_SLEEP}"; }
    [ "${SPEC_RC:-0}" = "0" ] || exit "${SPEC_RC}"
    printf '%s' "${SPEC_IMAGE}"; exit 0 ;;
  *"rollout status"*) [ -n "${ROLLOUT_SLEEP:-}" ] && sleep "${ROLLOUT_SLEEP}"; exit "${ROLLOUT_RC:-0}" ;;
  *"get pod"*"-o json"*)
    [ -n "${GET_SLEEP:-}" ] && { if [ "${GET_IGNORE_TERM:-0}" = "1" ]; then trap "" TERM; fi; sleep "${GET_SLEEP}"; }
    [ "${KC_GET_RC:-0}" = "0" ] && { cat "$POD_JSON"; exit 0; } || exit "${KC_GET_RC}" ;;
  *exec*)
    [ -n "${EXEC_SLEEP:-}" ] && { if [ "${EXEC_IGNORE_TERM:-0}" = "1" ]; then trap "" TERM; fi; sleep "${EXEC_SLEEP}"; }
    c=$(cat "$EXEC_CNT" 2>/dev/null || echo 0); c=$((c+1)); echo "$c" > "$EXEC_CNT"
    if [ "$c" -lt "${EXEC_OK_FROM:-1}" ]; then exit "${EXEC_FAIL_CODE:-21}"; fi
    exit "${EXEC_FINAL_CODE:-0}" ;;
esac
exit 0
"""

# Runner mit GESTUBBTEM cri_inspect (Fokus: Pod-Selektion/Health/Readiness). Gibt nach
# dem Aufruf den Deadline-State aus, damit Tests das Cleanup von _VERIFY_DEADLINE pruefen.
_RUNNER = """#!/usr/bin/env bash
source "{script}"
_ensure_tools() {{ :; }}
detect_runtime_tools() {{ :; }}
[ -n "${{CRI_ID_HELPER_OVERRIDE:-}}" ] && CRI_ID_HELPER="${{CRI_ID_HELPER_OVERRIDE}}"
cri_inspect() {{
  if [ -n "${{CRI_STUB_SLEEP:-}}" ]; then sleep "${{CRI_STUB_SLEEP}}"; fi
  case "${{CRI_STUB_MODE:-ok}}" in
    ok)    printf '%s' "$CRI_JSON"; return 0 ;;
    error) return "${{CRI_STUB_RC:-7}}" ;;
    empty) return "${{_RC_CRI_EMPTY}}" ;;
  esac
}}
rc=0; _verify_consumer_runtime "$1" "$2" "$3" || rc=$?
echo "DEADLINE_AFTER=[${{_VERIFY_DEADLINE}}]"
exit $rc
"""

# Runner mit ECHTEM cri_inspect (Fokus: docker/crictl-Transport + Deadline-Bindung).
# _ensure_tools/detect_runtime_tools gestubbt; CRICTL_BIN/K3D_NODE gesetzt; ein Fake-
# `docker` auf PATH simuliert `docker exec <node> <crictl> inspecti -o json <ref>`.
_RUNNER_REALCRI = """#!/usr/bin/env bash
source "{script}"
_ensure_tools() {{ :; }}
detect_runtime_tools() {{ :; }}
CRICTL_BIN=/fake/crictl
CTR_BIN=/fake/ctr
K3D_NODE=fake-node
rc=0; _verify_consumer_runtime "$1" "$2" "$3" || rc=$?
echo "DEADLINE_AFTER=[${{_VERIFY_DEADLINE}}]"
exit $rc
"""

_FAKE_DOCKER_CRI = r"""#!/usr/bin/env bash
# Simuliert nur `docker exec <node> <crictl> inspecti -o json <ref>`.
case "$*" in
  *"inspecti -o json"*)
    if [ -n "${DOCKER_SLEEP:-}" ]; then
      [ "${DOCKER_IGNORE_TERM:-0}" = "1" ] && trap "" TERM
      sleep "${DOCKER_SLEEP}"
    fi
    case "${DOCKER_CRI_MODE:-ok}" in
      ok)    printf '%s' "$CRI_JSON"; exit 0 ;;
      error) exit "${DOCKER_CRI_RC:-7}" ;;     # docker/crictl-Transportfehler
      empty) printf ''; exit 0 ;;              # leer trotz Exit 0
    esac ;;
esac
exit 0
"""

pytestmark_b = pytest.mark.skipif(shutil.which("bash") is None or shutil.which("timeout") is None,
                                  reason="bash + timeout benoetigt")


def _verify(tmp_path, items, expect=_TAG, cri_ref=_TAG, expect_rid="", *,
            spec_image=None, spec_rc="0", spec_sleep="", rollout_rc="0", rollout_sleep="",
            kc_get_rc="0", get_sleep="", get_ignore_term="0",
            exec_ok_from="1", exec_fail_code="21", exec_final_code="0", exec_sleep="",
            exec_ignore_term="0", cri_json=None, cri_stub_mode="ok", cri_stub_rc="7",
            cri_stub_sleep="", attempts="5", interval="0", budget="30", kill_after="1",
            helper=None, pod_raw=None, extra=None):
    spec_image = expect if spec_image is None else spec_image
    if cri_json is None:
        cri_json = json.dumps({"status": {"id": _RID, "repoTags": ["t"], "repoDigests": []}})
    fb = tmp_path / "bin"; fb.mkdir()
    kc = fb / "kubectl"; kc.write_text(_FAKE_KUBECTL, encoding="utf-8"); kc.chmod(0o755)
    pod_json = tmp_path / "pods.json"
    pod_json.write_text(pod_raw if pod_raw is not None else json.dumps({"items": items}), encoding="utf-8")
    klog = tmp_path / "k.log"; klog.write_text("", encoding="utf-8")
    runner = tmp_path / "runner.sh"; runner.write_text(_RUNNER.format(script=SCRIPT), encoding="utf-8")
    env = dict(os.environ)
    env.update({
        "PATH": f"{fb}:{env['PATH']}", "KLOG": str(klog), "POD_JSON": str(pod_json),
        "SPEC_IMAGE": spec_image, "SPEC_RC": spec_rc, "SPEC_SLEEP": spec_sleep,
        "ROLLOUT_RC": rollout_rc, "ROLLOUT_SLEEP": rollout_sleep,
        "KC_GET_RC": kc_get_rc, "GET_SLEEP": get_sleep, "GET_IGNORE_TERM": get_ignore_term,
        "EXEC_CNT": str(tmp_path / "exec.cnt"), "EXEC_OK_FROM": exec_ok_from,
        "EXEC_FAIL_CODE": exec_fail_code, "EXEC_FINAL_CODE": exec_final_code,
        "EXEC_SLEEP": exec_sleep, "EXEC_IGNORE_TERM": exec_ignore_term,
        "CRI_JSON": cri_json, "CRI_STUB_MODE": cri_stub_mode, "CRI_STUB_RC": cri_stub_rc,
        "CRI_STUB_SLEEP": cri_stub_sleep,
        "D3B2_RUNTIME_VERIFY_ATTEMPTS": attempts, "D3B2_RUNTIME_VERIFY_INTERVAL": interval,
        "D3B2_RUNTIME_VERIFY_BUDGET_SECONDS": budget, "D3B2_VERIFY_KILL_AFTER": kill_after,
    })
    if helper is not None:
        env["D3B2_POD_SELECT_HELPER"] = str(helper)
    if extra:
        env.update(extra)
    r = subprocess.run(["bash", str(runner), expect, cri_ref, expect_rid],
                       env=env, capture_output=True, text=True, timeout=90)
    return r.returncode, klog.read_text(), (r.stdout + r.stderr)


def _verify_realcri(tmp_path, items, expect=_TAG, cri_ref=_TAG, expect_rid="", *,
                    docker_cri_mode="ok", docker_cri_rc="7", docker_sleep="",
                    docker_ignore_term="0", cri_json=None, attempts="5", interval="0",
                    budget="30", kill_after="1"):
    """Wie _verify, aber mit ECHTEM cri_inspect + Fake-`docker` fuer den CRI-Transport."""
    if cri_json is None:
        cri_json = json.dumps({"status": {"id": _RID, "repoTags": ["t"], "repoDigests": []}})
    fb = tmp_path / "bin"; fb.mkdir()
    (fb / "kubectl").write_text(_FAKE_KUBECTL, encoding="utf-8"); (fb / "kubectl").chmod(0o755)
    (fb / "docker").write_text(_FAKE_DOCKER_CRI, encoding="utf-8"); (fb / "docker").chmod(0o755)
    pod_json = tmp_path / "pods.json"; pod_json.write_text(json.dumps({"items": items}), encoding="utf-8")
    klog = tmp_path / "k.log"; klog.write_text("", encoding="utf-8")
    runner = tmp_path / "runner_realcri.sh"
    runner.write_text(_RUNNER_REALCRI.format(script=SCRIPT), encoding="utf-8")
    env = dict(os.environ)
    env.update({
        "PATH": f"{fb}:{env['PATH']}", "KLOG": str(klog), "POD_JSON": str(pod_json),
        "SPEC_IMAGE": expect, "SPEC_RC": "0", "ROLLOUT_RC": "0", "KC_GET_RC": "0",
        "EXEC_CNT": str(tmp_path / "exec.cnt"), "EXEC_OK_FROM": "1", "EXEC_FINAL_CODE": "0",
        "CRI_JSON": cri_json, "DOCKER_CRI_MODE": docker_cri_mode, "DOCKER_CRI_RC": docker_cri_rc,
        "DOCKER_SLEEP": docker_sleep, "DOCKER_IGNORE_TERM": docker_ignore_term,
        "D3B2_RUNTIME_VERIFY_ATTEMPTS": attempts, "D3B2_RUNTIME_VERIFY_INTERVAL": interval,
        "D3B2_RUNTIME_VERIFY_BUDGET_SECONDS": budget, "D3B2_VERIFY_KILL_AFTER": kill_after,
    })
    r = subprocess.run(["bash", str(runner), expect, cri_ref, expect_rid],
                       env=env, capture_output=True, text=True, timeout=90)
    return r.returncode, klog.read_text(), (r.stdout + r.stderr)


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash benoetigt")
def test_verify_picks_new_pod_over_terminating(tmp_path):
    old = _pod("consumer-OLD", terminating=True, start="2026-01-01T00:00:00Z")
    new = _pod("consumer-NEW", start="2026-01-02T00:00:00Z")
    rc, klog, _ = _verify(tmp_path, [old, new])
    assert rc == 0
    assert "exec consumer-NEW" in klog and "exec consumer-OLD" not in klog


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash benoetigt")
def test_verify_order_independent(tmp_path):
    old = _pod("consumer-OLD", terminating=True, start="2026-01-01T00:00:00Z")
    new = _pod("consumer-NEW", start="2026-01-02T00:00:00Z")
    rc, klog, _ = _verify(tmp_path, [new, old])
    assert rc == 0 and "exec consumer-NEW" in klog


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash benoetigt")
def test_verify_one_valid_among_many(tmp_path):
    pods = [
        _pod("c-term", terminating=True),
        _pod("c-wrongimg", image="inventory-consumer:dev"),
        _pod("c-notready", ready=False),
        _pod("consumer-NEW", start="2026-01-05T00:00:00Z"),
    ]
    rc, klog, _ = _verify(tmp_path, pods)
    assert rc == 0 and "exec consumer-NEW" in klog


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash benoetigt")
def test_verify_spec_image_mismatch_fails_fast(tmp_path):
    rc, klog, out = _verify(tmp_path, [_pod("consumer-NEW")], spec_image="inventory-consumer:OTHER")
    assert rc != 0
    assert "substep=spec-image" in out
    assert "get pod" not in klog and "exec" not in klog  # vor Selektion abgebrochen


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash benoetigt")
def test_verify_no_valid_pod_fails_closed(tmp_path):
    rc, klog, out = _verify(tmp_path, [_pod("c", terminating=True)], attempts="3")
    assert rc != 0
    assert "exec" not in klog
    assert "substep=pod-selection" in out


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash benoetigt")
def test_verify_cri_identity_mismatch_fails_closed(tmp_path):
    # Laufende Pod-Image-ID gehoert nicht zur CRI-Identitaet -> fail closed (kein exec).
    bad_cri = json.dumps({"status": {"id": "sha256:" + "d" * 64, "repoTags": ["t"], "repoDigests": []}})
    rc, klog, out = _verify(tmp_path, [_pod("consumer-NEW")], cri_json=bad_cri, attempts="3")
    assert rc != 0
    assert "substep=cri-identity" in out and "exec" not in klog


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash benoetigt")
def test_verify_rollback_rid_must_match(tmp_path):
    # expect_rid gesetzt: CRI loest auf, aber != gespeichertem rid -> fail closed.
    rid_other = "sha256:" + "e" * 64
    rc, _, out = _verify(tmp_path, [_pod("consumer-NEW")], expect_rid=rid_other, attempts="3")
    assert rc != 0 and "substep=cri-identity" in out


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash benoetigt")
def test_verify_pod_and_imageid_from_same_snapshot_cri_and_exec(tmp_path):
    # CRI-Identitaet (imageID) und Health/Readiness (Pod-Name) beziehen sich auf
    # denselben gewaehlten Pod -> Erfolg, exec auf consumer-NEW.
    rc, klog, _ = _verify(tmp_path, [_pod("consumer-NEW")])
    assert rc == 0 and "exec consumer-NEW" in klog


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash benoetigt")
def test_verify_readyz_initially_not_ready_then_ready(tmp_path):
    rc, _, _ = _verify(tmp_path, [_pod("consumer-NEW")], exec_ok_from="3",
                       exec_fail_code="22", exec_final_code="0", attempts="5")
    assert rc == 0


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash benoetigt")
def test_verify_permanent_health_failure(tmp_path):
    rc, _, out = _verify(tmp_path, [_pod("consumer-NEW")], exec_ok_from="1",
                         exec_final_code="21", attempts="3")
    assert rc != 0 and "substep=healthz" in out


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash benoetigt")
def test_verify_permanent_readiness_failure(tmp_path):
    rc, _, out = _verify(tmp_path, [_pod("consumer-NEW")], exec_ok_from="1",
                         exec_final_code="22", attempts="3")
    assert rc != 0 and "substep=readyz" in out


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash benoetigt")
def test_verify_no_mutation_in_retry(tmp_path):
    rc, klog, _ = _verify(tmp_path, [_pod("consumer-NEW")], exec_final_code="22", attempts="3")
    assert rc != 0
    for bad in ("set image", "scale", "delete", "rollout restart"):
        assert bad not in klog
    assert "publisher" not in klog.lower()


# ---- Retry-Klassifikation: nur Exit 3 retryfaehig --------------------------

@pytest.mark.skipif(shutil.which("bash") is None, reason="bash benoetigt")
def test_only_nomatch_is_retried(tmp_path):
    # 'kein Kandidat' (Exit 3): mehrere get-pod-Versuche.
    rc, klog, _ = _verify(tmp_path, [_pod("c", terminating=True)], attempts="3", interval="0")
    assert rc != 0
    assert klog.count("get pod") >= 2  # retryfaehig -> mehr als ein Versuch


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash benoetigt")
def test_kubectl_get_error_aborts_first_attempt(tmp_path):
    rc, klog, out = _verify(tmp_path, [_pod("consumer-NEW")], kc_get_rc="1", attempts="5")
    assert rc != 0
    assert klog.count("get pod") == 1   # sofort fatal, kein Retry
    assert "exec" not in klog and "substep=pod-selection" in out


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash benoetigt")
def test_helper_input_error_aborts_first_attempt(tmp_path):
    rc, klog, out = _verify(tmp_path, [], pod_raw="{not json", attempts="5")
    assert rc != 0
    assert klog.count("get pod") == 1   # Helper-Exit 2 -> sofort fatal
    assert "exec" not in klog


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash benoetigt")
def test_helper_argument_error_aborts_first_attempt(tmp_path):
    # Helper, der wie 'ungueltige CLI-Argumente' (Exit 4) reagiert -> sofort fatal.
    bad = tmp_path / "exit4.py"; bad.write_text("import sys; sys.exit(4)\n", encoding="utf-8")
    rc, klog, _ = _verify(tmp_path, [_pod("consumer-NEW")], helper=bad, attempts="5")
    assert rc != 0
    assert klog.count("get pod") == 1
    assert "exec" not in klog


# ---- Hartes Gesamtzeitbudget ----------------------------------------------
# Zulaessige Testtoleranz fuer ein 2s-Budget: budget(2s) + kill_after(1s) +
# Prozess-/CI-Overhead(~2s) = 5s harte Obergrenze. Bewusst eng (kein 8-11s mehr),
# aber stabil auf langsameren CI-Systemen. kill_after im Test = 1s.
_HARD_LIMIT_S = 5
_BUDGET = "2"
_NEEDS_KILL = pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("timeout") is None,
    reason="bash + GNU timeout (--kill-after) benoetigt")


@_NEEDS_KILL
def test_total_budget_bounds_runtime(tmp_path):
    import time
    t0 = time.monotonic()
    rc, _, out = _verify(tmp_path, [_pod("consumer-NEW")], exec_final_code="22",
                         attempts="1000", interval="1", budget=_BUDGET)
    elapsed = time.monotonic() - t0
    assert rc != 0 and elapsed < _HARD_LIMIT_S, f"Budget nicht hart begrenzt: {elapsed:.1f}s"
    assert "budget=2s" in out


@_NEEDS_KILL
def test_interval_larger_than_remaining_does_not_exceed_deadline(tmp_path):
    import time
    t0 = time.monotonic()
    rc, _, _ = _verify(tmp_path, [_pod("consumer-NEW")], exec_final_code="22",
                       attempts="100", interval="30", budget=_BUDGET)
    elapsed = time.monotonic() - t0
    assert rc != 0 and elapsed < _HARD_LIMIT_S, f"Sleep ueberschritt Deadline: {elapsed:.1f}s"


@_NEEDS_KILL
def test_blocking_get_is_killed_by_deadline(tmp_path):
    import time
    t0 = time.monotonic()
    rc, _, _ = _verify(tmp_path, [_pod("consumer-NEW")], get_sleep="30", attempts="100", budget=_BUDGET)
    elapsed = time.monotonic() - t0
    assert rc != 0 and elapsed < _HARD_LIMIT_S, f"blockierendes get nicht beendet: {elapsed:.1f}s"


@_NEEDS_KILL
def test_blocking_exec_is_killed_by_deadline(tmp_path):
    import time
    t0 = time.monotonic()
    rc, _, out = _verify(tmp_path, [_pod("consumer-NEW")], exec_sleep="30", attempts="100", budget=_BUDGET)
    elapsed = time.monotonic() - t0
    assert rc != 0 and elapsed < _HARD_LIMIT_S, f"blockierendes exec nicht beendet: {elapsed:.1f}s"
    assert "SENTINEL" not in out


@_NEEDS_KILL
def test_blocking_pod_selector_is_killed_by_deadline(tmp_path):
    # Blockierender Selektor-Helper -> _dl beendet ihn ueber das gemeinsame Budget.
    blocking = tmp_path / "block_sel.py"
    blocking.write_text("import time; time.sleep(30)\n", encoding="utf-8")
    import time
    t0 = time.monotonic()
    rc, _, _ = _verify(tmp_path, [_pod("consumer-NEW")], helper=blocking, attempts="100", budget=_BUDGET)
    elapsed = time.monotonic() - t0
    assert rc != 0 and elapsed < _HARD_LIMIT_S, f"blockierender Selektor nicht beendet: {elapsed:.1f}s"


@_NEEDS_KILL
def test_blocking_cri_identity_helper_is_killed_by_deadline(tmp_path):
    # Blockierender CRI-Identity-Helper -> _dl beendet ihn ueber das gemeinsame Budget.
    blocking = tmp_path / "block_cri.py"
    blocking.write_text("import time; time.sleep(30)\n", encoding="utf-8")
    import time
    t0 = time.monotonic()
    rc, _, _ = _verify(tmp_path, [_pod("consumer-NEW")], attempts="100", budget=_BUDGET,
                       extra={"CRI_ID_HELPER_OVERRIDE": str(blocking)})
    elapsed = time.monotonic() - t0
    assert rc != 0 and elapsed < _HARD_LIMIT_S, f"blockierender CRI-Helper nicht beendet: {elapsed:.1f}s"


@_NEEDS_KILL
def test_term_ignoring_process_hard_killed_by_kill_after(tmp_path):
    # Kernforderung #1: ein Prozess, der TERM IGNORIERT, wird per kill-after hart (KILL,
    # rc 137) innerhalb des kleinen Budgets beendet — kein unbeschraenktes Weiterlaufen.
    rc, _, out = _verify(tmp_path, [_pod("consumer-NEW")], exec_sleep="30",
                         exec_ignore_term="1", attempts="100", budget=_BUDGET, kill_after="1")
    assert rc != 0
    assert "substep=exec-timeout" in out


@_NEEDS_KILL
def test_dl_wrapper_hard_kills_term_ignoring_process(tmp_path):
    # Direkter Wrapper-Test: _dl beendet `trap "" TERM; sleep 30` per KILL (rc 137).
    runner = tmp_path / "dl.sh"
    runner.write_text(
        f'#!/usr/bin/env bash\nsource "{SCRIPT}"\n'
        'RUNTIME_VERIFY_BUDGET=2; _begin_verify_deadline\n'
        'rc=0; _dl bash -c \'trap "" TERM; sleep 30\' >/dev/null 2>&1 || rc=$?\n'
        'echo "DL_RC=$rc"\n', encoding="utf-8")
    import time
    t0 = time.monotonic()
    r = subprocess.run(["bash", str(runner)], env={**os.environ, "D3B2_VERIFY_KILL_AFTER": "1"},
                       capture_output=True, text=True, timeout=30)
    elapsed = time.monotonic() - t0
    assert "DL_RC=137" in r.stdout, r.stdout + r.stderr
    assert elapsed < _HARD_LIMIT_S, f"kill-after nicht hart: {elapsed:.1f}s"


@_NEEDS_KILL
def test_cri_health_ready_share_single_budget(tmp_path):
    import time
    t0 = time.monotonic()
    rc, _, out = _verify(tmp_path, [_pod("consumer-NEW")], exec_final_code="22",
                         attempts="1000", interval="1", budget=_BUDGET)
    assert rc != 0 and (time.monotonic() - t0) < _HARD_LIMIT_S
    assert "budget=2s" in out


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash benoetigt")
def test_diagnostics_complete_and_secret_free(tmp_path):
    secret = "SENTINEL_DIAG_SECRET_zzz"
    pod = _pod("consumer-DIAG"); pod["metadata"]["annotations"] = {"leak": secret}
    rc, _, out = _verify(tmp_path, [pod], exec_final_code="22", attempts="2")
    assert rc != 0
    for token in ("substep=readyz", "pod=consumer-DIAG", "versuch=", "exit=",
                  "budget=", "elapsed=", "remaining="):
        assert token in out, token
    assert secret not in out
    assert "deletionTimestamp" not in out and "containerStatuses" not in out


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash benoetigt")
def test_success_within_budget_possible(tmp_path):
    rc, _, _ = _verify(tmp_path, [_pod("consumer-NEW")], budget="30", attempts="5")
    assert rc == 0


# ---- Spec-Image-Exit-Code (#4): mismatch / kubectl-Fehler / Timeout --------

@pytest.mark.skipif(shutil.which("bash") is None, reason="bash benoetigt")
def test_spec_image_mismatch_diagnosed(tmp_path):
    rc, klog, out = _verify(tmp_path, [_pod("consumer-NEW")], spec_image="inventory-consumer:OTHER")
    assert rc != 0
    assert "substep=spec-image" in out and "abbruch=image-mismatch" in out
    assert "get pod" not in klog  # vor Selektion abgebrochen


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash benoetigt")
def test_spec_image_kubectl_error_fails_closed(tmp_path):
    rc, klog, out = _verify(tmp_path, [_pod("consumer-NEW")], spec_rc="1", attempts="5")
    assert rc != 0
    assert "substep=spec-image" in out and "abbruch=fatal-kubectl" in out
    assert "get pod" not in klog


@_NEEDS_KILL
def test_spec_image_timeout_diagnosed(tmp_path):
    import time
    t0 = time.monotonic()
    rc, _, out = _verify(tmp_path, [_pod("consumer-NEW")], spec_sleep="30", budget=_BUDGET)
    elapsed = time.monotonic() - t0
    assert rc != 0 and elapsed < _HARD_LIMIT_S
    assert "substep=spec-image" in out and "abbruch=timeout" in out


# ---- Deadline-Cleanup auf jedem Return-Pfad (#5) ---------------------------

@pytest.mark.skipif(shutil.which("bash") is None, reason="bash benoetigt")
def test_deadline_cleared_after_success(tmp_path):
    rc, _, out = _verify(tmp_path, [_pod("consumer-NEW")])
    assert rc == 0 and "DEADLINE_AFTER=[]" in out


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash benoetigt")
def test_deadline_cleared_after_spec_error(tmp_path):
    rc, _, out = _verify(tmp_path, [_pod("consumer-NEW")], spec_rc="1")
    assert rc != 0 and "DEADLINE_AFTER=[]" in out


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash benoetigt")
def test_deadline_cleared_after_fatal_selector(tmp_path):
    rc, _, out = _verify(tmp_path, [], pod_raw="{not json")
    assert rc != 0 and "DEADLINE_AFTER=[]" in out


@_NEEDS_KILL
def test_deadline_cleared_after_timeout(tmp_path):
    rc, _, out = _verify(tmp_path, [_pod("consumer-NEW")], get_sleep="30", budget=_BUDGET)
    assert rc != 0 and "DEADLINE_AFTER=[]" in out


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash benoetigt")
def test_deadline_cleared_after_retry_timeout(tmp_path):
    rc, _, out = _verify(tmp_path, [_pod("consumer-NEW")], exec_final_code="22", attempts="2")
    assert rc != 0 and "DEADLINE_AFTER=[]" in out


# ---- CRI-Exit-Codes / Transport (#3, gestubbtes cri_inspect + echtes docker) --

@pytest.mark.skipif(shutil.which("bash") is None, reason="bash benoetigt")
def test_cri_identity_exit2_aborts_first_attempt(tmp_path):
    # CRI-JSON ohne status.id -> cri-image-identity.py Exit 2 -> sofort fatal.
    bad = json.dumps({"status": {"repoTags": ["t"]}})
    rc, klog, out = _verify(tmp_path, [_pod("consumer-NEW")], cri_json=bad, attempts="5")
    assert rc != 0
    assert "substep=cri-identity" in out and "abbruch=fatal-input" in out
    assert klog.count("get pod") == 1   # kein Retry
    assert "exec" not in klog


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash benoetigt")
def test_cri_identity_exit4_aborts_first_attempt(tmp_path):
    # Helper, der Exit 4 (ungueltige Argumente) liefert -> sofort fatal.
    bad = tmp_path / "cri4.py"; bad.write_text("import sys; sys.exit(4)\n", encoding="utf-8")
    rc, klog, out = _verify(tmp_path, [_pod("consumer-NEW")], attempts="5",
                            extra={"CRI_ID_HELPER_OVERRIDE": str(bad)})
    assert rc != 0
    assert "substep=cri-identity" in out and "abbruch=fatal-args" in out
    assert klog.count("get pod") == 1 and "exec" not in klog


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash benoetigt")
def test_cri_identity_exit3_is_retried(tmp_path):
    # status.id passt nicht zur Pod-imageID -> Helper Exit 3 (Digest-Mismatch) ->
    # begrenzt retryfaehig (mehrere get-pod-Versuche), nie exec.
    mismatch = json.dumps({"status": {"id": "sha256:" + "d" * 64, "repoTags": ["t"], "repoDigests": []}})
    rc, klog, out = _verify(tmp_path, [_pod("consumer-NEW")], cri_json=mismatch, attempts="3", interval="0")
    assert rc != 0
    assert "substep=cri-identity" in out
    assert klog.count("get pod") >= 2 and "exec" not in klog


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash benoetigt")
def test_cri_docker_transport_error_diagnosed(tmp_path):
    # ECHTES cri_inspect + Fake-docker, der mit Code 7 (Transportfehler) abbricht.
    rc, klog, out = _verify_realcri(tmp_path, [_pod("consumer-NEW")],
                                    docker_cri_mode="error", docker_cri_rc="7", attempts="2")
    assert rc != 0
    assert "substep=cri-transport" in out
    assert "exec" not in klog


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash benoetigt")
def test_cri_empty_despite_exit0_diagnosed(tmp_path):
    rc, klog, out = _verify_realcri(tmp_path, [_pod("consumer-NEW")],
                                    docker_cri_mode="empty", attempts="2")
    assert rc != 0
    assert "substep=cri-transport" in out  # _RC_CRI_EMPTY -> transienter Transport
    assert "exec" not in klog


@_NEEDS_KILL
def test_cri_docker_timeout_diagnosed(tmp_path):
    # Blockierendes docker/crictl -> per Deadline beendet -> als cri-timeout diagnostiziert.
    import time
    t0 = time.monotonic()
    rc, klog, out = _verify_realcri(tmp_path, [_pod("consumer-NEW")], docker_sleep="30",
                                    attempts="100", budget=_BUDGET)
    elapsed = time.monotonic() - t0
    assert rc != 0 and elapsed < _HARD_LIMIT_S
    assert "substep=cri-timeout" in out and "exec" not in klog


@_NEEDS_KILL
def test_cri_docker_term_ignoring_hard_killed(tmp_path):
    import time
    t0 = time.monotonic()
    rc, _, out = _verify_realcri(tmp_path, [_pod("consumer-NEW")], docker_sleep="30",
                                 docker_ignore_term="1", attempts="100", budget=_BUDGET)
    elapsed = time.monotonic() - t0
    assert rc != 0 and elapsed < _HARD_LIMIT_S
    assert "substep=cri-timeout" in out


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash benoetigt")
def test_cri_failure_no_secret_or_full_json_in_log(tmp_path):
    secret = "SENTINEL_CRI_SECRET_zzz"
    # Digest-Mismatch erzwingen, damit die cri-identity-Diagnose greift.
    mismatch = json.dumps({"status": {"id": "sha256:" + "d" * 64, "secret": secret, "repoTags": ["t"]}})
    rc, _, out = _verify(tmp_path, [_pod("consumer-NEW")], cri_json=mismatch, attempts="2")
    assert rc != 0
    assert secret not in out
    assert '"status"' not in out and "repoDigests" not in out  # kein volles CRI-JSON


def test_rollback_path_uses_single_verify_budget():
    # Strukturzusicherung: kein separater Zweit-Retry; der Rollback-Pfad ruft die
    # vereinheitlichte Verifikation (EIN Budget) auf und nutzt KEIN _select_pod_retry.
    src = SCRIPT.read_text(encoding="utf-8")
    assert "_select_pod_retry" not in src
    rb = src[src.index("_rollback_consumer()"):src.index("# ---- Ablaufsteuerung")]
    assert rb.count("_verify_consumer_runtime") == 1
    assert "rollout status" not in rb  # Rollout laeuft jetzt in der vereinheitlichten Verifikation


# ============================ C) Restart-Gate (source) =====================

_FAKE_KUBECTL_RG = r"""#!/usr/bin/env bash
case "$*" in
  *"get pod"*"-o json"*) [ "${KC_GET_RC:-0}" = "0" ] && { cat "$POD_JSON"; exit 0; } || exit "${KC_GET_RC}" ;;
esac
exit 0
"""


def _restart_gate(tmp_path, *, raw, ack="0", kc_get_rc="0", helper=None):
    fb = tmp_path / "bin"; fb.mkdir()
    kc = fb / "kubectl"; kc.write_text(_FAKE_KUBECTL_RG, encoding="utf-8"); kc.chmod(0o755)
    pod_json = tmp_path / "pods.json"; pod_json.write_text(raw, encoding="utf-8")
    state_dir = tmp_path / "state"
    runner = tmp_path / "rg.sh"
    runner.write_text(f'#!/usr/bin/env bash\nsource "{SCRIPT}"\nrestart_gate\necho "RG_OK"\n', encoding="utf-8")
    env = dict(os.environ)
    env.update({
        "PATH": f"{fb}:{env['PATH']}", "POD_JSON": str(pod_json), "KC_GET_RC": kc_get_rc,
        "D3B2_STATE_DIR": str(state_dir), "D3B2_ACK_CONSUMER_RESTARTS": ack,
    })
    if helper is not None:
        env["D3B2_POD_SELECT_HELPER"] = str(helper)
    r = subprocess.run(["bash", str(runner)], env=env, capture_output=True, text=True, timeout=60)
    return r.returncode, (r.stdout + r.stderr), state_dir


def _rg_pods(*counts, terminating_count=None):
    items = [_pod(f"c-{i}", restart=c) for i, c in enumerate(counts)]
    if terminating_count is not None:
        items.append(_pod("c-term", restart=terminating_count, terminating=True))
    return json.dumps({"items": items})


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash benoetigt")
def test_restart_gate_uses_highest_count_unacked_fails_closed(tmp_path):
    rc, out, _ = _restart_gate(tmp_path, raw=_rg_pods(0, 5, 2), ack="0")
    assert rc != 0 and "RG_OK" not in out
    assert "5 Restart" in out


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash benoetigt")
def test_restart_gate_acks_highest_count(tmp_path):
    rc, out, state_dir = _restart_gate(tmp_path, raw=_rg_pods(0, 5, 2), ack="1")
    assert rc == 0 and "RG_OK" in out
    audit = json.loads((state_dir / "restart-ack.json").read_text())
    assert audit["acked_restart_count"] == 5


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash benoetigt")
def test_restart_gate_ignores_terminating_pod_restarts(tmp_path):
    rc, out, _ = _restart_gate(tmp_path, raw=_rg_pods(0, 0, terminating_count=9), ack="0")
    assert rc == 0 and "RG_OK" in out


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash benoetigt")
def test_restart_gate_no_pods_is_zero(tmp_path):
    rc, out, _ = _restart_gate(tmp_path, raw='{"items":[]}', ack="0")
    assert rc == 0 and "RG_OK" in out


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash benoetigt")
def test_restart_gate_broken_json_fails_closed_not_zero(tmp_path):
    rc, out, _ = _restart_gate(tmp_path, raw="{not json", ack="0")
    assert rc != 0 and "RG_OK" not in out
    assert "nicht ermittelbar" in out


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash benoetigt")
def test_restart_gate_kubectl_error_fails_closed_not_zero(tmp_path):
    rc, out, _ = _restart_gate(tmp_path, raw='{"items":[]}', kc_get_rc="1", ack="0")
    assert rc != 0 and "RG_OK" not in out


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash benoetigt")
def test_restart_gate_missing_helper_fails_closed(tmp_path):
    rc, out, _ = _restart_gate(tmp_path, raw=_rg_pods(0), ack="0",
                               helper=tmp_path / "does-not-exist.py")
    assert rc != 0 and "RG_OK" not in out
    assert "Pod-Selektor" in out


# ---- Restart-Gate: Strukturstrenge (#6, source-level) ----------------------

_RG_SIDECAR_ONLY = json.dumps({"items": [{
    "metadata": {"name": "p", "deletionTimestamp": None},
    "spec": {"containers": [{"name": "sidecar", "image": "x"}]},
    "status": {"containerStatuses": [{"name": "sidecar", "restartCount": 0}]},
}]})

_RG_UNNAMED = json.dumps({"items": [{
    "metadata": {"name": "p", "deletionTimestamp": None},
    "spec": {"containers": [{"image": "x"}]},
    "status": {"containerStatuses": [{"restartCount": 0}]},
}]})


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash benoetigt")
def test_restart_gate_sidecar_only_pod_fails_closed(tmp_path):
    # Nicht terminierender Pod ohne Consumer-Spec -> Selektor Exit 2 -> Gate fail closed.
    rc, out, _ = _restart_gate(tmp_path, raw=_RG_SIDECAR_ONLY, ack="0")
    assert rc != 0 and "RG_OK" not in out
    assert "nicht ermittelbar" in out


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash benoetigt")
def test_restart_gate_unnamed_container_fails_closed(tmp_path):
    rc, out, _ = _restart_gate(tmp_path, raw=_RG_UNNAMED, ack="0")
    assert rc != 0 and "RG_OK" not in out


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash benoetigt")
def test_restart_gate_consumer_spec_without_statuses_is_zero(tmp_path):
    raw = json.dumps({"items": [{
        "metadata": {"name": "p", "deletionTimestamp": None},
        "spec": {"containers": [{"name": "consumer", "image": "x"}]},
        "status": {},
    }]})
    rc, out, _ = _restart_gate(tmp_path, raw=raw, ack="0")
    assert rc == 0 and "RG_OK" in out   # dokumentiert: frischer Pod -> 0, kein Ack noetig


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash benoetigt")
def test_restart_gate_consumer_status_five_seen_unacked(tmp_path):
    rc, out, _ = _restart_gate(tmp_path, raw=_rg_pods(5), ack="0")
    assert rc != 0 and "5 Restart" in out   # Gate sieht 5


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash benoetigt")
def test_restart_gate_terminating_old_pod_not_restart_source(tmp_path):
    # Terminierender, strukturell alter Pod (nur sidecar, restart 9) wird ignoriert;
    # lebender Consumer (0) bestimmt das Maximum -> kein Ack noetig.
    raw = json.dumps({"items": [
        {"metadata": {"name": "old", "deletionTimestamp": "2026-01-02T00:00:00Z"},
         "spec": {"containers": [{"name": "sidecar", "image": "x"}]},
         "status": {"containerStatuses": [{"name": "sidecar", "restartCount": 9}]}},
        json.loads(_rg_pods(0))["items"][0],
    ]})
    rc, out, _ = _restart_gate(tmp_path, raw=raw, ack="0")
    assert rc == 0 and "RG_OK" in out
