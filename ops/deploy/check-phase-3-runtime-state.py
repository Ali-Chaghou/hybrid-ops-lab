#!/usr/bin/env python3
"""Read-only Validator des Phase-3-Runtime-States (site-dc, Gate D3B1).

Prueft STRUKTURELL (nicht nur Dateiexistenz), ob der von
ops/deploy/upgrade-phase-3-runtime.sh geschriebene State einen abgeschlossenen,
Publisher-deaktivierten Phase-3-Runtime-Zustand beschreibt. Wird von Makefile
(`make up`) und Tests genutzt. Keine Secrets, keine Live-Aktion.

Exitcodes (eindeutig):
  0  State vorhanden, gueltig, complete, publisher disabled  -> site-dc darf starten
  2  State-Datei fehlt oder ist nicht lesbar
  3  State ungueltig / unvollstaendig / nicht complete / publisher nicht disabled
  4  falsche Aufrufargumente
"""
from __future__ import annotations

import json
import sys

EXPECTED_SCHEMA_VERSION = 1
EXPECTED_GATE = "D3B1"
KNOWN_STEPS = (
    "preflight",
    "images-built",
    "roles-ready",
    "inventory-stopped",
    "migration-complete",
    "inventory-ready",
    "publisher-disabled-ready",
    "complete",
)


def _fail(code: int, msg: str) -> int:
    sys.stderr.write(f"phase3-state: {msg}\n")
    return code


def check(path: str) -> int:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = fh.read()
    except FileNotFoundError:
        return _fail(2, "State-Datei fehlt (Phase-3-Upgrade noch nicht abgeschlossen).")
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
    if data.get("publisher_enabled") is not False:
        return _fail(3, "publisher_enabled muss false sein.")
    return 0


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        sys.stderr.write("usage: check-phase-3-runtime-state.py <state.json>\n")
        return 4
    return check(argv[0])


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
