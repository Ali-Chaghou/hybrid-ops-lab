"""Bounded, read-only Prometheus-Readiness-/Monitoring-Verifikation (D3B2.1).

Source-Tests von _verify_monitoring_ready in ops/deploy/upgrade-consumer-runtime.sh:
der Source-Guard erlaubt das `source`n; ein Fake-`curl` auf PATH simuliert /-/ready,
die Targets-API und die Rules-API mit pro-Endpoint-Aufrufzaehlern, sodass transiente
Startupzustaende (conn-refused -> ready, 0/0 -> Targets, down -> up, fehlende Rule ->
geladen) modelliert werden koennen. Das echte structurelle JSON-Parsing des Skripts
wird benutzt (kein gefakter Parser).
"""
from __future__ import annotations

import os
import pathlib
import shutil
import subprocess

import pytest

REPO = pathlib.Path(__file__).resolve().parents[3]
SCRIPT = REPO / "ops/deploy/upgrade-consumer-runtime.sh"

# Fake-curl: letztes Argument ist die URL. /-/ready -> HTTP-Code (oder exit 7 =
# connection refused); targets/rules -> Body + "\n" + HTTP-Code (das echte Skript nutzt
# `-w '\n%{http_code}'`). Pro-Endpoint-Zaehler erlauben einen Wechsel von einem "frueh"-
# Zustand zum kanonischen OK ab dem N-ten Aufruf. Alle Success-Bodies tragen realistisch
# {"status":"success","data":{...}}; adversariale Varianten decken status:error sowie
# success-foermige Bodies mit Nicht-2xx ab.
_FAKE_CURL = r"""#!/usr/bin/env bash
echo "curl $*" >> "$CURLLOG"
url=""; for a in "$@"; do url="$a"; done
inc() { local c; c=$(cat "$1" 2>/dev/null || echo 0); c=$((c+1)); echo "$c" > "$1"; printf '%s' "$c"; }

t_ok='{"status":"success","data":{"activeTargets":[{"labels":{"job":"consumer"},"health":"up"}]}}'
t_empty='{"status":"success","data":{"activeTargets":[]}}'
t_down='{"status":"success","data":{"activeTargets":[{"labels":{"job":"consumer"},"health":"down"}]}}'
t_pub='{"status":"success","data":{"activeTargets":[{"labels":{"job":"consumer"},"health":"up"},{"labels":{"job":"publisher"},"health":"up"}]}}'
t_bad='this is not json'
t_emptyobj='{}'
t_errstatus='{"status":"error","errorType":"internal","data":{"activeTargets":[{"labels":{"job":"consumer"},"health":"up"}]}}'
r_ok='{"status":"success","data":{"groups":[{"name":"consumer"},{"name":"queue"}]}}'
r_noc='{"status":"success","data":{"groups":[{"name":"queue"}]}}'
r_noq='{"status":"success","data":{"groups":[{"name":"consumer"}]}}'
r_bad='not json at all'
r_errstatus='{"status":"error","errorType":"internal","data":{"groups":[{"name":"consumer"},{"name":"queue"}]}}'

pick_t() { case "$1" in ok) printf '%s' "$t_ok";; empty) printf '%s' "$t_empty";; down) printf '%s' "$t_down";; pub) printf '%s' "$t_pub";; bad) printf '%s' "$t_bad";; emptyobj) printf '%s' "$t_emptyobj";; errstatus) printf '%s' "$t_errstatus";; *) printf '%s' "$t_ok";; esac; }
pick_r() { case "$1" in ok) printf '%s' "$r_ok";; noc) printf '%s' "$r_noc";; noq) printf '%s' "$r_noq";; bad) printf '%s' "$r_bad";; errstatus) printf '%s' "$r_errstatus";; *) printf '%s' "$r_ok";; esac; }

case "$url" in
  *-/ready*)
    n=$(inc "$READY_CNT")
    if [ "$n" -lt "${READY_OK_FROM:-1}" ]; then
      [ "${READY_EARLY:-notready}" = "refused" ] && exit 7
      printf '503'; exit 0
    fi
    printf '200'; exit 0 ;;
  *api/v1/targets*)
    n=$(inc "$TARG_CNT")
    if [ "$n" -lt "${TARG_OK_FROM:-1}" ]; then pick_t "${TARG_EARLY:-empty}"; else pick_t "${TARG_FINAL:-ok}"; fi
    printf '\n%s' "${TARG_HTTP:-200}"; exit 0 ;;
  *api/v1/rules*)
    n=$(inc "$RULE_CNT")
    if [ "$n" -lt "${RULE_OK_FROM:-1}" ]; then pick_r "${RULE_EARLY:-ok}"; else pick_r "${RULE_FINAL:-ok}"; fi
    printf '\n%s' "${RULE_HTTP:-200}"; exit 0 ;;
esac
printf '{}'; exit 0
"""

