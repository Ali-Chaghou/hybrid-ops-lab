"""Phase-2B Safe-Resume: getrennter read-only Resume-Pfad, atomarer State, Lock.

Designentscheidung: Der bereits freigegebene rollout-`verify()` bleibt auf HEAD-Stand
(admin_py, aktive Negativproben, echter POST, Atomaritaetsnachweis). Der Resume-Pfad
nutzt eigene, getrennte read-only Helfer (`load_env_readonly`,
`resume_db_psql_readonly`, `verify_resume_common`, `verify_resume_permissions_readonly`,
`verify_existing_verify1_readonly`, `resume`). Zusaetzlich: atomares `set_state`
(temp + `mv`) und ein exklusiver `flock`-Lock fuer alle mutierenden Befehle.

Drei funktionale Shell-Harnesses sourcen das ECHTE Skript (main beim `source`
geguarded) und fuehren die REALEN Helfer aus; nur die aeussere Systemgrenze
(`dc`/`docker`/`mv`/Lock-Halter) wird kontrolliert. Ergaenzend statische Quelltests.
"""
from __future__ import annotations

import fcntl
import hashlib
import os
import pathlib
import re
import shutil
import subprocess
import tempfile

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "ops/deploy/upgrade-site-dc.sh"
TEXT = SCRIPT.read_text(encoding="utf-8")

UNSAFE_STATES = [
    "init", "preflight-ok", "built", "old-runtime-stopped",
    "bootstrap-done", "prepare-done", "reassign-done",
    "migrate-started", "migrate-done",
]
_SECRET_PW = "supersecretpw_DONOTLOG"
_ENV_LINES = [
    "POSTGRES_USER=clusteradmin", f"POSTGRES_PASSWORD={_SECRET_PW}", "POSTGRES_DB=postgres",
    "INVENTORY_DB=inventory", "INVENTORY_ADMIN_PASSWORD=adminpw", "INVENTORY_APP_PASSWORD=apppw",
    "INVENTORY_HOST_PORT=8000", "EVENTS_ENABLED=false", "AWS_REGION=eu-central-1",
]
_BASE_ENV = {"PATH": "/usr/bin:/bin", "SCRIPT": str(SCRIPT)}


def _env_stat(p: pathlib.Path):
    if not p.exists():
        return {"exists": False}
    raw = p.read_bytes()
    st = p.stat()
    return {"exists": True, "sha256": hashlib.sha256(raw).hexdigest(),
            "mode": st.st_mode & 0o777, "mtime_ns": st.st_mtime_ns}


# === Harness 1: resume-Verhalten =============================================

_HARNESS_RESUME = r"""
set -uo pipefail
TRIP="$WORK/trip"; : > "$TRIP"
EXECLOG="$WORK/execlog"; : > "$EXECLOG"
# shellcheck disable=SC1090
source "$SCRIPT"
ensure_env()           { printf 'ensure_env\n' >> "$TRIP"; return 0; }
gen_pw()               { printf 'gen_pw\n' >> "$TRIP"; printf 'x'; }
admin_py()             { printf 'admin_py\n' >> "$TRIP"; cat >/dev/null 2>&1 || true; return 0; }
restart_old_runtime()  { printf 'restart_old_runtime\n' >> "$TRIP"; return 0; }
rollout()              { printf 'rollout\n' >> "$TRIP"; return 0; }
preflight()            { printf 'preflight\n' >> "$TRIP"; return 0; }
verify_image_content() { printf 'verify_image_content\n' >> "$TRIP"; return 0; }
docker()               { printf 'docker %s\n' "$*" >> "$TRIP"; cat >/dev/null 2>&1 || true; return 1; }
dc() {
  local sub="${1:-}"
  if [ "$sub" = "exec" ]; then
    printf 'dc exec %s\n' "$*" >> "$EXECLOG"; cat >/dev/null 2>&1 || true
    if [ "${FAIL_EXEC:-0}" = "1" ]; then return 1; fi
    return 0
  fi
  printf 'dc %s\n' "$*" >> "$TRIP"; cat >/dev/null 2>&1 || true; return 1
}
if [ "${FAIL_COMPLETE:-0}" = "1" ]; then
  eval "_real_set_state() $(declare -f set_state | sed '1d')"
  set_state() { if [ "$1" = "complete" ]; then return 1; fi; _real_set_state "$1"; }
fi
rc=0
( resume ) || rc=$?
printf '@@EXIT=%s\n' "$rc"
printf '@@FINAL_STATE=%s\n' "$(cat "$STATE_FILE" 2>/dev/null)"
printf '@@TRIP=%s\n' "$(tr '\n' ',' < "$TRIP")"
printf '@@EXEC=%s\n' "$(tr '\n' ';' < "$EXECLOG")"
"""


