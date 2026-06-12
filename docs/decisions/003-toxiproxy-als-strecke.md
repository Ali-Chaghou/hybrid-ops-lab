# ADR-003: Toxiproxy als simulierte Standortverbindung

**Status:** Accepted
**Datum:** 2026-06-11

## Kontext

Das zentrale Demo-Szenario ist eine Degradation der Verbindung zwischen
den Standorten – der Fall, der in Hybrid-Umgebungen am häufigsten
zwischen Teams und Providern hin- und hergeschoben wird. Dafür muss die
Strecke gezielt störbar sein: Latenz, Paketverlust, Komplettausfall.
Eine normale Netzwerkverbindung bietet dafür keinen sauberen Hebel.

## Entscheidung

Der gesamte Verkehr von site-dc nach site-cloud läuft durch Toxiproxy.
Die Strecke wird damit zu einem steuerbaren Objekt: Störungen lassen
sich per API reproduzierbar ein- und ausschalten, was definierte
Incident-Übungen ermöglicht.

## Konsequenzen

- Störungen sind reproduzierbar und skriptbar (Demo-Szenario,
  Chaos-Skripte).
- Die SLA-Schwellen aus docs/provider-management.md lassen sich gezielt
  testen.
- Nachteil: Toxiproxy arbeitet auf TCP-Ebene – kein echtes Routing,
  kein BGP, keine Layer-2-Effekte.
- Nachteil: ein zusätzlicher Single Point of Failure auf dem Datenpfad,
  in einer Demo akzeptabel.