# Fake-docker: darf vom read-only Waiter NIE aufgerufen werden (keine Mutation).
_FAKE_DOCKER = """#!/usr/bin/env bash
echo "docker $*" >> "$DOCKERLOG"
exit 0
"""

_RUNNER = """#!/usr/bin/env bash
source "{script}"
rc=0; _verify_monitoring_ready || rc=$?
echo "DEADLINE_AFTER=[${{_VERIFY_DEADLINE}}]"
exit $rc
"""

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("timeout") is None or shutil.which("python3") is None,
    reason="bash + GNU timeout + python3 benoetigt")


def _run(tmp_path, *, attempts="4", interval="0", budget="10", python_stub=None, **knobs):
    fb = tmp_path / "bin"; fb.mkdir()
    (fb / "curl").write_text(_FAKE_CURL, encoding="utf-8"); (fb / "curl").chmod(0o755)
    (fb / "docker").write_text(_FAKE_DOCKER, encoding="utf-8"); (fb / "docker").chmod(0o755)
    if python_stub is not None:
        # Schattet das echte python3 NUR fuer diesen Lauf (Parser-Fehler-/Timeout-Szenarien).
        (fb / "python3").write_text(python_stub, encoding="utf-8"); (fb / "python3").chmod(0o755)
    curllog = tmp_path / "curl.log"; curllog.write_text("", encoding="utf-8")
    dockerlog = tmp_path / "docker.log"; dockerlog.write_text("", encoding="utf-8")
    runner = tmp_path / "runner.sh"; runner.write_text(_RUNNER.format(script=SCRIPT), encoding="utf-8")
    env = dict(os.environ)
    env.update({
        "PATH": f"{fb}:{env['PATH']}",
        "CURLLOG": str(curllog), "DOCKERLOG": str(dockerlog),
        "READY_CNT": str(tmp_path / "ready.cnt"), "TARG_CNT": str(tmp_path / "targ.cnt"),
        "RULE_CNT": str(tmp_path / "rule.cnt"),
        "D3B2_MONITORING_VERIFY_ATTEMPTS": attempts,
        "D3B2_MONITORING_VERIFY_INTERVAL": interval,
        "D3B2_MONITORING_VERIFY_BUDGET_SECONDS": budget,
    })
    for k, v in knobs.items():
        env[k] = v
    r = subprocess.run(["bash", str(runner)], env=env, capture_output=True, text=True, timeout=60)
    out = r.stdout + r.stderr
    return r.returncode, out, curllog.read_text(), dockerlog.read_text()


# 1) immediate readiness
def test_immediate_readiness_succeeds(tmp_path):
    rc, out, _, dlog = _run(tmp_path)
    assert rc == 0
    assert "DEADLINE_AFTER=[]" in out          # Deadline aufgeraeumt
    assert dlog.strip() == ""                   # keine Mutation (docker nie aufgerufen)


# 2) connection failure then readiness
def test_connection_failure_then_ready_succeeds(tmp_path):
    rc, out, _, _ = _run(tmp_path, READY_EARLY="refused", READY_OK_FROM="3", attempts="6")
    assert rc == 0


def test_503_not_ready_then_ready_succeeds(tmp_path):
    rc, _, _, _ = _run(tmp_path, READY_EARLY="notready", READY_OK_FROM="3", attempts="6")
    assert rc == 0


# 3) 0/0 targets then loaded
def test_zero_targets_then_loaded_succeeds(tmp_path):
    rc, _, _, _ = _run(tmp_path, TARG_EARLY="empty", TARG_OK_FROM="3", attempts="6")
    assert rc == 0