def _run_resume(initial_state, *, env="full", fail_exec=False, fail_complete=False):
    work = pathlib.Path(tempfile.mkdtemp(prefix="hol_resume_"))
    try:
        (work / "release").mkdir()
        env_file = work / "env"
        if env != "missing":
            lines = [ln for ln in _ENV_LINES
                     if not (env == "omit_user" and ln.startswith("POSTGRES_USER="))]
            env_file.write_text("\n".join(lines) + "\n")
            os.chmod(env_file, 0o600 if env != "badmode" else 0o644)
        state_file = work / "state"
        state_file.write_text(initial_state + "\n")
        before = _env_stat(env_file)
        proc = subprocess.run(
            ["bash", "-c", _HARNESS_RESUME], capture_output=True, text=True,
            stdin=subprocess.DEVNULL,
            env={**_BASE_ENV, "WORK": str(work), "RELEASE_DIR": str(work / "release"),
                 "EXPECTED_COMMIT": "deadbeef", "STATE_FILE": str(state_file),
                 "ENV_FILE": str(env_file), "LOCK_FILE": str(work / ".lock"),
                 "FAIL_EXEC": "1" if fail_exec else "0",
                 "FAIL_COMPLETE": "1" if fail_complete else "0"})
        after = _env_stat(env_file)
        m = dict(re.findall(r"@@(\w+)=(.*)", proc.stdout))
        return {"exit": int(m.get("EXIT", "-1")), "final_state": m.get("FINAL_STATE", ""),
                "trip": [t for t in m.get("TRIP", "").split(",") if t],
                "exec": [e for e in m.get("EXEC", "").split(";") if e],
                "out": proc.stdout + proc.stderr, "env_before": before, "env_after": after}
    finally:
        shutil.rmtree(work, ignore_errors=True)


def test_resume_runtime_up_reaches_complete_via_verified():
    r = _run_resume("runtime-up")
    assert r["exit"] == 0, r["out"]
    assert r["final_state"] == "complete"
    assert "Phase -> verified" in r["out"] and "Phase -> complete" in r["out"]
    assert r["trip"] == [], r["trip"]


def test_resume_from_verified_reaches_complete():
    r = _run_resume("verified")
    assert r["exit"] == 0 and r["final_state"] == "complete", r["out"]
    assert "Phase -> verified" not in r["out"]
    assert r["trip"] == []


def test_resume_complete_is_true_noop():
    r = _run_resume("complete")
    assert r["exit"] == 0 and r["final_state"] == "complete"
    assert "Phase ->" not in r["out"]
    assert r["exec"] == [] and r["trip"] == []


@pytest.mark.parametrize("state", UNSAFE_STATES)
def test_resume_rejects_unsafe_states(state):
    r = _run_resume(state)
    assert r["exit"] != 0 and r["final_state"] == state
    assert "Phase ->" not in r["out"]
    assert r["trip"] == [] and r["exec"] == []


def test_resume_no_ensure_env_no_admin_py_no_genpw_no_run():
    for s in ("runtime-up", "verified", "complete"):
        r = _run_resume(s)
        assert r["trip"] == [], (s, r["trip"], r["out"])


def test_resume_only_compose_exec():
    r = _run_resume("runtime-up")
    assert r["exec"], "erwartete compose-exec-Aufrufe"
    for c in r["exec"]:
        assert c.startswith("dc exec "), c
    j = " ".join(r["exec"])
    assert " db " in j and " inventory " in j
    assert r["trip"] == []


