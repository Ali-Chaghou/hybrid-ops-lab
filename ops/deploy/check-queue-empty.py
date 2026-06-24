#!/usr/bin/env python3
"""Read-only Queue-Leerheitsgate fuer ElasticMQ (D3B2.1).

Nutzt AUSSCHLIESSLICH ListQueues + GetQueueAttributes (keine Receive/Delete/Send/
Purge/Redrive). Prueft, dass JEDE existierende Queue sichtbar=0 UND in-flight=0 ist.
Dient als Sicherheitsgate VOR einer kontrollierten ElasticMQ-Neuerstellung, damit
keine Nachrichten verloren gehen. Gibt KEINE Queue-URLs/Account-Teile aus.

usage:  check-queue-empty.py <endpoint-base-url>
        z. B. check-queue-empty.py http://localhost:9324
exit:   0 alle Queues leer (oder keine Queue vorhanden)
        2 nicht erreichbar / nicht sicher parsebar  -> fail closed
        3 mindestens eine Queue hat sichtbare/in-flight Nachrichten
        4 falsche Argumente
"""
from __future__ import annotations

import re
import sys
import urllib.request

_TIMEOUT = 6
_VISIBLE = "ApproximateNumberOfMessages"
_INFLIGHT = "ApproximateNumberOfMessagesNotVisible"


def _get(url: str) -> str:
    with urllib.request.urlopen(url, timeout=_TIMEOUT) as r:
        return r.read().decode("utf-8", "replace")


def _queue_names(list_xml: str) -> list[str]:
    # Aus <QueueUrl>.../<name></QueueUrl> nur den Namen (letztes Pfadsegment).
    names = []
    for u in re.findall(r"<QueueUrl>([^<]+)</QueueUrl>", list_xml):
        names.append(u.rstrip("/").rsplit("/", 1)[-1])
    return names


def _attr(attr_xml: str, name: str) -> int | None:
    # <Attribute><Name>X</Name><Value>N</Value></Attribute> (Reihenfolge tolerant).
    for blk in re.findall(r"<Attribute>(.*?)</Attribute>", attr_xml, re.S):
        n = re.search(r"<Name>([^<]+)</Name>", blk)
        v = re.search(r"<Value>([^<]*)</Value>", blk)
        if n and v and n.group(1) == name:
            try:
                return int(v.group(1))
            except ValueError:
                return None
    return None


def check(endpoint: str) -> int:
    base = endpoint.rstrip("/")
    try:
        list_xml = _get(f"{base}/?Action=ListQueues&Version=2012-11-05")
    except Exception:
        sys.stderr.write("queue-gate: ListQueues nicht erreichbar -> fail closed\n")
        return 2
    names = _queue_names(list_xml)
    if not names:
        print("queue-gate: keine Queue vorhanden (leer ok)")
        return 0
    not_empty = False
    for name in sorted(names):
        try:
            ax = _get(f"{base}/queue/{name}?Action=GetQueueAttributes"
                      f"&AttributeName.1={_VISIBLE}&AttributeName.2={_INFLIGHT}&Version=2012-11-05")
        except Exception:
            sys.stderr.write(f"queue-gate: GetQueueAttributes({name}) nicht erreichbar -> fail closed\n")
            return 2
        vis = _attr(ax, _VISIBLE)
        inf = _attr(ax, _INFLIGHT)
        if vis is None or inf is None:
            sys.stderr.write(f"queue-gate: Attribute fuer {name} nicht sicher parsebar -> fail closed\n")
            return 2
        print(f"queue-gate: {name} visible={vis} inflight={inf}")
        if vis != 0 or inf != 0:
            not_empty = True
    if not_empty:
        sys.stderr.write("queue-gate: Queue(s) NICHT leer -> Abbruch (keine Neuerstellung)\n")
        return 3
    return 0


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        sys.stderr.write("usage: check-queue-empty.py <endpoint-base-url>\n")
        return 4
    return check(argv[0])


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