# 4) consumer target down then up
def test_consumer_down_then_up_succeeds(tmp_path):
    rc, _, _, _ = _run(tmp_path, TARG_EARLY="down", TARG_OK_FROM="3", attempts="6")
    assert rc == 0


# 5) missing consumer rule then loaded
def test_missing_consumer_rule_then_loaded_succeeds(tmp_path):
    rc, _, _, _ = _run(tmp_path, RULE_EARLY="noc", RULE_OK_FROM="3", attempts="6")
    assert rc == 0


# 6) missing queue rule then loaded
def test_missing_queue_rule_then_loaded_succeeds(tmp_path):
    rc, _, _, _ = _run(tmp_path, RULE_EARLY="noq", RULE_OK_FROM="3", attempts="6")
    assert rc == 0


# 7) publisher target => immediate fail closed
def test_publisher_target_immediate_fail_closed(tmp_path):
    rc, out, clog, _ = _run(tmp_path, TARG_EARLY="pub", TARG_FINAL="pub",
                            TARG_OK_FROM="9999", attempts="20", budget="30")
    assert rc != 0
    assert "policy-violation" in out and "publisher-target" in out
    # sofort, nicht als Startupdelay: hoechstens ein Targets-Aufruf, danach Abbruch;
    # die Rules-API wird gar nicht erst erreicht.
    assert clog.count("api/v1/targets") == 1
    assert "api/v1/rules" not in clog


# 8) malformed / empty cannot cause success
def test_malformed_targets_never_succeeds(tmp_path):
    rc, out, _, _ = _run(tmp_path, TARG_EARLY="bad", TARG_FINAL="bad", TARG_OK_FROM="9999",
                         attempts="3", budget="5")
    assert rc != 0 and "targets-bad-json" in out


def test_empty_object_targets_never_succeeds(tmp_path):
    rc, out, _, _ = _run(tmp_path, TARG_EARLY="emptyobj", TARG_FINAL="emptyobj",
                         TARG_OK_FROM="9999", attempts="3", budget="5")
    assert rc != 0  # {} -> activeTargets fehlt -> BAD_JSON -> nie Erfolg


def test_malformed_rules_never_succeeds(tmp_path):
    rc, out, _, _ = _run(tmp_path, RULE_EARLY="bad", RULE_FINAL="bad", RULE_OK_FROM="9999",
                         attempts="3", budget="5")
    assert rc != 0 and "rules-bad-json" in out


# 9) persistent not-ready stops at the bound
def test_persistent_not_ready_stops_at_bound(tmp_path):
    rc, out, _, _ = _run(tmp_path, READY_OK_FROM="9999", attempts="3", interval="0", budget="5")
    assert rc != 0
    assert "not-ready" in out and "DEADLINE_AFTER=[]" in out


def test_persistent_not_ready_respects_time_budget(tmp_path):
    import time
    t0 = time.monotonic()
    rc, _, _, _ = _run(tmp_path, READY_OK_FROM="9999", attempts="1000", interval="1", budget="2")
    elapsed = time.monotonic() - t0
    # Budget(2s) + kill_after(1s) + Overhead: harte Obergrenze 6s, kein endloser Retry.
    assert rc != 0 and elapsed < 6, f"Budget nicht eingehalten: {elapsed:.1f}s"


# 11) no mutation inside the retry loop (waiter level)
def test_no_mutation_in_waiter(tmp_path):
    rc, _, clog, dlog = _run(tmp_path, TARG_EARLY="empty", TARG_OK_FROM="3", attempts="6")
    assert rc == 0
    assert dlog.strip() == ""                       # kein docker/compose
    for bad in ("force-recreate", "up -d", "restart", "set image", "rollout"):
        assert bad not in clog


# 16) logs contain no raw API JSON
def test_logs_have_no_raw_api_json(tmp_path):
    rc, out, _, _ = _run(tmp_path, TARG_EARLY="empty", TARG_OK_FROM="9999",
                         attempts="2", budget="5")
    assert rc != 0
    # Diagnose nennt nur Reason-Tokens, niemals Roh-JSON-Strukturen.
    for leak in ("activeTargets", "\"groups\"", "\"labels\"", "\"health\"", "\"data\""):
        assert leak not in out