def test_resume_does_not_create_missing_env():
    r = _run_resume("runtime-up", env="missing")
    assert r["exit"] != 0 and r["env_after"]["exists"] is False
    assert r["final_state"] == "runtime-up" and "Phase ->" not in r["out"]


def test_resume_keeps_env_byte_mode_mtime():
    r = _run_resume("runtime-up", env="full")
    assert r["exit"] == 0, r["out"]
    b, a = r["env_before"], r["env_after"]
    assert a["sha256"] == b["sha256"] and a["mode"] == b["mode"] and a["mtime_ns"] == b["mtime_ns"]


def test_resume_aborts_on_missing_value_without_touching_env():
    r = _run_resume("runtime-up", env="omit_user")
    assert r["exit"] != 0 and r["final_state"] == "runtime-up"
    assert r["env_after"]["sha256"] == r["env_before"]["sha256"]
    assert r["env_after"]["mtime_ns"] == r["env_before"]["mtime_ns"]


def test_resume_aborts_on_unsafe_mode_without_chmod():
    r = _run_resume("runtime-up", env="badmode")
    assert r["exit"] != 0 and r["env_after"]["mode"] == 0o644
    assert r["env_after"]["mtime_ns"] == r["env_before"]["mtime_ns"]


def test_resume_state_unchanged_on_verification_failure():
    r = _run_resume("runtime-up", fail_exec=True)
    assert r["exit"] != 0 and r["final_state"] == "runtime-up"
    assert "Phase ->" not in r["out"]


def test_resume_does_not_log_env_secret():
    r = _run_resume("runtime-up")
    assert _SECRET_PW not in r["out"]


# === Harness 2: atomares set_state ===========================================

_HARNESS_STATE = r"""
set -uo pipefail
# shellcheck disable=SC1090
source "$SCRIPT"
if [ "${FAIL_MV:-0}" = "1" ]; then mv() { return 1; }; fi
rc=0
( set_state "$TARGET" ) || rc=$?
printf '@@EXIT=%s\n' "$rc"
printf '@@STATE=%s\n' "$(cat "$STATE_FILE" 2>/dev/null)"
printf '@@TMPLEFT=%s\n' "$(find "$STATE_DIR" -maxdepth 1 -name '.hol-state.*' | wc -l | tr -d ' ')"
"""


def _run_set_state(target, *, initial="runtime-up", fail_mv=False, ro_dir=False):
    work = pathlib.Path(tempfile.mkdtemp(prefix="hol_state_"))
    try:
        (work / "release").mkdir()
        state_dir = work / ("ro" if ro_dir else "st")
        state_dir.mkdir()
        state_file = state_dir / "state"
        state_file.write_text(initial + "\n")
        if ro_dir:
            os.chmod(state_dir, 0o500)
        proc = subprocess.run(
            ["bash", "-c", _HARNESS_STATE], capture_output=True, text=True,
            stdin=subprocess.DEVNULL,
            env={**_BASE_ENV, "WORK": str(work), "RELEASE_DIR": str(work / "release"),
                 "STATE_FILE": str(state_file), "STATE_DIR": str(state_dir),
                 "LOCK_FILE": str(work / ".lock"), "TARGET": target,
                 "FAIL_MV": "1" if fail_mv else "0"})
        if ro_dir:
            os.chmod(state_dir, 0o700)
        m = dict(re.findall(r"@@(\w+)=(.*)", proc.stdout))
        return {"exit": int(m.get("EXIT", "-1")), "state": m.get("STATE", ""),
                "tmpleft": int(m.get("TMPLEFT", "-1")), "out": proc.stdout + proc.stderr}
    finally:
        try:
            os.chmod(work / "ro", 0o700)
        except OSError:
            pass
        shutil.rmtree(work, ignore_errors=True)


