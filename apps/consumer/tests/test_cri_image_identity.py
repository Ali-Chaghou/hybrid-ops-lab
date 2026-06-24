"""Gate D3B2.1: ops/deploy/cri-image-identity.py — volle Digest-Identitaet via CRI."""
from __future__ import annotations

import json
import pathlib
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parents[3]
HELPER = REPO / "ops/deploy/cri-image-identity.py"

_A = "sha256:" + "a" * 64
_B = "sha256:" + "b" * 64


def _run(stdin_obj, *args):
    raw = stdin_obj if isinstance(stdin_obj, str) else json.dumps(stdin_obj)
    return subprocess.run([sys.executable, str(HELPER), *args], input=raw,
                          capture_output=True, text=True)


def _status(id_=_A, repo_tags=None, repo_digests=None):
    return {"status": {"id": id_, "repoTags": repo_tags or [], "repoDigests": repo_digests or []}}


def test_pod_matches_status_id_allowed():
    r = _run(_status(id_=_A), _A)
    assert r.returncode == 0 and r.stdout.strip() == _A


def test_pod_matches_repodigest_allowed():
    # status.id weicht ab, aber Pod-Digest ist ein RepoDigest -> erlaubt.
    r = _run(_status(id_=_B, repo_digests=[f"docker.io/library/x@{_A}"]), _A)
    assert r.returncode == 0 and r.stdout.strip() == _B


def test_docker_id_differs_but_cri_matches_allowed():
    # "Docker-.Id" spielt keine Rolle; nur die CRI-Identitaet zaehlt.
    r = _run(_status(id_=_A, repo_tags=["docker.io/library/inventory-consumer:dev"]), _A)
    assert r.returncode == 0


def test_pod_and_cri_digest_differ_rejected():
    assert _run(_status(id_=_A), _B).returncode == 3


def test_truncated_pod_digest_rejected():
    assert _run(_status(id_=_A), "sha256:" + "a" * 12).returncode == 2


def test_malformed_pod_digest_rejected():
    assert _run(_status(id_=_A), "notadigest").returncode == 2


def test_empty_cri_response_rejected():
    assert _run("", _A).returncode == 2


def test_missing_status_id_rejected():
    assert _run({"status": {"repoTags": ["x:dev"]}}, _A).returncode == 2


def test_unparseable_json_rejected():
    assert _run("{not json", _A).returncode == 2


def test_no_prefix_match():
    # 64-hex, die sich nur im letzten Zeichen unterscheiden -> KEIN Praefixtreffer.
    near = "sha256:" + "a" * 63 + "b"
    assert _run(_status(id_=_A), near).returncode == 3


def test_source_ref_prefers_repotag():
    r = _run(_status(id_=_A, repo_tags=["docker.io/library/inventory-consumer:dev"],
                     repo_digests=[f"x@{_A}"]), "--source-ref", _A)
    assert r.returncode == 0 and r.stdout.strip() == "docker.io/library/inventory-consumer:dev"


def test_source_ref_falls_back_to_repodigest():
    r = _run(_status(id_=_A, repo_digests=[f"docker.io/library/x@{_A}"]), "--source-ref", _A)
    assert r.returncode == 0 and r.stdout.strip() == f"docker.io/library/x@{_A}"


def test_source_ref_none_rejected():
    assert _run(_status(id_=_A), "--source-ref", _A).returncode == 2


def test_bad_args():
    assert _run(_status(), "a", "b", "c").returncode == 4
