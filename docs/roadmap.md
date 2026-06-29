# Roadmap

[Übersicht](../README.md) · [Dokumentation](README.md) · [Status & Roadmap](roadmap.md) · [Nachweise](evidence-index.md) · [Security](../SECURITY.md)

Synthetisches Lab, keine Produktionsumgebung. Diese Seite beschreibt den Weg vom heutigen
Stand bis zum geplanten Phase-3-Abschluss.

## Aktueller Stand

D3B2.1 wurde am 28. Juni 2026 im Lab abgeschlossen und verifiziert (Release
`5d319cad54e5a26dd59baacc1c269780ee90b1e4`). D1 und D2 laufen auf `site-cloud`: Consumer
ready, keine Restarts, Prometheus ready, Consumer-Target up, kein Publisher-Target, Queue
und DLQ leer. site-dc wurde nicht verändert. Der Publisher und der vollständige
Phase-3-Eventfluss sind nicht aktiviert. Ergebnisse:
[D3B2.1-Abschlussnachweis](handoff-d3b2.1-complete.md).

## D3B2.2 — site-dc-Migration

- **Ziel:** Migration `0004` auf site-dc anwenden und die neue Inventory-Version starten;
  der Publisher bleibt deaktiviert.
- **Voraussetzungen:** D3B2.1-Stand, geprüftes Backup, sauberer `main`-Stand.
- **Änderung:** Schema-Migration auf site-dc, Neustart der Inventory-Runtime
  (Variante B mit kurzem, definiertem Downtime-Fenster).
- **Sicherheitsgrenze:** `EVENTS_ENABLED` und `PUBLISHER_ENABLED` bleiben aus; kein
  Eventversand; Least-Privilege-Rollen.
- **Erfolgskriterien:** Migration `0004` angewendet, neue Inventory-Version ready, kein
  Eventversand, Monitoring unauffällig.
- **Rückweg:** dokumentierter Variante-B-Rückweg (siehe
  [Phase-3-Runtime-Upgrade-Runbook](runbook-phase-3-runtime-upgrade.md)).
- **Status:** geplant.

## D3B2.3 — Publisher-Aktivierung und Ende-zu-Ende-Nachweis

- **Ziel:** den Publisher aktivieren und den Fluss
  `event_outbox → Publisher → Queue → Consumer` nachweisen.
- **Voraussetzungen:** D3B2.2 abgeschlossen, Route-Konfiguration und Publisher-Secret
  gesetzt.
- **Änderung:** `PUBLISHER_ENABLED=true`, erster echter Eventfluss, Publisher-Metriken
  und Alerts.
- **Sicherheitsgrenze:** eigene Least-Privilege-Rolle, enable-gated Alerts, jederzeit
  deaktivierbar.
- **Erfolgskriterien:** Ende-zu-Ende belegt; Idempotenz, DLQ-Weg, Negativfall, Disable und
  Rückweg getestet; Publisher-Metriken und Alerts in der Lab-Laufzeit sichtbar.
- **Rückweg:** `PUBLISHER_ENABLED=false` und Disable-Test.
- **Status:** geplant.

## Weitere Abschlussarbeiten

- Negativ- und Fehlerfalltests (Poison-Nachrichten in die DLQ, Konflikte, Recovery).
- Aktualisierung des Architekturdiagramms (aktiver und noch inaktiver Pfad sichtbar).
- Reproduktion aus einem sauberen Clone (Voraussetzungen, `.env`-Vorlagen, Make-Ziele).
- öffentlicher Security-/Leak-Check vor einem Release-Tag.
- möglicher Release-Tag mit Verweis auf die technischen Nachweise.

## Abschlusskriterien

Das Projekt erreicht den geplanten Phase-3-Abschluss, sobald D3B2.2 und D3B2.3 im Lab
verifiziert sind, der Ende-zu-Ende-Fluss inklusive Negativ-, Disable- und Rückwegtest
belegt ist, die Dokumentation (README, ADRs, Runbooks, Nachweise, Diagramm) den aktuellen
Stand abbildet, der Security-/Leak-Check abgeschlossen ist und ein reproduzierbarer Clone
mit grüner CI vorliegt.
