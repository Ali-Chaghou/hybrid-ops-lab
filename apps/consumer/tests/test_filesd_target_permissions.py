"""Prometheus file_sd-Targets muessen world-readable (0644) sein.

Prometheus laeuft als anderer Container-User und liest die bind-gemounteten Targets;
`mktemp` (0600) + `mv` wuerde 'permission denied' verursachen. Diese Tests fahren die
ECHTEN Skripte (deploy-consumer.sh, render-publisher-target.sh) mit restriktivem umask
und Fake-Kommandos und pruefen Modus 0644, valides JSON, atomares Verhalten,
fail-closed bei chmod-Fehler und das Ausbleiben von Secrets.
"""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import stat
import subprocess

import pytest

REPO = pathlib.Path(__file__).resolve().parents[3]
DEPLOY = REPO / "ops/deploy/deploy-consumer.sh"
RENDER = REPO / "ops/deploy/render-publisher-target.sh"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="bash benoetigt")

_APP_SENTINEL = "SENTINEL_APP_PW_zzz"

_FAKE_DOCKER = r"""#!/usr/bin/env bash
case "$1" in
  network) echo "172.30.0.1"; exit 0 ;;
  inspect) echo "running"; exit 0 ;;
  port)    echo "0.0.0.0:30090"; exit 0 ;;
esac
exit 0
"""
_FAKE_K3D = "#!/usr/bin/env bash\nexit 0\n"
_FAKE_KUBECTL = r"""#!/usr/bin/env bash
case "$*" in *--dry-run=client*) printf 'apiVersion: v1\nkind: Secret\n' ;; esac
case "$*" in *"apply -f -"*) cat >/dev/null 2>&1 || true ;; esac
exit 0
"""
# chmod-Fake: schlaegt NUR fuer die Target-Tempdatei fehl, delegiert sonst an /bin/chmod
# (damit z. B. das 0600-Secret weiterhin gesetzt werden kann).
_FAKE_CHMOD_FAIL_TARGET = r"""#!/usr/bin/env bash
for a in "$@"; do last="$a"; done
case "$last" in *.consumer.json.*|*.publisher.json.*) echo "fake chmod: refuse" >&2; exit 1 ;; esac
exec /bin/chmod "$@"
"""


def _w(p, body):
    p.write_text(body, encoding="utf-8"); p.chmod(0o755)


def _mode(p: pathlib.Path) -> int:
    return stat.S_IMODE(p.stat().st_mode)


def _bin(tmp_path, with_chmod_fail=False):
    fb = tmp_path / "bin"; fb.mkdir()
    _w(fb / "docker", _FAKE_DOCKER); _w(fb / "k3d", _FAKE_K3D); _w(fb / "kubectl", _FAKE_KUBECTL)
    if with_chmod_fail:
        _w(fb / "chmod", _FAKE_CHMOD_FAIL_TARGET)
    env = dict(os.environ); env["PATH"] = f"{fb}:{env['PATH']}"
    return env


def _run_umask(argv, env, umask="077"):
    # Skript unter restriktivem umask ausfuehren (beweist expliziten chmod).
    quoted = " ".join("'" + a.replace("'", "'\\''") + "'" for a in argv)
    return subprocess.run(["bash", "-c", f"umask {umask}; exec {quoted}"],
                          env=env, capture_output=True, text=True, timeout=60)


# --- render-publisher-target.sh --------------------------------------------

def _render(tmp_path, env, target):
    env = dict(env)
    env["PUBLISHER_METRICS_HOST"] = "metrics-host.lab"
    env["PUBLISHER_HOST_PORT"] = "8001"
    env["TARGET_FILE"] = str(target)
    return env


def test_publisher_target_mode_0644_under_restrictive_umask(tmp_path):
    target = tmp_path / "publisher.json"
    env = _render(tmp_path, _bin(tmp_path), target)
    r = _run_umask([str(RENDER)], env, umask="077")
    assert r.returncode == 0, r.stderr
    assert _mode(target) == 0o644
    data = json.loads(target.read_text())
    assert data[0]["targets"] == ["metrics-host.lab:8001"]
    assert not list(tmp_path.glob(".publisher.json.*"))  # kein Tempdatei-Rest


def test_publisher_target_fail_closed_on_chmod_error(tmp_path):
    target = tmp_path / "publisher.json"
    env = _render(tmp_path, _bin(tmp_path, with_chmod_fail=True), target)
    r = _run_umask([str(RENDER)], env, umask="077")
    assert r.returncode != 0
    assert not target.exists()                      # kein Target bei Fehler
    assert not list(tmp_path.glob(".publisher.json.*"))  # Cleanup-Trap griff


# --- deploy-consumer.sh -----------------------------------------------------

def _deploy_env(tmp_path, with_chmod_fail=False):
    env = _bin(tmp_path, with_chmod_fail=with_chmod_fail)
    envf = tmp_path / "site-cloud.env"
    envf.write_text(f"CONSUMER_DB=consumer\nCONSUMER_APP_PASSWORD={_APP_SENTINEL}\nCONSUMER_DB_HOST_PORT=5433\n",
                    encoding="utf-8")
    target_dir = tmp_path / "targets"; target_dir.mkdir()
    env["ENV_FILE"] = str(envf); env["TARGET_DIR"] = str(target_dir)
    return env, target_dir


def test_consumer_target_mode_0644_under_restrictive_umask(tmp_path):
    env, target_dir = _deploy_env(tmp_path)
    r = _run_umask([str(DEPLOY)], env, umask="077")
    assert r.returncode == 0, r.stderr
    cj = target_dir / "consumer.json"
    assert _mode(cj) == 0o644
    data = json.loads(cj.read_text())
    assert data[0]["targets"] == ["host.docker.internal:30090"]
    assert data[0]["labels"]["app"] == "inventory-consumer"
    assert not list(target_dir.glob(".consumer.json.*"))     # kein Tempdatei-Rest
    # Kein Secret im Target oder in der Ausgabe.
    assert _APP_SENTINEL not in cj.read_text()
    assert _APP_SENTINEL not in (r.stdout + r.stderr)


def test_consumer_target_fail_closed_on_chmod_error(tmp_path):
    env, target_dir = _deploy_env(tmp_path, with_chmod_fail=True)
    r = _run_umask([str(DEPLOY)], env, umask="077")
    assert r.returncode != 0
    assert not (target_dir / "consumer.json").exists()       # kein Target bei Fehler
    assert not list(target_dir.glob(".consumer.json.*"))     # Tempdatei entfernt
