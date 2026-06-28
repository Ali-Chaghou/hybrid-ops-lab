# Runbook — D3B2.1 Consumer-/D1-/D2-Rollout (site-cloud)

Kontrollierter, **site-cloud-isolierter** Rollout der idempotenten Consumer-Runtime
(Gate D1) sowie Queue/DLQ/Redrive + Consumer-Monitoring (Gate D2). Orchestriert durch
`ops/deploy/upgrade-consumer-runtime.sh`. **Berührt weder site-dc noch den Publisher;
aktiviert weder Phase 3 noch Events.**

> Synthetische, isolierte Lab-Umgebung. Keine echten Adressen/Secrets in diesem Dokument.

## D3B2.1-Ziel
D1 (Inbox/Projection-Idempotenz) und D2 (DLQ, native Redrive, Consumer-/Queue-Monitoring)
**live** bereitstellen und read-only verifizieren — als Voraussetzung für das spätere,
**getrennte** D3B2.2 (site-dc-Migration `0004` + disabled Publisher). D3B2.1 ändert
**nichts** an site-dc.

## Voraussetzungen
- `make.env` (gitignored) mit `DC_HOST`/`CLOUD_HOST`, **Modus 600** (Guard
  `ops/deploy/check-local-perms.sh`; kein Gruppen-/World-Read).
- `sites/cloud/.env` (gitignored, **600**) mit `CONSUMER_DB` + `CONSUMER_APP_PASSWORD`.
- k3d-Cluster `site-cloud` mit auf den Host veröffentlichtem Consumer-NodePort `30090`.
- ElasticMQ + Toxiproxy erreichbar.

## Queue-Leerheitsgate
ElasticMQ ist im Lab **kein** dauerhafter Produktionsbroker. Eine Neuerstellung des
`sqs`-Service (um `elasticmq.conf` mit DLQ/Redrive zu laden) erfolgt **nur**, wenn
`ops/deploy/check-queue-empty.py` nachweist, dass **jede** existierende Queue
sichtbar=0 **und** in-flight=0 ist. Ausschließlich `ListQueues`/`GetQueueAttributes`;
bei Nachrichten **oder** nicht sicher parsebaren Werten → **fail closed Abbruch**.
**Keine** Receive-/Delete-/Send-/Purge-/Redrive-Operation; **keine** Queue-URLs im Log.

