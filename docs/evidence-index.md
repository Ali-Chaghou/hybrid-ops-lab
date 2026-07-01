# Technische Nachweise

[Übersicht](../README.md) · [Dokumentation](README.md) · [Status & Roadmap](roadmap.md) · [Nachweise](evidence-index.md) · [Security](../SECURITY.md)

Diese Seite verbindet die Projektphasen mit ihren technischen Nachweisen, Runbooks und
Laufzeitergebnissen. Synthetisches Lab, keine Produktionsumgebung.

„Implementiert" bedeutet Code und Tests im Repository; „live verifiziert" bedeutet in der
Lab-Laufzeit geprüft; „aktiviert" bedeutet Teilnahme am laufenden Eventfluss.

| Bereich | Status | Nachweis | Datum | Offene Grenze |
|---|---|---|---|---|
| Basisphasen 1–7 | Im Lab verifiziert | [README, Status](../README.md#status); `monitoring/`, `ops/chaos/`, [Runbook Strecke](runbook-link-degradation.md) | — | Lab, keine Produktion |
| Gate A / Phase 2B | Im Lab verifiziert | [Handoff Phase 2B / Gate A](handoff-phase-2b-gate-a.md); [ADR-006](decisions/006-transactional-outbox.md) | — | Outbox `pending`; Events deaktiviert |
| Consumer-Idempotenz / D1 | Auf site-cloud live verifiziert | [D3B2.1-Abschlussnachweis](handoff-d3b2.1-complete.md); [Idempotenz](idempotency.md) | 28.06.2026 | at-least-once, keine Exactly-once |
| Queue, DLQ, Monitoring / D2 | Auf site-cloud live verifiziert | [D3B2.1-Abschlussnachweis](handoff-d3b2.1-complete.md); [ADR-007](decisions/007-dlq-and-redrive.md) | 28.06.2026 | Queue-/DLQ-Tiefe wird vom Consumer abgefragt |
| Publisher-Kern / D3A | Implementiert; standardmäßig deaktiviert | `apps/publisher/`, Migration `0004`, [ADR-008](decisions/008-outbox-publisher.md) | — | Default `PUBLISHER_ENABLED=false`; Aktivierung kontrolliert unter D3B2.3 |
| Publisher-Wiring / D3B1 | Implementiert; standardmäßig deaktiviert | [Phase-3-Runtime-Upgrade-Runbook](runbook-phase-3-runtime-upgrade.md); `monitoring/prometheus/targets/publisher.json.example` | — | Service standardmäßig deaktiviert |
| Consumer-Rollout / D3B2.1 | Abgeschlossen und live verifiziert | [D3B2.1-Abschlussnachweis](handoff-d3b2.1-complete.md); [D3B2.1-Runbook](runbook-d3b2-consumer-rollout.md) | 28.06.2026 | nur site-cloud; kein Publisher |
| site-dc-Migration / D3B2.2 | Abgeschlossen und live verifiziert | [D3B2.2-Abschlussnachweis](evidence-d3b2.2.md); [Runtime-Upgrade-Runbook](runbook-phase-3-runtime-upgrade.md) | 30.06.2026 | Publisher deaktiviert; kein E2E-Eventfluss |
| Publisher-Aktivierung, E2E und Disable/Re-enable / D3B2.3 | Live verifiziert, Gesamtnachweis noch offen | [D3B2.3-Zwischennachweis](evidence-d3b2.3.md); [Roadmap](roadmap.md) | 30.06.2026 | Failure Injection, Redelivery, Duplikat, Validation/Poison, DLQ-Redrive offen |
| Phase 3 gesamt | In progress; noch nicht formal abgeschlossen | [README, Phase 3](../README.md); [Roadmap](roadmap.md) | — | Fehler-/Duplicate-/DLQ-Runtime-Nachweise und Gesamtcheck offen |