# diagnostics shape
def test_diagnostics_contain_required_fields(tmp_path):
    rc, out, _, _ = _run(tmp_path, TARG_EARLY="empty", TARG_OK_FROM="9999",
                         attempts="2", budget="5")
    assert rc != 0
    for token in ("reason=", "versuch=", "budget=", "elapsed=", "remaining=", "abbruch="):
        assert token in out, token


# --- Blocker 3: status == "success" wird verlangt ---------------------------

# Pflicht-Test 3: Targets HTTP 200, status:error, sonst gueltige Consumer-Daten -> nie Erfolg.
def test_targets_status_error_never_succeeds(tmp_path):
    rc, out, _, _ = _run(tmp_path, TARG_EARLY="errstatus", TARG_FINAL="errstatus",
                         TARG_OK_FROM="9999", attempts="3", budget="5")
    assert rc != 0 and "targets-bad-status" in out


# Pflicht-Test 4: Rules HTTP 200, status:error, sonst gueltige Rule-Gruppen -> nie Erfolg.
def test_rules_status_error_never_succeeds(tmp_path):
    rc, out, _, _ = _run(tmp_path, RULE_EARLY="errstatus", RULE_FINAL="errstatus",
                         RULE_OK_FROM="9999", attempts="3", budget="5")
    assert rc != 0 and "rules-bad-status" in out


# --- Blocker 2: nur HTTP 200 wird geparst -----------------------------------

# Pflicht-Test 5: Targets HTTP 500 mit success-foermigem Body -> nie Erfolg.
def test_targets_http500_success_body_never_succeeds(tmp_path):
    rc, out, clog, _ = _run(tmp_path, TARG_FINAL="ok", TARG_HTTP="500",
                            attempts="3", budget="5")
    assert rc != 0 and "targets-http-500" in out
    assert "api/v1/rules" not in clog   # bei nicht-200 Targets wird Rules nie erreicht


# Pflicht-Test 6: Rules HTTP 500 mit success-foermigem Body -> nie Erfolg.
def test_rules_http500_success_body_never_succeeds(tmp_path):
    rc, out, _, _ = _run(tmp_path, RULE_FINAL="ok", RULE_HTTP="500",
                         attempts="3", budget="5")
    assert rc != 0 and "rules-http-500" in out


def test_targets_non2xx_is_transient_retryable(tmp_path):
    # 503 ist transient (retryfaehig), wird aber nie erfolgreich -> mehrere Versuche.
    rc, _, clog, _ = _run(tmp_path, TARG_HTTP="503", attempts="3", interval="0", budget="5")
    assert rc != 0
    assert clog.count("api/v1/targets") >= 2


# --- Blocker 4: Parser-Exit-Codes nicht verschlucken ------------------------

# Pflicht-Test 7: fehlender/nicht ausfuehrbarer Parser -> sofort fatal, kein bad-json-Retry.
def test_parser_tool_error_is_immediately_fatal(tmp_path):
    stub = "#!/usr/bin/env bash\nexit 127\n"   # python3 'nicht ausfuehrbar/gefunden'
    rc, out, clog, _ = _run(tmp_path, python_stub=stub, attempts="9", interval="0", budget="10")
    assert rc != 0
    assert "fatal-tool" in out and "parser-error" in out
    assert "bad-json" not in out
    # sofort: genau EIN Targets-Aufruf, kein Retry bis zum Attempts-Limit, Rules nie erreicht.
    assert clog.count("api/v1/targets") == 1
    assert "api/v1/rules" not in clog


# Pflicht-Test 8: Parser-Timeout behaelt Exit-Code, kontrollierter Abbruch, kein malformed.
def test_parser_timeout_classified_as_timeout_not_badjson(tmp_path):
    import time
    stub = '#!/usr/bin/env bash\ntrap "" TERM\nsleep 30\n'   # ignoriert TERM -> kill-after
    t0 = time.monotonic()
    rc, out, _, _ = _run(tmp_path, python_stub=stub, attempts="100", interval="0",
                         budget="2", D3B2_VERIFY_KILL_AFTER="1")
    elapsed = time.monotonic() - t0
    assert rc != 0
    assert "parser-timeout" in out
    assert "bad-json" not in out
    assert elapsed < 8, f"Parser-Timeout nicht hart begrenzt: {elapsed:.1f}s"
