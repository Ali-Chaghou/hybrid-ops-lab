#!/usr/bin/env python3
"""Deterministische, read-only Auswahl GENAU EINES Consumer-Pods (bzw. einer Restart-
Kennzahl) aus einer `kubectl ... get pod -l app=<deploy> -o json`-Ausgabe.

Liest die Pod-Liste von stdin und gibt genau das angeforderte, nicht-sensible Feld
des deterministisch gewaehlten Pods aus. Ersetzt das nicht deterministische `.items[0]`
in Runtime-Verifikation, Rollback- und Restart-Pfad: die Kubernetes-Listenreihenfolge
ist nicht garantiert, weshalb `.items[0]` waehrend eines Rollouts einen alten/
terminierenden Pod treffen kann.

Container-Auswahl OHNE Fallback: es wird ausschliesslich der Container mit
`name == <--container>` (Default 'consumer') gewaehlt — niemals blind Index 0. Ein
einzelner falsch benannter oder unbenannter Container gilt NICHT als Consumer.
Doppelter Consumer-Containername (Spec oder Status) -> Input-Fehler (kein 'erster').

Strenge Struktur-/Typvalidierung (sonst Exit 2, niemals Traceback/Pod-JSON):
  root Objekt; items Liste; jedes Pod-Element Objekt; metadata/spec/status Objekte;
  metadata.name nicht leerer String; spec.containers Liste; status.containerStatuses
  Liste (falls vorhanden); Containerobjekte Objekte; Containernamen Strings; image/
  imageID Strings; ready boolean (falls ausgewertet); restartCount nicht negative
  Ganzzahl und kein bool; Sortier-/Zeitfelder Strings oder fehlend.

Auswahlregeln (Pod-Modi): terminierende Pods (metadata.deletionTimestamp) IMMER
ausgeschlossen; optionale Invarianten (Running, Ready, exaktes Image, nicht leere
Image-ID) filtern; unter den verbleibenden wird der JUENGSTE gewaehlt
(status.startTime, Fallback metadata.creationTimestamp, dann Name), Name als
deterministischer Tie-Break (lexikografisch groesster).

Aggregat-Modus `--print max-restart-count`: hoechste restartCount ueber ALLE nicht
terminierenden Consumer-Pods (nicht nur der juengste). Keine nicht terminierenden
Consumer-Pods -> 0.

Mehrfeld-Modus `--print identity`: gibt ausschliesslich {"name":...,"imageID":...} des
gewaehlten Pods aus EINEM Snapshot aus (fuer kohaerente CRI-Pruefung + Diagnose),
niemals Annotations/Env/ganze Pod-Objekte.

Read-only: ausschliesslich stdin; KEINE kubectl-/Netz-/Schreiboperation.

Exit-Codes (an cri-image-identity.py angelehnt, klar unterscheidbar):
  0  ok
  2  ungueltige/leere/malformed Eingabe bzw. unerwartete Datenstruktur (fail closed)
  3  kein passender Pod
  4  ungueltige CLI-Argumente
"""
from __future__ import annotations

import argparse
import json
import sys

_FIELDS = ("name", "image", "imageID", "restartCount", "max-restart-count", "identity")

EXIT_OK = 0
EXIT_INPUT = 2
EXIT_NOMATCH = 3
EXIT_ARGS = 4


class _InputError(Exception):
    """Strukturell/typmaessig unerwartete Eingabe -> kontrolliert Exit 2."""


class _ArgParser(argparse.ArgumentParser):
    """Eindeutiger Exit-Code 4 fuer ungueltige CLI-Argumente (statt argparse-Default 2,
    der sonst mit 'ungueltige Eingabe' verschmelzen wuerde)."""

    def error(self, message: str):  # noqa: D401
        self.print_usage(sys.stderr)
        sys.exit(EXIT_ARGS)


def _need_dict(v, what: str) -> dict:
    if not isinstance(v, dict):
        raise _InputError(f"{what}: Objekt erwartet")
    return v


def _need_list(v, what: str) -> list:
    if not isinstance(v, list):
        raise _InputError(f"{what}: Liste erwartet")
    return v


def _need_str(v, what: str, *, allow_empty: bool = True) -> str:
    if not isinstance(v, str):
        raise _InputError(f"{what}: String erwartet")
    if not allow_empty and not v:
        raise _InputError(f"{what}: nicht leer erwartet")
    return v


def _opt_str(v, what: str):
    if v is not None and not isinstance(v, str):
        raise _InputError(f"{what}: String oder fehlend erwartet")


def _validate_container(c, what: str) -> dict:
    _need_dict(c, what)
    if c.get("name") is not None and not isinstance(c.get("name"), str):
        raise _InputError(f"{what}.name: String erwartet")
    _opt_str(c.get("image"), f"{what}.image")
    return c


def _validate_status(c, what: str) -> dict:
    _need_dict(c, what)
    if c.get("name") is not None and not isinstance(c.get("name"), str):
        raise _InputError(f"{what}.name: String erwartet")
    _opt_str(c.get("imageID"), f"{what}.imageID")
    if c.get("ready") is not None and not isinstance(c.get("ready"), bool):
        raise _InputError(f"{what}.ready: Boolean erwartet")
    rc = c.get("restartCount")
    if rc is not None and (isinstance(rc, bool) or not isinstance(rc, int) or rc < 0):
        raise _InputError(f"{what}.restartCount: nicht negative Ganzzahl erwartet")
    return c