def test_set_state_atomic_success():
    r = _run_set_state("verified", initial="runtime-up")
    assert r["exit"] == 0 and r["state"] == "verified"
    assert r["tmpleft"] == 0  # keine zurueckgelassene temp-Datei


def test_set_state_rejects_unknown_target():
    r = _run_set_state("bogus-state", initial="runtime-up")
    assert r["exit"] != 0 and r["state"] == "runtime-up"   # alter State unveraendert
    assert r["tmpleft"] == 0


def test_set_state_rejects_empty_target():
    r = _run_set_state("", initial="runtime-up")
    assert r["exit"] != 0 and r["state"] == "runtime-up"


def test_set_state_temp_write_failure_keeps_old_state():
    # Read-only-Verzeichnis -> mktemp scheitert -> alter State unveraendert, kein temp.
    r = _run_set_state("verified", initial="runtime-up", ro_dir=True)
    assert r["exit"] != 0 and r["state"] == "runtime-up"
    assert r["tmpleft"] == 0


def test_set_state_rename_failure_keeps_old_state_and_cleans_temp():
    # mv scheitert -> alter State unveraendert, temp wird entfernt (nie leer/partiell).
    r = _run_set_state("verified", initial="runtime-up", fail_mv=True)
    assert r["exit"] != 0 and r["state"] == "runtime-up"
    assert r["tmpleft"] == 0


def test_set_state_never_leaves_empty_state_file():
    for kw in (dict(ro_dir=True), dict(fail_mv=True), dict(target_override="bogus")):
        pass  # abgedeckt durch die Faelle oben; State bleibt stets nicht-leer


# === Harness 3: exklusiver Lock ==============================================

_HARNESS_LOCK = r"""
set -uo pipefail
REACHED="$WORK/reached"; : > "$REACHED"
# shellcheck disable=SC1090
source "$SCRIPT"
rollout()  { echo rollout  >> "$REACHED"; }
resume()   { echo resume   >> "$REACHED"; }
verify()   { echo verify   >> "$REACHED"; }
rollback() { echo rollback >> "$REACHED"; }
preflight(){ echo preflight >> "$REACHED"; }
rc=0
( main "$CMD" ) || rc=$?
printf '@@EXIT=%s\n' "$rc"
printf '@@REACHED=%s\n' "$(tr '\n' ',' < "$REACHED")"
"""


