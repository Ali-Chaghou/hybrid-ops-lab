# D3B2.1 — Technischer Rollout-Nachweis

[Übersicht](../README.md) · [Dokumentation](README.md) · [Status & Roadmap](roadmap.md) · [Nachweise](evidence-index.md) · [Security](../SECURITY.md)

Datum: 28. Juni 2026
Umgebung: synthetisches Lab (`site-cloud`), keine Produktionsumgebung.

Dieses Dokument enthält die Laufzeitergebnisse, die während des kontrollierten
D3B2.1-Rollouts erfasst wurden. Es nennt keine echten Adressen, Hosts oder Secrets.

## 1. Scope

Gate D3B2.1 bringt die idempotente Consumer-Runtime (D1) sowie Queue/DLQ/Redrive und
Consumer-/Queue-Monitoring (D2) site-cloud-isoliert in die Lab-Laufzeit. Orchestriert
durch `ops/deploy/upgrade-consumer-runtime.sh` (`make cloud-up`). Kein site-dc-Rollout,
keine Publisher-Aktivierung, keine vollständige Phase-3-Aktivierung.

## 2. Release-Identität

| Feld | Wert |
|---|---|
| Gate | D3B2.1 |
| Release-SHA | `5d319cad54e5a26dd59baacc1c269780ee90b1e4` |
| Runtime-Image-Tag | `inventory-consumer:5d319cad54e5` |
| laufende Pod-Image-ID (CRI/containerd) | `sha256:1d344543b6d1adc387b29d862b38abbbafe1159c8c6393ade14527cc3ae0795d` |

## 3. Rollout-Zusammenfassung

Der kontrollierte Fresh Run (`make cloud-up`, release-gebunden, site-cloud-isoliert)
durchlief die Phasenkette
`preflight → images-built → queue-config-ready → consumer-db-ready →
consumer-schema-ready → consumer-deployed → monitoring-ready → verified → complete`
und endete im Zustand `complete`. Der release-gebundene State-Checker
(`ops/deploy/check-d3b2-consumer-state.py`) bestätigte den Abschluss gegen den erwarteten
Release-SHA. Es gab kein Queue-Purge und keine Datenlöschung.

## 4. Ergebnisse

Die folgenden Ergebnisse wurden nach dem erfolgreichen Fresh Run erfasst.

| Bereich | Geprüfter Zustand | Ergebnis |
|---|---|---|
| Controller-State | gate=D3B2.1, step=complete, complete=true, release_sha + runtime_image_tag gesetzt; release-gebundener State-Checker | erfolgreich |
| Kubernetes Deployment | Ready=1, Available=1, Spec-Image = `inventory-consumer:5d319cad54e5` | ok |
| Kubernetes Pod | Phase=Running, Ready=true, Restarts=0, nicht in Löschung | ok |
| CRI/containerd-Identität | Release-Tag `inventory-consumer:5d319cad54e5` löst im containerd-Store exakt auf die laufende Pod-Image-ID `sha256:1d34…0795d` auf | identisch |
| Prometheus Readiness | `/-/ready` → HTTP 200 | ok |
| Consumer-Target | genau ein Consumer-Target vorhanden, health=up | ok |
| Publisher-Target | kein Publisher-Target aktiv | erwartet |
| Rule-Gruppen | Gruppe `consumer` geladen, Gruppe `queue` geladen | ok |
| Queue `inventory-movements` | visible=0, inflight=0 | leer |
| DLQ `inventory-movements-dlq` | visible=0, inflight=0 | leer |
| Rollout-Prozess und Lock | kein aktiver Rollout-Prozess, Rollout-Lock frei | ok |
| Vorgänger-State | alter unvollständiger Release-State von `2bbbf0c41465e6f86e9cf9aa89c085d5df6d10dd` archiviert; SHA256-Prüfsummen für `state.json`, `rollback.json`, `metadata.txt` gültig; der neue Rollout besitzt eigenen aktiven `state.json` und `rollback.json` | ok |

## 5. Nicht durchgeführte Änderungen

- Kein site-dc-Rollout (site-dc wurde durch D3B2.1 nicht verändert).
- Keine Publisher-Aktivierung; kein Publisher-Target aktiv.
- Keine vollständige Phase-3-Aktivierung (kein Ende-zu-Ende-Event-Fluss).
- Kein Queue-Purge.
- Keine Datenlöschung.

## 6. Hinweis zur Image-Anzeige

Das Kubernetes-Feld `status.image` kann einen anderen vorhandenen Repo-Tag derselben
containerd-Image-ID anzeigen. Eine zusätzlich sichtbare ältere Repo-Tag-Bezeichnung ist
nur ein Alias derselben containerd-Image-ID und kein alter laufender Runtime-Stand.
Maßgeblich für die laufende Identität ist die vollständige CRI/containerd-Image-ID
`sha256:1d344543b6d1adc387b29d862b38abbbafe1159c8c6393ade14527cc3ae0795d`, auf die der
Release-Tag exakt auflöst.

## 7. Nächste Schritte

- D3B2.2 — site-dc-Migration `0004`, Publisher bleibt deaktiviert. Ausstehend.
- D3B2.3 — Publisher-Aktivierung und End-to-End-Nachweis. Ausstehend.

Phase 3 als vollständiger Event-Flow (`event_outbox → Publisher → Queue → Consumer`) ist
damit noch nicht vollständig aktiviert. Vollständiger Fahrplan: [roadmap.md](roadmap.md).

D1 und D2 laufen nach diesem Rollout auf `site-cloud` live; der Zustand wurde am
28. Juni 2026 erfasst. Dies ist keine Produktionsumgebung und keine Aktivierung des
Publishers oder des Event-Versands.