## Ungeklärte Restarts & Acknowledgement
Der Controller liest die aktuelle Consumer-Pod-Restartzahl. Bei **> 0** stoppt er
**fail closed** und verlangt ein bewusstes Operator-Acknowledgement über
`D3B2_ACK_CONSUMER_RESTARTS=1` (kein Secret). Das Makefile hat Default `?= 0`, akzeptiert
nur `0`/`1` und **übergibt den Wert explizit über ssh** (lokale Env wird sonst nicht
weitergereicht). Das Acknowledgement ist **an genau die beobachtete Restartzahl
gebunden** (Audit `restart-ack.json`): eine **höhere** Zahl als die zuletzt bestätigte
verlangt **erneut** ein Acknowledgement (kein Blanket-„alle künftigen Restarts ok"). Die
bereits bestätigte Zahl darf ohne erneutes Ack fortgesetzt werden. Gespeichert werden nur
Restartzahl + Zeitpunkt — keine Hosts/Pod-Namen/Secrets.

## Image-Build vor Migration
Der Controller baut **explizit** das aktuelle `hol-consumer:dev` aus dem Repo
(`docker compose build consumer-db-bootstrap`) und dokumentiert die Image-ID
(gekürzt, ohne Registry/Hosts), **bevor** Bootstrap/Prepare/Migrate laufen. **Kein**
Vertrauen auf ein bereits vorhandenes Image (`docker compose up -d` allein genügt nicht).

## DB-Kette (deterministisch)
`consumer-db` starten → **healthy abwarten** → `consumer-db-bootstrap` (Exit 0) →
`consumer-db-prepare` (Exit 0) → `consumer-migrate` (Exit 0). Danach **read-only**
verifizieren: erwartete Migration vorhanden, keine unerwarteten Migrationen,
`event_inbox` + `movement_projection` vorhanden, Idempotenz-Constraints,
`consumer_admin`/`consumer_app` Least Privilege. **Keine** DSN/Passwörter im Log.

## Release-Integritätsgate (vor jedem Sync)
`make cloud-up`/`cloud-resume` führen **vor** `sync-cloud` und jedem `ssh`/`rsync` den
lokalen Guard `ops/deploy/check-d3b2-local-release.sh` aus: Branch ist exakt `main`,
Worktree sauber (inkl. untracked, exkl. gitignore), `HEAD` = 40 hex, `HEAD == origin/main`,
und der read-only `git ls-remote origin refs/heads/main` liefert genau einen SHA == `HEAD`
(optional == erwarteter SHA). Bei jeder Abweichung **fail closed, kein Sync, kein Remote-
Aufruf** — so beschreibt der übergebene `D3B2_RELEASE_SHA` garantiert die gesyncten
Dateien. Der Guard ändert nichts (kein fetch/pull/reset/checkout) und gibt keine Remote-URL aus.

## Immutable Runtime-Image & Release-Bindung
Der Desktop ermittelt den vollständigen lokalen Commit und übergibt ihn explizit als
`D3B2_RELEASE_SHA` (genau 40 hex; `.git` wird nicht gesynct). Der Controller validiert
ihn und leitet den **immutablen** Runtime-Tag `inventory-consumer:<12hex>` ab.
`deploy-consumer.sh` rendert genau diesen Tag in das Manifest (Platzhalter
`__CONSUMER_IMAGE__`, injection-sicher) — **kein** Lauf mehr auf dem mutablen `:dev`-Tag.
Nach dem Deploy muss das **Pod-Spec-Image exakt** dem Release-Tag entsprechen (sonst
fail closed). Der State speichert `release_sha` + `runtime_image_tag`; **`resume`
akzeptiert nur denselben Release** (Mismatch/fehlender SHA → fail closed). Das
Setup-Image `hol-consumer:dev` bleibt separat für Bootstrap/Migration.

## Runtime-Werkzeuge im k3d-Node (crictl/ctr, kein `k3s`-Wrapper)
Der k3d-Server-Node nutzt die **eigenständigen** Binärdateien `crictl` und `ctr`
(`/bin/crictl`, `/bin/ctr`). **`k3s crictl` / `k3s ctr` werden NICHT angenommen** —
sie liefern in diesem k3d-Node keine verwertbare Ausgabe. Der Controller erkennt die
Tools fail-closed über eine **feste, im Code definierte Kandidatenliste** (per Muster
validierte absolute Pfade), prüft Node-Erreichbarkeit, Ausführbarkeit, eine harmlose
`crictl inspecti --help`-Probe, `ctr -n k8s.io images ls -q` und die Verfügbarkeit des
Tagging-Subkommandos via `ctr -n k8s.io images tag --help` (Exit 0, **erzeugt keinen
Tag**; das echte Tagging erfolgt erst in der Rollback-Sicherung). Fehlt/funktioniert ein
Werkzeug nicht → **fail closed**
(**keine** automatische Installation, **kein** stiller Rückfall auf `k3s …`). Das
**Tool-Gate läuft im Preflight vor dem State-Schreiben** und beim `resume` vor jeder
Mutation (Queue-Neuerstellung, Build, Migration, Deployment).

## Consumer-Rollout & deterministischer Rollback (CRI/containerd als Source of Truth)
**Wichtig:** Docker- und containerd-Image-IDs sind **keine vergleichbare
Identitätsdomäne** — selbst bei inhaltsgleichem Image unterscheiden sie sich. Der
Docker-Daemon dient daher **ausschließlich** dem Bauen und k3d-Import des **neuen**
Release-Images; der Identitätsnachweis des **laufenden** alten Pod-Images läuft
**ausschließlich über CRI/containerd** (`crictl`/`ctr`) im k3d-Server-Node.

`deploy-consumer.sh` (Build Runtime-Image mit Release-Tag, k3d-Import, Secret via
0600-Tempdatei, Manifest-Render, Rollout) — **erst nach** erfolgreicher DB-/Schema-Prüfung.
**Vor** der Mutation erfasst der Controller deterministisch: Revision, Spec-Image,
laufende Pod-Image-ID; er bestätigt die laufende Identität über
`crictl inspecti` und prüft, ob die **vollständige** Pod-`sha256:<64>`-Digest exakt
zu `status.id` **oder** einem `status.repoDigests`-Eintrag gehört (kein Präfix-/
Kurzvergleich, **keine Docker-`.Id`**). Das bestätigte Image wird **direkt im
containerd-Namespace `k8s.io`** unter einem eindeutigen Rollback-Tag
(`…:rollback-<12hex des Runtime-Digests>`) per `ctr -n k8s.io images tag` gesichert
(**kein `docker tag`, kein `k3d image import`** für das Legacy-Image) und über CRI erneut
verifiziert. Existiert der Rollback-Tag bereits, wird nur bei **identischer** CRI-Identität
weitergefahren, sonst **fail closed** (kein stilles Überschreiben). Fehlt das Image oder
ist die Identität widersprüchlich/leer/verkürzt → **fail closed vor jeder Mutation**.

Bei Rollout-Fehler wird das Deployment **explizit per `kubectl set image` auf den
containerd-Rollback-Tag** gesetzt, der Rollout abgewartet, Health/Ready geprüft und die
neue laufende Pod-Digest **über CRI gegen den gespeicherten vollen Runtime-Digest**
verifiziert — nur dann gilt der Rollback als erfolgreich. **`kubectl rollout undo` bleibt
ausdrücklich unzureichend und wird nicht verwendet.** Beim **neuen** Release wird zudem
verifiziert, dass der Release-Tag im CRI-Store auflösbar ist **und** die laufende
Pod-Digest exakt zu seiner CRI-Identität gehört (nicht nur das Spec-Image stimmt). **Keine**
Löschung von DB/Queue, **kein** Purge; State zurück auf den letzten guten Schritt, Resume möglich.

## Monitoring-Reload (bounded, read-only, fail closed)
Der Consumer erzeugt `consumer.json` atomar. `phase_monitoring_ready` führt dann **in
fester Reihenfolge** aus:
1. `consumer.json` vorhanden;
2. Monitoring-Stack starten (`mon up -d`);
3. Prometheus **genau einmal** force-recreaten (`up -d --force-recreate --no-deps
   prometheus`) — **außerhalb** und **vor** der Warteschleife;
4. **bounded read-only Waiter** (`_verify_monitoring_ready`);
5. `monitoring-ready` **nur** nach bewiesenem Erfolg.

**Warum der Waiter:** Docker meldet „Started", **bevor** Prometheus' HTTP-API,
Target-Discovery und Rule-Loading bereit sind — direkt nach dem Recreate liefert die
API kurzzeitig **0/0 Targets**. Eine **einzelne** sofortige Verifikation schlägt dann
fälschlich fehl. Der Waiter prüft daher wiederholt (read-only, **keine** Mutation in der
Schleife), bis **alle** Bedingungen erfüllt sind oder die Deadline greift:

- `/-/ready` liefert exakt HTTP 200;
- Targets-/Rules-API: **Body und HTTP-Status stammen aus derselben Anfrage**
  (`curl -w '\n%{http_code}'`, keine Temp-Datei); **nur HTTP 200** wird geparst —
  ein Nicht-2xx-Status kann **niemals** über strukturell passend aussehendes JSON
  erfolgreich werden;
- der Parser verlangt zusätzlich Top-Level-Objekt **und** `status == "success"`
  (fehlender/anderer Status → niemals Erfolg), dann erst die `data`-Struktur;
- ≥ 1 aktives Target mit `labels.job == "consumer"`, **alle** Consumer-Targets `health == "up"`;
- **kein** aktives Target mit `labels.job == "publisher"` → **Policy-Verletzung, sofortiger
  Abbruch** (nicht als Startup-Delay retryt);
- Rules-API: Rule-Gruppen `consumer` **und** `queue` geladen.

**Transiente** Startupzustände werden innerhalb der gemeinsamen Schranke erneut geprüft:
Connection-refused/`/-/ready`≠200, **Nicht-2xx auf Targets/Rules**, 0/0 Targets,
Consumer-Target noch nicht `up`, Rules noch nicht geladen, leere/unvollständige Antworten,
`status != "success"`. Der **Parser-Exit-Code** wird ausgewertet (kein `|| true`): Timeout
(124/137) endet kontrolliert über die Deadline; ein Tool-Fehler (Parser nicht ausführbar,
125/126/127) ist **sofort fatal** und wird **nicht** als „malformed" bis zum Limit retryt.

**Zeitbudget:** genau **eine** monotone Gesamtdeadline pro Waiter-Aufruf (kein frisches
Budget je Endpoint/Versuch). Jeder `curl` und jeder JSON-Parser läuft über den zentralen
`_dl`-Wrapper (`timeout --kill-after`); Sleeps sind auf die Restzeit gedeckelt; nach
Ablauf **fail closed**. Konfiguration (im **Preflight** vor State/Mutation validiert,
konservative Obergrenzen):
- `D3B2_MONITORING_VERIFY_ATTEMPTS` (Default 60, 1…1000)
- `D3B2_MONITORING_VERIFY_INTERVAL` (Default 2, 0…60)
- `D3B2_MONITORING_VERIFY_BUDGET_SECONDS` (Default 90, 1…600)

Diese Konfiguration **sowie** `timeout --kill-after`, `curl`, `python3` und der
Pod-Selektor werden über **eine gemeinsame Voraussetzungsfunktion**
(`_validate_verify_prerequisites`) geprüft — sowohl im `preflight` als auch in
`resume` **vor** `restart_gate`/`_run_from` und damit vor jeder State-Schreib- oder
Mutationsoperation. Ein Resume ab `consumer-deployed` führt also **kein** `mon up -d`
und **keinen** Force-Recreate aus, wenn eine Voraussetzung (z. B. ungültige Monitoring-
Konfiguration oder fehlendes `--kill-after`) fehlschlägt (fail closed).

**Diagnose** ist secret-frei: nur Reason-Token, Versuch/Gesamt, Budget, elapsed/remaining,
Abbruchgrund — **niemals** Roh-JSON, vollständige API-Antworten, URLs mit Credentials,
Env-Inhalte oder Hosts. `phase_verified` nutzt **denselben** Waiter read-only, **ohne**
erneuten Force-Recreate. `docker compose up -d` ohne nachgewiesene Readiness genügt **nicht**.

## Verifikation (read-only)
- **D1:** Consumer healthy/ready, DB erreichbar, Inbox+Projection vorhanden,
  Idempotenz-Metriken, Least-Privilege-Rechte, keine ungeklärten Processing-Fehler.
- **D2:** Main Queue + DLQ vorhanden, Redrive `maxReceiveCount=5`, Queue-/DLQ-Metriken,
  Receive-/Redelivery-/Validation-/Integrity-/DB-/Delete-Fehlermetriken, Consumer-Target
  `up`, Rules geladen, **DLQ leer**. **Keine** fachliche Testnachricht.

## State & Resume
State: `sites/cloud/.d3b2-consumer/state.json` (gitignored), atomar (Tempdatei + `mv`),
strukturell validiert via `ops/deploy/check-d3b2-consumer-state.py`. Phasen:
`preflight → images-built → queue-config-ready → consumer-db-ready →
consumer-schema-ready → consumer-deployed → monitoring-ready → verified → complete`.
`flock` serialisiert; beschädigter State → **fail closed**. Bei **Monitoring-Fehler nach
erfolgreichem Consumer** bleibt der State bei `consumer-deployed` (kein automatischer
Consumer-Rollback) und der Monitoring-Schritt ist **separat resumierbar** (`make cloud-resume`).

## Rollback
- **Vor Consumer-Deploy:** idempotent wiederholbar; Queue/DLQ + DB bleiben erhalten.
- **Fehlgeschlagener Consumer-Rollout:** Undo auf erfasste Revision; vorheriges Image
  muss verfügbar sein (sonst vorab fail closed); DB-Migration + DLQ/Redrive bleiben.
- **Monitoring-Fehler:** Consumer nicht zurückrollen; Monitoring separat resumieren.

## Stop-Gates
Queue nicht leer / nicht parsebar; ungeklärte Restarts ohne Ack; unsichere `.env`-Rechte;
fehlende NodePort-Veröffentlichung; Schema-Verifikation fehlgeschlagen; Consumer nicht
ready; Prometheus-Reload nicht verifizierbar.

## Abgrenzung
- **Kein site-dc**, **kein Publisher**, **kein Publisher-Target**, **keine** `PUBLISHER_ENABLED`/
  `EVENTS_ENABLED`-Änderung, **keine** Migration `0004`.
- **Keine** direkte Queue-Testnachricht außerhalb des regulären Consumer-Pfads.
- **D3B2.2** (site-dc-Migration + disabled Publisher) bleibt ein **getrenntes** späteres Gate.
- Keine Exactly-once-Garantie (at-least-once + Consumer-Idempotenz).