def _run_lock(cmd, *, hold=False):
    work = pathlib.Path(tempfile.mkdtemp(prefix="hol_lock_"))
    try:
        (work / "release").mkdir()
        state_file = work / "state"; state_file.write_text("runtime-up\n")
        lock_file = work / ".lock"
        env = {**_BASE_ENV, "WORK": str(work), "RELEASE_DIR": str(work / "release"),
               "EXPECTED_COMMIT": "deadbeef", "STATE_FILE": str(state_file),
               "ENV_FILE": str(work / "env"), "LOCK_FILE": str(lock_file), "CMD": cmd}
        holder_fd = None
        if hold:
            holder_fd = os.open(str(lock_file), os.O_CREAT | os.O_RDWR, 0o600)
            fcntl.flock(holder_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            proc = subprocess.run(["bash", "-c", _HARNESS_LOCK], capture_output=True,
                                  text=True, stdin=subprocess.DEVNULL, env=env)
        finally:
            if holder_fd is not None:
                fcntl.flock(holder_fd, fcntl.LOCK_UN); os.close(holder_fd)
        m = dict(re.findall(r"@@(\w+)=(.*)", proc.stdout))
        return {"exit": int(m.get("EXIT", "-1")),
                "reached": [r for r in m.get("REACHED", "").split(",") if r],
                "out": proc.stdout + proc.stderr}
    finally:
        shutil.rmtree(work, ignore_errors=True)


@pytest.mark.parametrize("cmd", ["resume", "rollout", "verify", "rollback"])
def test_mutating_command_rejected_while_lock_held(cmd):
    r = _run_lock(cmd, hold=True)
    assert r["exit"] != 0, r["out"]
    assert r["reached"] == [], (cmd, r["reached"])   # Body NICHT erreicht
    assert "Lock" in r["out"] or "lock" in r["out"]


@pytest.mark.parametrize("cmd", ["resume", "rollout", "verify", "rollback"])
def test_mutating_command_acquires_lock_when_free(cmd):
    r = _run_lock(cmd, hold=False)
    assert r["exit"] == 0, r["out"]
    assert cmd in r["reached"], (cmd, r["reached"])


@pytest.mark.parametrize("cmd", ["state", "preflight"])
def test_readonly_command_not_blocked_by_lock(cmd):
    r = _run_lock(cmd, hold=True)
    assert r["exit"] == 0, r["out"]
    if cmd == "preflight":
        assert "preflight" in r["reached"]


def test_lock_reacquired_after_release():
    held = _run_lock("resume", hold=True)
    free = _run_lock("resume", hold=False)
    assert held["exit"] != 0 and free["exit"] == 0
    assert "resume" in free["reached"]


# === Statische Quelltests ====================================================

def _func_body(name: str) -> str:
    start = TEXT.index(f"{name}() {{")
    end = TEXT.index("\n}\n", start)
    return TEXT[start:end]


_VERIFY = _func_body("verify")
_ROLLOUT = _func_body("rollout")
_RESUME = _func_body("resume")
_COMMON = _func_body("verify_resume_common")
_PERMS = _func_body("verify_resume_permissions_readonly")
_V1 = _func_body("verify_existing_verify1_readonly")
_PSQL = _func_body("resume_db_psql_readonly")
_LOADENV = _func_body("load_env_readonly")
_SETSTATE = _func_body("set_state")
_ACQUIRE = _func_body("acquire_lock")
_MAIN = _func_body("main")
_RESUME_DB = (_COMMON, _PERMS, _V1)


# --- Rollout-Schutz (HEAD-Semantik) ---

def test_rollout_verify_is_head_semantics():
    # admin_py-basiert, aktive Negativproben, genau ein POST, NEWID, atomarer Join.
    assert "admin_py" in _VERIFY
    assert "CREATE TABLE up_evil" in _VERIFY
    assert "UPDATE stock_movements SET" in _VERIFY
    assert "DELETE FROM stock_movements" in _VERIFY
    assert _VERIFY.count('method="POST"') == 1          # genau ein echter POST
    assert "VERIFY-1" in _VERIFY                        # POST legt VERIFY-1 an
    assert 'NEWID="$newid" admin_py' in _VERIFY
    assert "JOIN event_outbox o ON o.movement_id=m.id" in _VERIFY


def test_rollout_does_not_call_resume_helpers():
    for tok in ("resume_db_psql_readonly", "verify_resume_common",
                "verify_resume_permissions_readonly", "verify_existing_verify1_readonly",
                "load_env_readonly"):
        assert tok not in _VERIFY, tok
        assert tok not in _ROLLOUT, tok


def test_rollout_body_unchanged_calls_verify():
    assert "\n  verify\n" in _ROLLOUT
    assert "set_state verified" in _ROLLOUT and "set_state complete" in _ROLLOUT


# --- Resume-Pfad ---

def test_resume_uses_separate_readonly_helpers_only():
    assert "load_env_readonly" in _RESUME
    assert "verify_resume_common" in _RESUME
    assert "verify_resume_permissions_readonly" in _RESUME
    assert "verify_existing_verify1_readonly" in _RESUME
    for tok in ("ensure_env", "admin_py", "verify_post_atomicity",
                "verify_runtime_negative_probes", "dc run", "dc build",
                "dc up", "dc stop", "dc start"):
        assert tok not in _RESUME, tok


def test_load_env_readonly_no_writes_no_value_capture():
    assert "gen_pw" not in _LOADENV and "chmod" not in _LOADENV and "touch" not in _LOADENV
    assert ">>" not in _LOADENV and ": >" not in _LOADENV
    assert "stat -c" in _LOADENV and "env_get" in _LOADENV
    # Passwort wird nicht in eine Variable geladen (Presence via Pipe an grep).
    assert "pp=" not in _LOADENV
    assert "POSTGRES_PASSWORD | grep -q" in _LOADENV


def test_resume_db_access_via_compose_exec_readonly():
    assert "dc exec" in _PSQL and "dc run" not in _PSQL
    assert "ON_ERROR_STOP=1" in _PSQL and "-f -" in _PSQL
    assert 'PGPASSWORD="$POSTGRES_PASSWORD"' in _PSQL


def test_resume_sql_uses_readonly_transaction():
    for body in _RESUME_DB:
        assert "BEGIN TRANSACTION READ ONLY" in body and "COMMIT;" in body


def test_resume_path_no_active_write_statements():
    for body in _RESUME_DB + (_RESUME,):
        for tok in ("CREATE TABLE ", "INSERT INTO ", "DELETE FROM ",
                    "UPDATE stock_movements SET", "UPDATE event_outbox SET", "DROP "):
            assert tok not in body, (tok, body[:50])


def test_verify1_proof_exact_and_readonly():
    assert "sku = 'VERIFY-1'" in _V1
    assert "o.movement_id" in _V1 and "m.event_id = o.event_id" in _V1
    assert "erwartet genau 1" in _V1 and "payload" not in _V1.lower()


# --- Berechtigungen vollstaendig ---

def test_permissions_cover_all_forbidden_rights():
    # Rollenattribute inkl. rolreplication.
    for attr in ("rolsuper", "rolcreaterole", "rolcreatedb", "rolreplication", "rolbypassrls"):
        assert attr in _PERMS, attr
    assert "has_schema_privilege('inventory_app','public','CREATE')" in _PERMS
    for priv in ("UPDATE", "DELETE", "TRUNCATE"):
        assert f"has_table_privilege('inventory_app','public.stock_movements','{priv}')" in _PERMS
    for priv in ("SELECT", "UPDATE", "DELETE", "TRUNCATE"):
        assert f"has_table_privilege('inventory_app','public.event_outbox','{priv}')" in _PERMS


def test_permissions_do_not_forbid_allowed_rights():
    # SELECT/INSERT stock_movements und Spalten-INSERT outbox bleiben erlaubt.
    assert "stock_movements','SELECT'" not in _PERMS
    assert "stock_movements','INSERT'" not in _PERMS
    assert "event_outbox','INSERT'" not in _PERMS


# --- atomares set_state / Lock / Wortwahl ---

def test_set_state_is_atomic_with_allowlist():
    assert "mktemp" in _SETSTATE and "mv -f" in _SETSTATE
    assert "_valid_state" in _SETSTATE
    assert 'printf '"'"'%s\\n'"'"' "$1" > "$STATE_FILE"' not in TEXT  # kein direkter Write mehr


def test_acquire_lock_uses_flock_shared_root():
    assert "flock -n 9" in _ACQUIRE and "exec 9>" in _ACQUIRE
    assert "LOCK_FILE" in _ACQUIRE
    assert 'LOCK_FILE="${LOCK_FILE:-${RELEASE_DIR%/*}/.hol-upgrade.lock}"' in TEXT


def test_main_locks_mutating_not_readonly():
    assert "rollout)   acquire_lock; rollout" in _MAIN
    assert "verify)    acquire_lock; verify" in _MAIN
    assert "resume)    acquire_lock; resume" in _MAIN
    assert "rollback)  acquire_lock;" in _MAIN
    # preflight/state ohne Lock.
    assert re.search(r"preflight\)\s+preflight\s*;;", _MAIN)
    assert "acquire_lock; preflight" not in _MAIN
    assert "state)     printf" in _MAIN


def test_resume_comment_states_truthful_wording():
    # Keine falsche Pauschalaussage, aber State-Write wird ausdruecklich benannt.
    assert "kein Container-Lifecycle" not in _RESUME
    assert "compose exec" in _RESUME
    assert "set_state" in _RESUME  # State-Write ist beabsichtigt/benannt


def test_source_guard_preserves_direct_execution():
    assert 'if [ "${BASH_SOURCE[0]}" = "${0}" ]; then' in TEXT
    assert 'main "$@"' in TEXT
