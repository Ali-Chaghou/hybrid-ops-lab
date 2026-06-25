"""NodePort-Publikations-Check gegen den k3d-Server-Loadbalancer (serverlb).

k3d veroeffentlicht den Host-Port nicht auf dem Server-Node, sondern auf dem
generierten Server-Loadbalancer. Diese Tests fahren die ECHTEN Skripte mit
Fake-`docker`/`k3d`/`kubectl` und weisen nach, dass der Publikations-Check den
Loadbalancer prueft (fail closed bei fehlendem Port / ungueltigem Namen) und
CRI/containerd-Operationen weiterhin den Server-Node nutzen.
"""
from __future__ import annotations

import os
import pathlib
import shutil
import subprocess

import pytest

REPO = pathlib.Path(__file__).resolve().parents[3]
CREATE = REPO / "ops/bootstrap/create-site-cloud-cluster.sh"
DEPLOY = REPO / "ops/deploy/deploy-consumer.sh"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="bash benoetigt")

_FAKE_DOCKER = r"""#!/usr/bin/env bash
echo "docker $*" >> "$DLOG"
case "$1" in
  network) echo "172.30.0.1"; exit 0 ;;
  inspect) [ "${FAKE_OWNER_PRESENT:-1}" = "1" ] && { echo "running"; exit 0; } || exit 1 ;;
  port)    [ "${FAKE_PORT_PRESENT:-1}" = "1" ] && { echo "0.0.0.0:30090"; exit 0; } || exit 1 ;;
  build)   exit 0 ;;
esac
exit 0
"""

_FAKE_K3D = r"""#!/usr/bin/env bash
echo "k3d $*" >> "$DLOG"
case "$*" in
  "cluster list "*) [ "${FAKE_CLUSTER_EXISTS:-1}" = "1" ] && exit 0 || exit 1 ;;
esac
exit 0
"""

_FAKE_KUBECTL = r"""#!/usr/bin/env bash
echo "kubectl $*" >> "$DLOG"
case "$*" in *--dry-run=client*) printf 'apiVersion: v1\nkind: Secret\n' ;; esac
case "$*" in *"apply -f -"*) cat >/dev/null 2>&1 || true ;; esac
exit 0
"""


def _bin(tmp_path):
    fb = tmp_path / "bin"; fb.mkdir()
    for name, body in (("docker", _FAKE_DOCKER), ("k3d", _FAKE_K3D), ("kubectl", _FAKE_KUBECTL)):
        p = fb / name; p.write_text(body, encoding="utf-8"); p.chmod(0o755)
    dlog = tmp_path / "d.log"; dlog.write_text("", encoding="utf-8")
    env = dict(os.environ); env["PATH"] = f"{fb}:{env['PATH']}"; env["DLOG"] = str(dlog)
    return env, dlog


def _run(script, env, *, cwd=None):
    return subprocess.run(["bash", str(script)], env=env, capture_output=True, text=True,
                          cwd=cwd, timeout=60)


# --- create-site-cloud-cluster.sh ------------------------------------------

def test_create_existing_cluster_port_present_on_serverlb(tmp_path):
    env, dlog = _bin(tmp_path)
    r = _run(CREATE, env)
    assert r.returncode == 0, r.stderr
    assert "serverlb" in r.stdout and "via k3d-site-cloud-serverlb" in r.stdout
    # docker port gegen den Loadbalancer, NICHT den Server-Node.
    log = dlog.read_text()
    assert "docker port k3d-site-cloud-serverlb 30090/tcp" in log
    assert "docker port k3d-site-cloud-server-0" not in log


def test_create_port_missing_on_serverlb_fails_closed(tmp_path):
    env, _ = _bin(tmp_path); env["FAKE_PORT_PRESENT"] = "0"
    r = _run(CREATE, env)
    assert r.returncode != 0
    assert "serverlb" in r.stderr and "NICHT veroeffentlicht" in r.stderr


def test_create_port_owner_missing_fails_closed(tmp_path):
    env, _ = _bin(tmp_path); env["FAKE_OWNER_PRESENT"] = "0"
    r = _run(CREATE, env)
    assert r.returncode != 0
    assert "nicht gefunden" in r.stderr.lower() or "Port-Owner" in r.stderr


def test_create_invalid_port_owner_rejected(tmp_path):
    env, dlog = _bin(tmp_path); env["K3D_PORT_OWNER"] = "bad name!"
    r = _run(CREATE, env)
    assert r.returncode != 0
    assert "ungueltiger Port-Owner" in r.stderr
    assert "docker port" not in dlog.read_text()   # vor jedem docker-Aufruf abgelehnt


# --- deploy-consumer.sh -----------------------------------------------------

def _deploy_env(tmp_path):
    env, dlog = _bin(tmp_path)
    envf = tmp_path / "site-cloud.env"
    envf.write_text("CONSUMER_DB=consumer\nCONSUMER_APP_PASSWORD=pw\nCONSUMER_DB_HOST_PORT=5433\n", encoding="utf-8")
    target = tmp_path / "targets"; target.mkdir()
    env["ENV_FILE"] = str(envf); env["TARGET_DIR"] = str(target)
    return env, dlog


def test_deploy_uses_serverlb_for_port_check(tmp_path):
    env, dlog = _deploy_env(tmp_path)
    r = _run(DEPLOY, env)
    assert r.returncode == 0, r.stderr
    log = dlog.read_text()
    assert "docker port k3d-site-cloud-serverlb 30090/tcp" in log
    assert "docker port k3d-site-cloud-server-0" not in log
    assert "via k3d-site-cloud-serverlb" in r.stdout


def test_deploy_port_missing_on_serverlb_fails_closed(tmp_path):
    env, _ = _deploy_env(tmp_path); env["FAKE_PORT_PRESENT"] = "0"
    r = _run(DEPLOY, env)
    assert r.returncode != 0
    assert "serverlb" in r.stderr and "FEHLER: NodePort" in r.stderr


def test_deploy_invalid_port_owner_rejected(tmp_path):
    env, dlog = _deploy_env(tmp_path); env["K3D_PORT_OWNER"] = "bad;name"
    r = _run(DEPLOY, env)
    assert r.returncode != 0
    assert "ungueltiger Port-Owner" in r.stderr
    assert "docker port" not in dlog.read_text()
