#!/usr/bin/env python3
"""CRI/containerd-Image-Identitaetsnachweis fuer den D3B2.1-Rollback (Source of Truth).

Liest `crictl inspecti -o json` auf stdin und prueft, ob ein uebergebener
Pod-Image-Digest EXAKT zur CRI-Identitaet dieses Images gehoert (status.id ODER ein
status.repoDigests-Eintrag). Vergleicht ausschliesslich VOLLE sha256:<64-hex>-Digests
— KEIN Praefix-/Kurzvergleich, KEINE Docker-.Id.

Der Docker-Daemon ist hier bewusst NICHT beteiligt: Docker- und containerd-IDs sind
verschiedene Identitaetsdomaenen.

Modi:
  cri-image-identity.py <pod-image-digest>              -> druckt canonical status.id
  cri-image-identity.py --source-ref <pod-image-digest> -> druckt nutzbare Quell-Referenz
                                                           (repoTag, sonst repoDigest)

exit: 0 ok | 2 leer/malformed/nicht zuordenbar | 3 Digest gehoert NICHT zur Identitaet
      4 falsche Argumente
"""
from __future__ import annotations

import json
import re
import sys

_FULL = re.compile(r"sha256:[0-9a-f]{64}(?![0-9a-f])")


def _full_digest(s: str | None) -> str | None:
    if not s:
        return None
    m = _FULL.search(s)
    return m.group(0) if m else None


def _load_status(raw: str):
    if not raw.strip():
        return None, "leere CRI-Antwort"
    try:
        data = json.loads(raw)
    except (ValueError, json.JSONDecodeError):
        return None, "CRI-JSON nicht parsebar"
    if not isinstance(data, dict):
        return None, "CRI-JSON kein Objekt"
    st = data.get("status")
    if not isinstance(st, dict):
        return None, "CRI status fehlt"
    return st, None


def main(argv: list[str]) -> int:
    source_ref = False
    if argv and argv[0] == "--source-ref":
        source_ref = True
        argv = argv[1:]
    if len(argv) != 1:
        sys.stderr.write("usage: cri-image-identity.py [--source-ref] <pod-image-digest>\n")
        return 4

    pod = _full_digest(argv[0])
    if not pod:
        sys.stderr.write("cri-identity: Pod-Image-Digest fehlt/verkuerzt/malformed\n")
        return 2

    st, err = _load_status(sys.stdin.read())
    if err:
        sys.stderr.write(f"cri-identity: {err}\n")
        return 2

    cid = _full_digest(st.get("id"))
    if not cid:
        sys.stderr.write("cri-identity: status.id fehlt/ungueltig\n")
        return 2

    identity = {cid}
    for rd in st.get("repoDigests") or []:
        f = _full_digest(rd)
        if f:
            identity.add(f)

    if pod not in identity:
        sys.stderr.write("cri-identity: Pod-Digest gehoert NICHT zur CRI-Identitaet des Images\n")
        return 3

    if source_ref:
        tags = [t for t in (st.get("repoTags") or []) if isinstance(t, str) and t]
        digs = [d for d in (st.get("repoDigests") or []) if isinstance(d, str) and d]
        ref = tags[0] if tags else (digs[0] if digs else "")
        if not ref:
            sys.stderr.write("cri-identity: keine nutzbare Quell-Referenz (repoTag/repoDigest)\n")
            return 2
        print(ref)
    else:
        print(cid)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
