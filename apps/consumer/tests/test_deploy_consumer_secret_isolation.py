"""Secret-Isolation von ops/deploy/deploy-consumer.sh.

Faehrt das ECHTE Deploy-Skript mit Fake-`docker`/`k3d`/`kubectl` auf dem PATH und
weist nach, dass die Passwortwerte aus der .env NICHT in den Environments der
Kindprozesse landen (kein `set -a`-Export). Die Fakes schreiben ihr `env` in
Capture-Dateien; der Test prueft, dass keine Sentinel-Passwoerter darin auftauchen.
"""
from __future__ import annotations

import os
import pathlib
import shutil
import subprocess

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
DEPLOY = REPO_ROOT / "ops" / "deploy" / "deploy-consumer.sh"

_PG = "SENTINEL_PG_PW_zzz"
_ADMIN = "SENTINEL_ADMIN_PW_zzz"
_APP = "SENTINEL_APP_PW_zzz"
_SENTINELS = (_PG, _ADMIN, _APP)

_FAKE_DOCKER = """#!/usr/bin/env bash
env > "${CAPTURE_DIR}/docker.$$"
if [ "$1" = "network" ]; then echo "172.30.0.1"; fi
exit 0
"""

_FAKE_K3D = """#!/usr/bin/env bash
env > "${CAPTURE_DIR}/k3d.$$"
exit 0
"""

_FAKE_KUBECTL = """#!/usr/bin/env bash
env > "${CAPTURE_DIR}/kubectl.$$"
case "$*" in
  *--dry-run=client*) printf 'apiVersion: v1\\nkind: Secret\\n' ;;
esac
case "$*" in
  *"apply -f -"*) cat >/dev/null 2>&1 || true ;;
esac
exit 0
"""


def _write_exec(path: pathlib.Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash benoetigt")
def test_passwords_not_inherited_by_child_processes(tmp_path):
    fakedir = tmp_path / "fakebin"
    capture = tmp_path / "capture"
    fakedir.mkdir()
    capture.mkdir()
    _write_exec(fakedir / "docker", _FAKE_DOCKER)
    _write_exec(fakedir / "k3d", _FAKE_K3D)
    _write_exec(fakedir / "kubectl", _FAKE_KUBECTL)

    env_file = tmp_path / "site-cloud.env"
    env_file.write_text(
        "AWS_REGION=eu-central-1\n"
        "POSTGRES_USER=hol_admin\n"
        f"POSTGRES_PASSWORD={_PG}\n"
        "POSTGRES_DB=postgres\n"
        "CONSUMER_DB=consumer\n"
        "CONSUMER_DB_HOST_PORT=5433\n"
        f"CONSUMER_ADMIN_PASSWORD={_ADMIN}\n"
        f"CONSUMER_APP_PASSWORD={_APP}\n",
        encoding="utf-8",
    )

    env = dict(os.environ)
    env["PATH"] = f"{fakedir}:{env['PATH']}"
    env["CAPTURE_DIR"] = str(capture)
    env["ENV_FILE"] = str(env_file)
    env["CLUSTER"] = "site-cloud"
    env["NETWORK"] = "k3d-site-cloud"

    result = subprocess.run(
        ["bash", str(DEPLOY)],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr

    captures = list(capture.iterdir())
    assert captures, "Fake-Kindprozesse wurden nicht aufgerufen"
    # Mindestens docker, k3d und kubectl muessen aufgerufen worden sein.
    names = {p.name.split(".")[0] for p in captures}
    assert {"docker", "k3d", "kubectl"} <= names

    for cap in captures:
        text = cap.read_text(encoding="utf-8", errors="replace")
        for secret in _SENTINELS:
            assert secret not in text, f"Secret an Kindprozess {cap.name} vererbt!"
