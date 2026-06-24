#!/usr/bin/env python3
"""Read-only Validator des D3B2.1-Consumer-Rollout-States (site-cloud).

Strukturelle Pruefung (nicht nur Existenz), ob der vom Controller
ops/deploy/upgrade-consumer-runtime.sh geschriebene State einen abgeschlossenen
Consumer-Rollout beschreibt. Genutzt von Makefile (`make cloud-state`) und Tests.
Keine Secrets, keine Live-Aktion.

Exitcodes:
  0  State vorhanden, gueltig, complete (ggf. release_sha == erwartet)
  2  State-Datei fehlt oder nicht lesbar
  3  State ungueltig / unvollstaendig / nicht complete / release-Mismatch
  4  falsche Aufrufargumente

usage: check-d3b2-consumer-state.py <state.json> [erwarteter-release-sha]
"""
from __future__ import annotations

import json
import re
import sys

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")

EXPECTED_SCHEMA_VERSION = 1
EXPECTED_GATE = "D3B2.1"
KNOWN_STEPS = (
    "preflight",
    "images-built",
    "queue-config-ready",
    "consumer-db-ready",
    "consumer-schema-ready",
    "consumer-deployed",
    "monitoring-ready",
    "verified",
    "complete",
)


def _fail(code: int, msg: str) -> int:
    sys.stderr.write(f"d3b2-state: {msg}\n")
    return code


def check(path: str, expect_sha: str | None = None) -> int:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = fh.read()
    except FileNotFoundError:
        return _fail(2, "State-Datei fehlt (Consumer-Rollout noch nicht abgeschlossen).")
    except OSError:
        return _fail(2, "State-Datei nicht lesbar.")
    try:
        data = json.loads(raw)
    except (ValueError, json.JSONDecodeError):
        return _fail(3, "State-Datei ist kein gueltiges JSON.")
    if not isinstance(data, dict):
        return _fail(3, "State muss ein JSON-Objekt sein.")
    if data.get("schema_version") != EXPECTED_SCHEMA_VERSION:
        return _fail(3, "unerwartete schema_version.")
    if data.get("gate") != EXPECTED_GATE:
        return _fail(3, "unerwartetes gate.")
    step = data.get("step")
    if step not in KNOWN_STEPS:
        return _fail(3, "unbekannter/fehlender step.")
    if data.get("complete") is not True:
        return _fail(3, f"State nicht complete (step={step}).")
    # Release-Bindung: release_sha muss vorhanden + wohlgeformt sein.
    rel = data.get("release_sha")
    if not isinstance(rel, str) or not _SHA_RE.match(rel):
        return _fail(3, "release_sha fehlt/ungueltig (40 hex erwartet).")
    if expect_sha is not None and rel != expect_sha:
        return _fail(3, "release_sha weicht vom erwarteten Release ab.")
    return 0


def main(argv: list[str]) -> int:
    if len(argv) not in (1, 2):
        sys.stderr.write("usage: check-d3b2-consumer-state.py <state.json> [erwarteter-release-sha]\n")
        return 4
    expect = argv[1] if len(argv) == 2 else None
    if expect is not None and not _SHA_RE.match(expect):
        sys.stderr.write("erwarteter-release-sha muss 40 hex sein\n")
        return 4
    return check(argv[0], expect)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