def _validate_pod(pod, name: str) -> None:
    _need_dict(pod, "pod")
    md = _need_dict(pod.get("metadata"), "metadata")
    _need_str(md.get("name"), "metadata.name", allow_empty=False)
    _opt_str(md.get("deletionTimestamp"), "metadata.deletionTimestamp")
    _opt_str(md.get("creationTimestamp"), "metadata.creationTimestamp")
    spec = _need_dict(pod.get("spec"), "spec")
    containers = _need_list(spec.get("containers"), "spec.containers")
    for c in containers:
        _validate_container(c, "spec.containers[]")
    status = _need_dict(pod.get("status"), "status")
    _opt_str(status.get("startTime"), "status.startTime")
    _opt_str(status.get("phase"), "status.phase")
    cstats = status.get("containerStatuses")
    if cstats is not None:
        _need_list(cstats, "status.containerStatuses")
        for c in cstats:
            _validate_status(c, "status.containerStatuses[]")
    # Doppelter Consumer-Containername (Spec oder Status) -> Input-Fehler, nicht 'erster'.
    if len([c for c in containers if c.get("name") == name]) > 1:
        raise _InputError("doppelter Consumer-Container in spec.containers")
    if cstats is not None and len([c for c in cstats if c.get("name") == name]) > 1:
        raise _InputError("doppelter Consumer-Container in status.containerStatuses")


def _spec_container(pod: dict, name: str):
    for c in pod["spec"]["containers"]:
        if c.get("name") == name:
            return c
    return None


def _status_container(pod: dict, name: str):
    for c in pod["status"].get("containerStatuses") or []:
        if c.get("name") == name:
            return c
    return None


def _sort_key(pod: dict):
    md = pod.get("metadata", {})
    st = pod.get("status", {}).get("startTime") or ""
    return (st, md.get("creationTimestamp") or "", md.get("name", ""))


def _parse_args():
    ap = _ArgParser(description="Deterministische read-only Consumer-Pod-Auswahl.")
    ap.add_argument("--container", default="consumer",
                    help="Name des Consumer-Containers (Default 'consumer'); kein Index-0-Fallback.")
    ap.add_argument("--expect-image", default=None,
                    help="Falls gesetzt: spec.containers[name].image MUSS exakt diesem Wert entsprechen.")
    ap.add_argument("--require-running", action="store_true",
                    help="status.phase MUSS 'Running' sein.")
    ap.add_argument("--require-ready", action="store_true",
                    help="containerStatuses[name].ready MUSS true sein.")
    ap.add_argument("--require-image-id", action="store_true",
                    help="containerStatuses[name].imageID MUSS nicht leer sein.")
    ap.add_argument("--print", dest="field", default="name", choices=_FIELDS,
                    help="Auszugebendes Feld bzw. Aggregat/Identity des gewaehlten Pods.")
    return ap.parse_args()


def _run(args) -> int:
    data = json.load(sys.stdin)  # JSONDecodeError -> in main() als Input-Fehler behandelt
    _need_dict(data, "root")
    items = _need_list(data.get("items"), "items")
    name = args.container

    alive = []
    for pod in items:
        _validate_pod(pod, name)
        if pod["metadata"].get("deletionTimestamp") is not None:
            continue
        alive.append(pod)

    if args.field == "max-restart-count":
        # Restart-Gate-Aggregat (fail closed bei Strukturbruch): jeder nicht
        # terminierende Pod MUSS genau einen Consumer-Spec-Container haben (Fehlen/
        # Doppelung -> Exit 2; Doppelung faengt bereits _validate_pod ab). Fehlt
        # status.containerStatuses ganz ODER fehlt nur der Consumer-Status (frisch
        # startender Pod, uebrige Struktur konsistent), gilt dokumentiert restartCount 0
        # — ein VORHANDENER, aber ungueltiger restartCount bleibt Exit 2 (_validate_pod).
        counts = []
        for pod in alive:
            if _spec_container(pod, name) is None:
                raise _InputError("Consumer-Spec-Container fehlt (Restart-Gate fail closed)")
            cs = _status_container(pod, name)
            counts.append(int(cs.get("restartCount", 0)) if cs is not None else 0)
        print(max(counts) if counts else 0)
        return EXIT_OK

    candidates = []
    for pod in alive:
        sc = _spec_container(pod, name)
        if sc is None:
            continue  # kein eindeutiger Consumer-Container -> kein Kandidat
        st = pod["status"]
        if args.require_running and st.get("phase") != "Running":
            continue
        cs = _status_container(pod, name) or {}
        if args.require_ready and cs.get("ready") is not True:
            continue
        if args.expect_image is not None and (sc.get("image") or "") != args.expect_image:
            continue
        if args.require_image_id and not (cs.get("imageID") or ""):
            continue
        candidates.append((pod, sc, cs))

    if not candidates:
        return EXIT_NOMATCH

    pod, sc, cs = max(candidates, key=lambda t: _sort_key(t[0]))  # juengster, Name als Tie-Break
    name_v = pod["metadata"]["name"]
    image_v = sc.get("image") or ""
    imageid_v = cs.get("imageID") or ""
    restart_v = cs.get("restartCount", 0)

    if args.field == "identity":
        if not name_v or not imageid_v:
            return EXIT_NOMATCH
        print(json.dumps({"name": name_v, "imageID": imageid_v}, separators=(",", ":")))
        return EXIT_OK

    values = {"name": name_v, "image": image_v, "imageID": imageid_v, "restartCount": restart_v}
    out = values[args.field]
    if args.field != "restartCount" and out == "":
        return EXIT_NOMATCH
    print(out)
    return EXIT_OK


def main() -> int:
    args = _parse_args()
    try:
        return _run(args)
    except _InputError:
        return EXIT_INPUT
    except SystemExit:
        raise
    except Exception:
        # Niemals Traceback oder Pod-Rohdaten ausgeben -> kontrolliert Exit 2.
        return EXIT_INPUT


if __name__ == "__main__":
    sys.exit(main())
