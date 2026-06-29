# Dokumentation

[Übersicht](../README.md) · [Dokumentation](README.md) · [Status & Roadmap](roadmap.md) · [Nachweise](evidence-index.md) · [Security](../SECURITY.md)

Technische Dokumentation, Entscheidungen, Runbooks und Laufzeitnachweise für
hybrid-ops-lab. Synthetisches Lab, keine Produktionsumgebung.

## Architektur und Funktionsweise

- [Architektur in 60 Sekunden (README)](../README.md#architektur-in-60-sekunden)
- [Das Problem in einfachen Worten (README)](../README.md#das-problem-in-einfachen-worten)
- [Consumer-Idempotenz](idempotency.md) — at-least-once-Verarbeitung über die `event_id`.

## Status und Roadmap

- [Projektstatus (README)](../README.md#status)
- [Roadmap](roadmap.md)

## Technische Nachweise

- [Nachweis-Übersicht](evidence-index.md)
- [D3B2.1 — Rollout-Nachweis](handoff-d3b2.1-complete.md)
- [Phase 2B / Gate A — Nachweis](handoff-phase-2b-gate-a.md)

## Entscheidungen

- [ADR-001 — Lokal statt echtes AWS](decisions/001-warum-lokal-statt-aws.md)
- [ADR-002 — Zwei VMs statt einer](decisions/002-zwei-vms-statt-einer.md)
- [ADR-003 — Toxiproxy als Strecke](decisions/003-toxiproxy-als-strecke.md)
- [ADR-004 — Proxmox-Provisionierung](decisions/004-proxmox-provisionierung.md)
- [ADR-005 — ElasticMQ statt LocalStack](decisions/005-elasticmq-statt-localstack.md)
- [ADR-006 — Transactional Outbox](decisions/006-transactional-outbox.md)
- [ADR-007 — DLQ und native Redrive-Policy](decisions/007-dlq-and-redrive.md)
- [ADR-008 — Separater Outbox-Publisher (Lease/Fencing, deaktiviert)](decisions/008-outbox-publisher.md)

## Runbooks

- [Phase 2B — site-dc-Upgrade (Outbox)](runbook-phase-2b-upgrade-site-dc.md)
- [D3B2.1 — Consumer-/D1-/D2-Rollout (site-cloud)](runbook-d3b2-consumer-rollout.md)
- [Phase-3-Runtime-Upgrade (site-dc, Publisher deaktiviert)](runbook-phase-3-runtime-upgrade.md)

## Betrieb und Fehleranalyse

- [Runbook Strecken-Degradation](runbook-link-degradation.md) — Toxiproxy-Incident und
  Diagnose; Chaos-Skripte in `../ops/chaos/`.

## Security

- [SECURITY.md](../SECURITY.md)
