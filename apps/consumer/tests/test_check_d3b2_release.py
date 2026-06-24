"""Gate D3B2.1: ops/deploy/check-d3b2-local-release.sh (Release-Integritaetsgate).

Ausschliesslich temporaere Git-Repos + ueberschreibbares ls-remote — kein Netzzugriff
auf das echte Repository.
"""
from __future__ import annotations

import os
import pathlib
import shutil
import subprocess

import pytest

REPO = pathlib.Path(__file__).resolve().parents[3]
GUARD = REPO / "ops/deploy/check-d3b2-local-release.sh"

pytestmark = pytest.mark.skipif(shutil.which("git") is None or shutil.which("bash") is None,
                                reason="git + bash benoetigt")


def _git(repo, *args, check=True):
    return subprocess.run(["git", "-C", str(repo), *args], check=check,
                          capture_output=True, text=True)


def _make_repo(tmp_path):
    origin = tmp_path / "origin.git"
    work = tmp_path / "work"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(origin)], check=True, capture_output=True)
    subprocess.run(["git", "init", "-b", "main", str(work)], check=True, capture_output=True)
    _git(work, "config", "user.email", "t@example.invalid")
    _git(work, "config", "user.name", "test")
    _git(work, "config", "commit.gpgsign", "false")
    (work / "f.txt").write_text("hello\n", encoding="utf-8")
    (work / ".gitignore").write_text("state/\n", encoding="utf-8")
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "init")
    _git(work, "remote", "add", "origin", str(origin))
    _git(work, "push", "-q", "-u", "origin", "main")
    return work


def _run(work, *args, ls_remote=None):
    env = dict(os.environ)
    if ls_remote is not None:
        env["D3B2_LS_REMOTE_CMD"] = ls_remote
    return subprocess.run(["bash", str(GUARD), *args], cwd=str(work), env=env,
                          capture_output=True, text=True, timeout=30)


def _head(work):
    return _git(work, "rev-parse", "HEAD").stdout.strip()


# --- erlaubt ---------------------------------------------------------------

def test_clean_main_allowed(tmp_path):
    work = _make_repo(tmp_path)
    assert _run(work).returncode == 0, _run(work).stderr


def test_expected_sha_match_allowed(tmp_path):
    work = _make_repo(tmp_path)
    assert _run(work, _head(work)).returncode == 0


def test_ignored_state_file_does_not_block(tmp_path):
    work = _make_repo(tmp_path)
    (work / "state").mkdir()
    (work / "state" / "x.json").write_text("{}", encoding="utf-8")  # gitignored
    assert _run(work).returncode == 0


# --- abgelehnt -------------------------------------------------------------

def test_feature_branch_rejected(tmp_path):
    work = _make_repo(tmp_path)
    _git(work, "checkout", "-q", "-b", "feature")
    assert _run(work).returncode != 0


def test_unstaged_change_rejected(tmp_path):
    work = _make_repo(tmp_path)
    (work / "f.txt").write_text("changed\n", encoding="utf-8")
    assert _run(work).returncode != 0


def test_staged_change_rejected(tmp_path):
    work = _make_repo(tmp_path)
    (work / "f.txt").write_text("changed\n", encoding="utf-8")
    _git(work, "add", "-A")
    assert _run(work).returncode != 0


def test_untracked_nonignored_file_rejected(tmp_path):
    work = _make_repo(tmp_path)
    (work / "new.txt").write_text("x", encoding="utf-8")
    assert _run(work).returncode != 0


def test_head_not_equal_origin_main_rejected(tmp_path):
    work = _make_repo(tmp_path)
    # Lokaler, NICHT gepushter Commit -> origin/main bleibt alt.
    (work / "g.txt").write_text("y", encoding="utf-8")
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "local only")
    assert _run(work).returncode != 0


def test_ls_remote_divergent_rejected(tmp_path):
    work = _make_repo(tmp_path)
    other = "f" * 40
    assert _run(work, ls_remote=f"printf '%s\\trefs/heads/main\\n' {other}").returncode != 0


def test_ls_remote_empty_rejected(tmp_path):
    work = _make_repo(tmp_path)
    assert _run(work, ls_remote="true").returncode != 0


def test_ls_remote_ambiguous_rejected(tmp_path):
    work = _make_repo(tmp_path)
    h = _head(work)
    assert _run(work, ls_remote=f"printf '%s\\trefs/heads/main\\n%s\\trefs/heads/main\\n' {h} {h}").returncode != 0


def test_malformed_expected_sha_rejected(tmp_path):
    work = _make_repo(tmp_path)
    assert _run(work, "nothex").returncode != 0


def test_expected_sha_mismatch_rejected(tmp_path):
    work = _make_repo(tmp_path)
    assert _run(work, "a" * 40).returncode != 0


def test_no_remote_url_in_output(tmp_path):
    work = _make_repo(tmp_path)
    r = _run(work, "feature")  # malformed expected -> error path
    assert "origin.git" not in (r.stdout + r.stderr)
