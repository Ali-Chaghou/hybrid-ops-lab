"""Gate D3B2.1: ops/deploy/check-local-perms.sh, check-queue-empty.py, check-d3b2-consumer-state.py."""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

REPO = pathlib.Path(__file__).resolve().parents[3]
PERMS = REPO / "ops/deploy/check-local-perms.sh"
QEMPTY = REPO / "ops/deploy/check-queue-empty.py"
STATE = REPO / "ops/deploy/check-d3b2-consumer-state.py"

_bash = pytest.mark.skipif(shutil.which("bash") is None, reason="bash benoetigt")


# --- check-local-perms.sh ---------------------------------------------------

@_bash
@pytest.mark.parametrize("mode,ok", [(0o600, True), (0o400, True), (0o700, True),
                                     (0o644, False), (0o664, False), (0o640, False), (0o660, False)])
def test_perms_modes(tmp_path, mode, ok):
    f = tmp_path / "make.env"
    f.write_text("DC_HOST=x\n", encoding="utf-8")
    os.chmod(f, mode)
    rc = subprocess.run(["bash", str(PERMS), str(f)], capture_output=True).returncode
    assert (rc == 0) == ok


@_bash
def test_perms_missing_file(tmp_path):
    rc = subprocess.run(["bash", str(PERMS), str(tmp_path / "nope")], capture_output=True).returncode
    assert rc == 1


@_bash
def test_perms_bad_args():
    rc = subprocess.run(["bash", str(PERMS)], capture_output=True).returncode
    assert rc == 2


# --- check-queue-empty.py (echter HTTP-Fixture-Server) ----------------------

class _MQHandler(BaseHTTPRequestHandler):
    queues: dict = {}

    def log_message(self, *a):
        pass

    def _xml(self, body):
        self.send_response(200)
        self.send_header("Content-Type", "application/xml")
        self.end_headers()
        self.wfile.write(body.encode())

    def do_GET(self):
        if "Action=ListQueues" in self.path:
            urls = "".join(f"<QueueUrl>http://h:9324/000000000000/{n}</QueueUrl>" for n in self.queues)
            self._xml(f"<ListQueuesResponse>{urls}</ListQueuesResponse>")
        elif "Action=GetQueueAttributes" in self.path:
            name = self.path.split("?")[0].rstrip("/").rsplit("/", 1)[-1]
            vis, inf = self.queues.get(name, (0, 0))
            self._xml("<GetQueueAttributesResponse>"
                      f"<Attribute><Name>ApproximateNumberOfMessages</Name><Value>{vis}</Value></Attribute>"
                      f"<Attribute><Name>ApproximateNumberOfMessagesNotVisible</Name><Value>{inf}</Value></Attribute>"
                      "</GetQueueAttributesResponse>")
        else:
            self._xml("<x/>")


def _serve(queues):
    _MQHandler.queues = queues
    srv = HTTPServer(("127.0.0.1", 0), _MQHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def _qcheck(endpoint):
    return subprocess.run([sys.executable, str(QEMPTY), endpoint], capture_output=True, text=True).returncode


def test_queue_empty_passes():
    srv, ep = _serve({"inventory-movements": (0, 0), "inventory-movements-dlq": (0, 0)})
    try:
        assert _qcheck(ep) == 0
    finally:
        srv.shutdown()


def test_queue_no_queues_passes():
    srv, ep = _serve({})
    try:
        assert _qcheck(ep) == 0
    finally:
        srv.shutdown()


def test_queue_with_visible_messages_aborts():
    srv, ep = _serve({"inventory-movements": (3, 0)})
    try:
        assert _qcheck(ep) == 3
    finally:
        srv.shutdown()


def test_queue_with_inflight_messages_aborts():
    srv, ep = _serve({"inventory-movements": (0, 1)})
    try:
        assert _qcheck(ep) == 3
    finally:
        srv.shutdown()


def test_queue_unreachable_fails_closed():
    # Kein Server auf diesem Port.
    assert _qcheck("http://127.0.0.1:1") == 2


def test_queue_bad_args():
    assert subprocess.run([sys.executable, str(QEMPTY)], capture_output=True).returncode == 4


# --- check-d3b2-consumer-state.py -------------------------------------------

_SHA = "0123456789abcdef0123456789abcdef01234567"


def _valid():
    return {"schema_version": 1, "gate": "D3B2.1", "step": "complete", "complete": True,
            "release_sha": _SHA, "runtime_image_tag": "inventory-consumer:0123456789ab", "updated_at": "x"}


def _scheck(tmp_path, obj, *extra):
    p = tmp_path / "state.json"
    p.write_text(json.dumps(obj) if not isinstance(obj, str) else obj, encoding="utf-8")
    return subprocess.run([sys.executable, str(STATE), str(p), *extra], capture_output=True).returncode


def test_state_valid(tmp_path):
    assert _scheck(tmp_path, _valid()) == 0


def test_state_missing_release_sha_rejected(tmp_path):
    s = _valid(); del s["release_sha"]
    assert _scheck(tmp_path, s) == 3


def test_state_malformed_release_sha_rejected(tmp_path):
    s = _valid(); s["release_sha"] = "nothex"
    assert _scheck(tmp_path, s) == 3


def test_state_expected_sha_match(tmp_path):
    assert _scheck(tmp_path, _valid(), _SHA) == 0


def test_state_expected_sha_mismatch_rejected(tmp_path):
    assert _scheck(tmp_path, _valid(), "f" * 40) == 3


def test_state_missing(tmp_path):
    assert subprocess.run([sys.executable, str(STATE), str(tmp_path / "nope")], capture_output=True).returncode == 2


def test_state_corrupt(tmp_path):
    assert _scheck(tmp_path, "{bad") == 3


def test_state_not_complete(tmp_path):
    s = _valid(); s["complete"] = False; s["step"] = "monitoring-ready"
    assert _scheck(tmp_path, s) == 3


def test_state_wrong_gate(tmp_path):
    s = _valid(); s["gate"] = "D3B1"
    assert _scheck(tmp_path, s) == 3


def test_state_unknown_step(tmp_path):
    s = _valid(); s["step"] = "frob"
    assert _scheck(tmp_path, s) == 3
