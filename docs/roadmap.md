# Roadmap

[Übersicht](../README.md) · [Dokumentation](README.md) · [Status & Roadmap](roadmap.md) · [Nachweise](evidence-index.md) · [Security](../SECURITY.md)

Synthetisches Lab, keine Produktionsumgebung. Diese Seite beschreibt den Weg vom heutigen
Stand bis zum geplanten Phase-3-Abschluss.

## Aktueller Stand

D3B2.1 wurde am 28. Juni 2026 im Lab abgeschlossen und verifiziert
(Release `5d319cad54e5a26dd59baacc1c269780ee90b1e4`).

D3B2.2 wurde am 30. Juni 2026 kontrolliert ausgerollt und live verifiziert:

- Migration `0004_add_outbox_claim_fields` ist auf `site-dc` angewendet.
- Inventory und der deaktivierte Publisher sind healthy.
- Die Publisher-Rolle erfüllt Least Privilege.
- Das Publisher-Prometheus-Target ist aktiv und `up`.
- Prometheus meldet `8/8` Targets up.
- Main Queue und DLQ sind leer.
- Keine Outbox-Zeile wurde geclaimt oder publiziert.

D3B2.3 wurde am 30. Juni 2026 begonnen und ist **in progress**: Aktivierungs-Preflight,
kontrollierte Publisher-Aktivierung, ein echter Ende-zu-Ende-Pfad sowie Disable-Test und
Re-enable mit sicherem Backlog-Nachlauf sind im Lab live verifiziert. Am 2. Juli 2026
wurden zusätzlich die One-shot Failure Injection nach DB-Commit und vor Queue-Delete,
die Redelivery nach Visibility Timeout, das Transport-Duplikat und die genau einmalige
fachliche Projection-Wirkung live bewiesen. Offen bleiben der Validation-/Poison-Fall,
der vollständige DLQ-Weg, Redrive oder eine dokumentierte ElasticMQ-Grenze sowie der
abschließende Gesamtcheck. Phase 3 ist deshalb noch nicht formal abgeschlossen. Das
System bleibt at-least-once.

Nachweise:
[D3B2.1-Abschlussnachweis](handoff-d3b2.1-complete.md),
[D3B2.2-Abschlussnachweis](evidence-d3b2.2.md) und
[D3B2.3-Zwischennachweis](evidence-d3b2.3.md).

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
- **Status:** am 30. Juni 2026 abgeschlossen und live verifiziert. Nachweis:
  [D3B2.2-Abschlussnachweis](evidence-d3b2.2.md).

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

**Abgeschlossen innerhalb D3B2.3** (am 30. Juni und 2. Juli 2026 live verifiziert,
Nachweis: [D3B2.3-Runtime-Nachweis](evidence-d3b2.3.md)):

- Aktivierungs-Preflight;
- Publisher-Aktivierung;
- realer E2E-Pfad (Inventory API → Transactional Outbox → Publisher → Main Queue →
  Consumer Inbox → Movement Projection);
- Disable-Test (Event blieb `pending` ohne Claim, Main Queue und DLQ leer);
- Re-enable und sicherer Backlog-Nachlauf (das wartende Event wurde veröffentlicht,
  konsumiert und projiziert);
- One-shot Failure Injection nach DB-Commit und vor Queue-Delete;
- Redelivery nach dem Visibility Timeout;
- Transport-Duplikat derselben Queue-Nachricht und `event_id`;
- exakt eine Inbox-Zeile und eine Projection-Wirkung trotz zweifacher Zustellung;
- kontrollierter Rollback der Injection auf `0`.

**Offen innerhalb D3B2.3:**

- kontrollierter Validation-/Poison-Fall;
- vollständiger Weg einer dauerhaft nicht verarbeitbaren Nachricht in die DLQ;
- Redrive aus der DLQ oder belastbare Dokumentation einer ElasticMQ-Grenze;
- abschließender Gesamtcheck;
- formaler Abschluss.

- **Status:** in progress. Failure Injection, Redelivery und Transport-Duplikat sind
  live verifiziert; Poison-, DLQ- und Redrive-Nachweise stehen noch aus.

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
