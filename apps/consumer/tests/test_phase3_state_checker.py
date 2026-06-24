"""Gate D3B1: ops/deploy/check-phase-3-runtime-state.py (Exitcodes + Validierung)."""
from __future__ import annotations

import json
import pathlib
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parents[3]
CHECKER = REPO / "ops/deploy/check-phase-3-runtime-state.py"


def _run(path) -> int:
    return subprocess.run([sys.executable, str(CHECKER), str(path)],
                          capture_output=True, text=True).returncode


def _valid():
    return {"schema_version": 1, "gate": "D3B1", "step": "complete",
            "complete": True, "publisher_enabled": False, "updated_at": "x"}


def _write(tmp_path, obj_or_text):
    p = tmp_path / "state.json"
    p.write_text(json.dumps(obj_or_text) if not isinstance(obj_or_text, str) else obj_or_text,
                 encoding="utf-8")
    return p


def test_valid_complete_disabled_passes(tmp_path):
    assert _run(_write(tmp_path, _valid())) == 0


def test_missing_file_is_exit_2(tmp_path):
    assert _run(tmp_path / "nope.json") == 2


def test_invalid_json_is_exit_3(tmp_path):
    assert _run(_write(tmp_path, "{not json")) == 3


def test_not_complete_is_exit_3(tmp_path):
    s = _valid(); s["complete"] = False; s["step"] = "migration-complete"
    assert _run(_write(tmp_path, s)) == 3


def test_publisher_enabled_true_rejected(tmp_path):
    s = _valid(); s["publisher_enabled"] = True
    assert _run(_write(tmp_path, s)) == 3


def test_wrong_schema_version_rejected(tmp_path):
    s = _valid(); s["schema_version"] = 999
    assert _run(_write(tmp_path, s)) == 3


def test_unknown_step_rejected(tmp_path):
    s = _valid(); s["step"] = "frobnicate"
    assert _run(_write(tmp_path, s)) == 3


def test_no_args_is_usage_error(tmp_path):
    rc = subprocess.run([sys.executable, str(CHECKER)], capture_output=True).returncode
    assert rc == 4
