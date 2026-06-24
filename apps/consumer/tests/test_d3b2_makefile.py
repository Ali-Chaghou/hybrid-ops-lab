"""Gate D3B2.1: Makefile-Abhaengigkeitsgraph (echte rsync/ssh-Operationszeilen).

Prueft den expandierten Graphen via `make -n` (nicht nur Text in einem Block):
cloud-up haengt ausschliesslich an sync-cloud und beruehrt site-dc weder direkt noch
ueber Abhaengigkeiten; Sync-Ziele sind sauber getrennt; kein Publisher-Target/-Enable.
"""
from __future__ import annotations

import pathlib
import shutil
import subprocess

import pytest

REPO = pathlib.Path(__file__).resolve().parents[3]

pytestmark = pytest.mark.skipif(shutil.which("make") is None, reason="make benoetigt")

DC = "DCSENTINEL"
CLOUD = "CLOUDSENTINEL"


def _dry(target: str) -> list[str]:
    out = subprocess.run(["make", "-n", target, f"DC_HOST={DC}", f"CLOUD_HOST={CLOUD}"],
                         cwd=str(REPO), capture_output=True, text=True)
    # Backslash-Zeilenfortsetzungen zu logischen Zeilen zusammenfassen.
    logical, buf = [], ""
    for ln in out.stdout.splitlines():
        if ln.rstrip().endswith("\\"):
            buf += ln.rstrip()[:-1] + " "
        else:
            logical.append(buf + ln); buf = ""
    if buf:
        logical.append(buf)
    return logical


def _ops(lines):
    """Nur echte Remote-Operationen (rsync/ssh), nicht die check-env-Guard-Echos."""
    return [ln.strip() for ln in lines if ln.strip().startswith(("rsync ", "ssh "))]


def test_cloud_up_only_touches_cloud():
    ops = _ops(_dry("cloud-up"))
    assert ops, "cloud-up hat keine Remote-Operationen"
    assert all(DC not in op for op in ops), ops      # kein site-dc
    assert any(CLOUD in op for op in ops)            # site-cloud


def test_cloud_up_invokes_controller_not_legacy_compose():
    ops = _ops(_dry("cloud-up"))
    assert any("upgrade-consumer-runtime.sh run" in op for op in ops)
    # Nicht mehr der alte ungated cloud-up-Pfad.
    assert not any("deploy-consumer.sh" in op and "upgrade-consumer-runtime" not in op for op in ops)


def test_cloud_up_no_publisher_target_step():
    lines = _dry("cloud-up")
    assert not any("render-publisher-target" in ln or "install-publisher-target" in ln for ln in lines)


def test_sync_dc_only_dc():
    ops = _ops(_dry("sync-dc"))
    assert ops and all(CLOUD not in op for op in ops)
    assert any(DC in op for op in ops)


def test_sync_cloud_only_cloud():
    ops = _ops(_dry("sync-cloud"))
    assert ops and all(DC not in op for op in ops)
    assert any(CLOUD in op for op in ops)


def test_sync_combines_both():
    ops = _ops(_dry("sync"))
    assert any(DC in op for op in ops)
    assert any(CLOUD in op for op in ops)


def test_phase3_upgrade_uses_sync_dc_not_full_sync():
    # phase3-upgrade synct site-dc (sync-dc); der Publisher-Target-Schritt geht NUR
    # gezielt nach cloud (install-publisher-target), kein genereller cloud-Code-Sync.
    ops = _ops(_dry("phase3-upgrade"))
    # Genau ein genereller Code-rsync, und der geht nach DC.
    code_syncs = [op for op in ops if op.startswith("rsync ") and "publisher.json" not in op.split()[-1]]
    assert code_syncs, ops
    assert all(DC in op for op in code_syncs)


def test_no_target_enables_publisher_or_events():
    mk = (REPO / "Makefile").read_text(encoding="utf-8")
    assert "PUBLISHER_ENABLED=true" not in mk
    assert "EVENTS_ENABLED=true" not in mk


def test_check_env_enforces_make_env_perms():
    mk = (REPO / "Makefile").read_text(encoding="utf-8")
    assert "check-local-perms.sh make.env" in mk


def test_cloud_targets_exist():
    mk = (REPO / "Makefile").read_text(encoding="utf-8")
    for t in ("sync-cloud:", "sync-dc:", "cloud-up:", "cloud-resume:", "cloud-state:", "cloud-check:"):
        assert t in mk, t


def test_cloud_up_passes_release_sha_and_ack_over_ssh():
    ops = _ops(_dry("cloud-up"))
    ssh = [op for op in ops if op.startswith("ssh ")]
    assert ssh and all("D3B2_RELEASE_SHA=" in op and "D3B2_ACK_CONSUMER_RESTARTS=" in op for op in ssh)


def test_cloud_resume_passes_release_sha_and_ack():
    ssh = [op for op in _ops(_dry("cloud-resume")) if op.startswith("ssh ")]
    assert ssh and all("D3B2_RELEASE_SHA=" in op and "D3B2_ACK_CONSUMER_RESTARTS=" in op for op in ssh)


def test_ack_default_is_zero_and_validated():
    mk = (REPO / "Makefile").read_text(encoding="utf-8")
    assert "D3B2_ACK_CONSUMER_RESTARTS ?= 0" in mk
    assert "_validate-ack" in mk
    # cloud-up/cloud-resume haengen am Ack-Validator (nach dem Release-Guard + sync).
    assert "cloud-up: check-d3b2-release sync-cloud _validate-ack" in mk
    assert "cloud-resume: check-d3b2-release sync-cloud _validate-ack" in mk


def test_release_sha_derived_from_local_git():
    mk = (REPO / "Makefile").read_text(encoding="utf-8")
    assert "D3B2_RELEASE_SHA := $(shell git" in mk


def _all_lines(target):
    """Alle Rezeptzeilen (inkl. Guard/Script-Aufrufe), Reihenfolge erhalten."""
    return [ln.strip() for ln in _dry(target)]


def _first_index(lines, predicate):
    for i, ln in enumerate(lines):
        if predicate(ln):
            return i
    return 10**9


def test_release_guard_is_first_prereq_of_cloud_targets():
    mk = (REPO / "Makefile").read_text(encoding="utf-8")
    assert "cloud-up: check-d3b2-release sync-cloud" in mk
    assert "cloud-resume: check-d3b2-release sync-cloud" in mk


@pytest.mark.parametrize("target", ["cloud-up", "cloud-resume"])
def test_release_guard_runs_before_any_sync_or_ssh(target):
    lines = _all_lines(target)
    i_guard = _first_index(lines, lambda l: "check-d3b2-local-release.sh" in l)
    i_remote = _first_index(lines, lambda l: l.startswith(("rsync ", "ssh ")))
    assert i_guard < i_remote, (i_guard, i_remote)


def test_check_d3b2_release_target_exists_and_validates_sha():
    mk = (REPO / "Makefile").read_text(encoding="utf-8")
    assert "check-d3b2-release:" in mk
    assert "check-d3b2-local-release.sh" in mk
    assert "[0-9a-f]{40}" in mk
