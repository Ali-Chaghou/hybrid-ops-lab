# Contract-Tests fuer den kontrollierten Consumer-Failure-Test-Deploy.
from __future__ import annotations

import os
import pathlib
import shutil
import subprocess

import pytest


REPO = pathlib.Path(__file__).resolve().parents[3]
DEPLOY = REPO / "ops" / "deploy" / "deploy-consumer.sh"
RELEASE_IMAGE = "inventory-consumer:5d319cad54e5"


FAKE_DOCKER = """#!/usr/bin/env bash
printf "docker %s\n" "$*" >> "${CMDLOG}"
case "$1" in
  network) echo "172.30.0.1" ;;
  inspect) exit 0 ;;
  port) echo "0.0.0.0:30090" ;;
esac
exit 0
"""

FAKE_K3D = """#!/usr/bin/env bash
printf "k3d %s\n" "$*" >> "${CMDLOG}"
exit 0
"""

FAKE_KUBECTL = """#!/usr/bin/env bash
printf "kubectl %s\n" "$*" >> "${CMDLOG}"

case "$*" in
  *"get deployment inventory-consumer"*)
    printf "%s" "${FAKE_CURRENT_IMAGE}"
    exit 0
    ;;
  *"create secret generic consumer-db-creds"*)
    printf "apiVersion: v1\nkind: Secret\n"
    exit 0
    ;;
  *"apply -f -"*)
    cat >/dev/null
    exit 0
    ;;
  *"apply -f "*)
    file="${@: -1}"
    cat "$file" > "${RENDERED_MANIFEST}"
    exit 0
    ;;
esac

exit 0
"""


def write_exec(path: pathlib.Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def runtime_env(tmp_path: pathlib.Path, current_image: str = RELEASE_IMAGE) -> dict[str, str]:
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir()

    write_exec(fakebin / "docker", FAKE_DOCKER)
    write_exec(fakebin / "k3d", FAKE_K3D)
    write_exec(fakebin / "kubectl", FAKE_KUBECTL)

    env_file = tmp_path / "site-cloud.env"
    env_file.write_text(
        "CONSUMER_DB=consumer\n"
        "CONSUMER_DB_HOST_PORT=5433\n"
        "CONSUMER_APP_PASSWORD=test-password\n",
        encoding="utf-8",
    )

    targets = tmp_path / "targets"
    targets.mkdir()

    env = dict(os.environ)
    env.update(
        {
            "PATH": f"{fakebin}:{env["PATH"]}",
            "CMDLOG": str(tmp_path / "commands.log"),
            "RENDERED_MANIFEST": str(tmp_path / "rendered.yaml"),
            "FAKE_CURRENT_IMAGE": current_image,
            "ENV_FILE": str(env_file),
            "TARGET_DIR": str(targets),
            "CLUSTER": "site-cloud",
            "NETWORK": "k3d-site-cloud",
            "IMAGE": RELEASE_IMAGE,
            "CONSUMER_REUSE_EXISTING_IMAGE": "1",
            "CONSUMER_LAB_FAIL_AFTER_COMMIT_ONCE": "1",
        }
    )
    return env


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash benoetigt")
def test_injection_requires_reuse_mode():
    env = dict(os.environ)
    env["CONSUMER_LAB_FAIL_AFTER_COMMIT_ONCE"] = "1"

    result = subprocess.run(
        ["bash", str(DEPLOY)],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )

    assert result.returncode != 0
    assert "nur mit CONSUMER_REUSE_EXISTING_IMAGE=1" in result.stderr


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash benoetigt")
def test_reuse_mode_skips_build_and_renders_one_shot_injection(tmp_path):
    env = runtime_env(tmp_path)

    result = subprocess.run(
        ["bash", str(DEPLOY)],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr

    commands = pathlib.Path(env["CMDLOG"]).read_text(encoding="utf-8")
    rendered = pathlib.Path(env["RENDERED_MANIFEST"]).read_text(encoding="utf-8")

    assert "docker build" not in commands
    assert "k3d image import" not in commands
    assert f"image: {RELEASE_IMAGE}" in rendered
    assert "name: LAB_FAIL_AFTER_COMMIT_ONCE" in rendered
    assert "value: \"1\"" in rendered
    assert "__CONSUMER_IMAGE__" not in rendered
    assert "__LAB_FAIL_AFTER_COMMIT_ONCE__" not in rendered


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash benoetigt")
def test_default_mode_builds_and_renders_injection_disabled(tmp_path):
    env = runtime_env(tmp_path)
    env["CONSUMER_REUSE_EXISTING_IMAGE"] = "0"
    env["CONSUMER_LAB_FAIL_AFTER_COMMIT_ONCE"] = "0"

    result = subprocess.run(
        ["bash", str(DEPLOY)],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr

    commands = pathlib.Path(env["CMDLOG"]).read_text(encoding="utf-8")
    rendered = pathlib.Path(env["RENDERED_MANIFEST"]).read_text(encoding="utf-8")

    assert "docker build" in commands
    assert "k3d image import" in commands
    assert f"image: {RELEASE_IMAGE}" in rendered
    assert "name: LAB_FAIL_AFTER_COMMIT_ONCE" in rendered
    assert 'value: "0"' in rendered
    assert "__CONSUMER_IMAGE__" not in rendered
    assert "__LAB_FAIL_AFTER_COMMIT_ONCE__" not in rendered


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash benoetigt")
def test_reuse_mode_fails_closed_on_image_mismatch(tmp_path):
    env = runtime_env(tmp_path, current_image="inventory-consumer:wrong")

    result = subprocess.run(
        ["bash", str(DEPLOY)],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode != 0
    assert "laufendes Image stimmt nicht exakt" in result.stderr
    assert not pathlib.Path(env["RENDERED_MANIFEST"]).exists()
