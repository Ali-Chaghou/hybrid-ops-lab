"""Gate D3B1: ops/deploy/render-publisher-target.sh (atomar, valide, injection-fest)."""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess

import pytest

REPO = pathlib.Path(__file__).resolve().parents[3]
RENDER = REPO / "ops/deploy/render-publisher-target.sh"


def _run(tmp_path, host, port="8001"):
    target = tmp_path / "publisher.json"
    env = dict(os.environ)
    env["PUBLISHER_METRICS_HOST"] = host
    env["PUBLISHER_HOST_PORT"] = port
    env["TARGET_FILE"] = str(target)
    res = subprocess.run(["bash", str(RENDER)], env=env, capture_output=True, text=True, timeout=30)
    return res, target


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash benoetigt")
def test_valid_host_port_writes_valid_json(tmp_path):
    res, target = _run(tmp_path, "metrics-host.lab", "8001")
    assert res.returncode == 0, res.stderr
    data = json.loads(target.read_text())
    assert data[0]["targets"] == ["metrics-host.lab:8001"]
    assert data[0]["labels"]["app"] == "outbox-publisher"
    # atomar: keine Tempdatei zurueck
    assert not list(tmp_path.glob(".publisher.json.*"))


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash benoetigt")
def test_address_not_logged_by_default(tmp_path):
    res, _ = _run(tmp_path, "secret-host.lab", "8001")
    assert "secret-host.lab" not in res.stdout and "secret-host.lab" not in res.stderr


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash benoetigt")
@pytest.mark.parametrize("bad_host", [
    "http://host",          # scheme
    "host/path",            # path
    "host with space",      # whitespace
    'host"quote',           # quote
    "host,inject",          # comma / json-injection
    'a"]}],"x":[{"targets":["evil',  # json injection
    "",                     # empty
])
def test_rejects_injection_hosts(tmp_path, bad_host):
    res, target = _run(tmp_path, bad_host)
    assert res.returncode != 0
    assert not target.exists()


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash benoetigt")
@pytest.mark.parametrize("bad_port", ["0", "70000", "8a", "-1", "12.3"])
def test_rejects_invalid_ports(tmp_path, bad_port):
    res, target = _run(tmp_path, "host.lab", bad_port)
    assert res.returncode != 0
    assert not target.exists()


def test_example_target_is_valid_json():
    ex = REPO / "monitoring/prometheus/targets/publisher.json.example"
    json.loads(ex.read_text())
    assert "PUBLISHER_METRICS_HOST" in ex.read_text()  # nur Platzhalter, keine echte IP
