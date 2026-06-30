# Desktop-orchestriertes Makefile fuer das hybrid-ops-lab.
# Der Desktop hat kein Docker/sudo; alle Container-/k8s-Schritte laufen per ssh
# auf den VMs. Host-Adressen kommen aus make.env (gitignored; Vorlage:
# make.env.example). rsync laeuft vom Desktop, docker/kubectl auf den VMs.
# Bewusst KEIN rsync --delete: die VMs halten gitignorte .env/targets-Dateien,
# die es auf dem Desktop nicht gibt.

-include make.env

SSH_USER ?= ops
REMOTE_DIR := ~/hybrid-ops-lab
# Das Publisher-file_sd-Target wird NIE durch den generischen Code-Sync verteilt
# (sonst entstuende vor dem disabled-Deployment ein falscher PublisherDown-Alarm).
# Es wird ausschliesslich nach erfolgreichem Phase-3-Upgrade gezielt installiert.
RSYNC := rsync -a --exclude '.git' --exclude '.venv' --exclude 'infra' \
	--exclude 'monitoring/prometheus/targets/publisher.json'

DC := $(SSH_USER)@$(DC_HOST)
CLOUD := $(SSH_USER)@$(CLOUD_HOST)

PUBLISHER_HOST_PORT ?= 8001
PHASE3_STATE := sites/dc/.phase3-runtime/state.json

D3B2_STATE := sites/cloud/.d3b2-consumer/state.json
# Release-Bindung: vollstaendiger lokaler Commit (kein Remote-.git nach rsync).
D3B2_RELEASE_SHA := $(shell git -C . rev-parse HEAD 2>/dev/null)
# Bewusstes Restart-Acknowledgement: Default 0, nur 0/1 zulaessig (s. _validate-ack).
D3B2_ACK_CONSUMER_RESTARTS ?= 0

# D3B2.3: bewusste Publisher-Aktivierung.
D3B23_EXPECTED_PENDING ?=
D3B23_EXPECTED_CONSUMER_RELEASE_SHA ?=
D3B23_ACK_ACTIVATE ?= 0

.DEFAULT_GOAL := help

# Sicherheitsreihenfolge auch bei versehentlichem make -j:
# Release-/Input-Gates -> Sync -> Preflight -> Enable.
.NOTPARALLEL: phase3-activation-preflight phase3-activation-enable
.PHONY: help check-env _validate-ack _validate-d3b23 _validate-d3b23-enable check-d3b2-release render-publisher-target install-publisher-target sync sync-cloud sync-dc cloud-up cloud-resume cloud-state cloud-check phase3-upgrade phase3-activation-preflight phase3-activation-enable phase3-activation-disable phase3-activation-state up down check demo-incident demo-restore

help: ## Diese Hilfe anzeigen
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}'

check-env:
	@test -n "$(DC_HOST)" || { echo "DC_HOST nicht gesetzt - make.env aus make.env.example anlegen."; exit 1; }
	@test -n "$(CLOUD_HOST)" || { echo "CLOUD_HOST nicht gesetzt - make.env aus make.env.example anlegen."; exit 1; }
	@./ops/deploy/check-local-perms.sh make.env || { echo "make.env-Rechte unsicher - 'chmod 600 make.env'."; exit 1; }
	@test -n "$(D3B2_RELEASE_SHA)" || { echo "D3B2_RELEASE_SHA leer - kein git-HEAD ermittelbar."; exit 1; }

_validate-ack:
	@case "$(D3B2_ACK_CONSUMER_RESTARTS)" in 0|1) : ;; *) echo "D3B2_ACK_CONSUMER_RESTARTS nur 0 oder 1 (war: $(D3B2_ACK_CONSUMER_RESTARTS))."; exit 1 ;; esac

_validate-d3b23:
	@printf "%s" "$(D3B23_EXPECTED_PENDING)" | grep -Eq "^[0-9]+$$" || { echo "D3B23_EXPECTED_PENDING muss als nichtnegative Zahl gesetzt sein."; exit 1; }
	@printf "%s" "$(D3B23_EXPECTED_CONSUMER_RELEASE_SHA)" | grep -Eq "^[0-9a-f]{40}$$" || { echo "D3B23_EXPECTED_CONSUMER_RELEASE_SHA muss 40 hex sein."; exit 1; }
	@case "$(D3B23_ACK_ACTIVATE)" in 0|1) : ;; *) echo "D3B23_ACK_ACTIVATE nur 0 oder 1 (war: $(D3B23_ACK_ACTIVATE))."; exit 1 ;; esac

_validate-d3b23-enable: _validate-d3b23
	@test "$(D3B23_ACK_ACTIVATE)" = "1" || { echo "Aktivierung nicht bestaetigt: D3B23_ACK_ACTIVATE=1 erforderlich."; exit 1; }

# Release-Integritaetsgate: Worktree == main == origin == remote refs/heads/main.
# MUSS vor jedem Sync/Remote-Aufruf des produktiven D3B2.1-Pfads laufen.
check-d3b2-release:
	@./ops/deploy/check-d3b2-local-release.sh "$(D3B2_RELEASE_SHA)"
	@printf '%s' "$(D3B2_RELEASE_SHA)" | grep -Eq '^[0-9a-f]{40}$$' || { echo "D3B2_RELEASE_SHA nicht 40 hex."; exit 1; }

render-publisher-target: check-env ## Prometheus-Publisher-Target (file_sd) NUR lokal erzeugen
	PUBLISHER_METRICS_HOST="$(DC_HOST)" PUBLISHER_HOST_PORT="$(PUBLISHER_HOST_PORT)" \
		./ops/deploy/render-publisher-target.sh

install-publisher-target: render-publisher-target ## Target lokal erzeugen + NUR die Datei nach site-cloud syncen
	$(RSYNC) ./monitoring/prometheus/targets/publisher.json \
		$(CLOUD):$(REMOTE_DIR)/monitoring/prometheus/targets/publisher.json

sync-cloud: check-env ## NUR Repository-Code nach site-cloud rsyncen (kein site-dc)
	$(RSYNC) ./ $(CLOUD):$(REMOTE_DIR)/

sync-dc: check-env ## NUR Repository-Code nach site-dc rsyncen (kein site-cloud)
	$(RSYNC) ./ $(DC):$(REMOTE_DIR)/

sync: sync-cloud sync-dc ## Beide Sites explizit rsyncen (NICHT von cloud-up genutzt)

cloud-up: check-d3b2-release sync-cloud _validate-ack ## D3B2.1: NUR site-cloud — Release-gated Consumer-/D1-/D2-Rollout (kein site-dc, kein Publisher)
	ssh $(CLOUD) 'cd $(REMOTE_DIR) && D3B2_RELEASE_SHA=$(D3B2_RELEASE_SHA) D3B2_ACK_CONSUMER_RESTARTS=$(D3B2_ACK_CONSUMER_RESTARTS) ./ops/deploy/upgrade-consumer-runtime.sh run'

cloud-resume: check-d3b2-release sync-cloud _validate-ack ## D3B2.1-Rollout fortsetzen — Release-gated (gleicher, sauberer main-Stand)
	ssh $(CLOUD) 'cd $(REMOTE_DIR) && D3B2_RELEASE_SHA=$(D3B2_RELEASE_SHA) D3B2_ACK_CONSUMER_RESTARTS=$(D3B2_ACK_CONSUMER_RESTARTS) ./ops/deploy/upgrade-consumer-runtime.sh resume'

cloud-state: check-env ## D3B2.1-Consumer-Rollout-State (read-only) anzeigen
	@ssh $(CLOUD) 'cd $(REMOTE_DIR) && ./ops/deploy/upgrade-consumer-runtime.sh state'

cloud-check: check-env ## Read-only: Prometheus-Targets + Consumer-Rollout-Status (release-gebunden)
	@ssh $(CLOUD) 'curl -s "http://localhost:9090/api/v1/targets?state=active"' \
		| python3 -c 'import sys,json; t=json.load(sys.stdin)["data"]["activeTargets"]; up=sum(x["health"]=="up" for x in t); print(f"Prometheus-Targets up: {up}/{len(t)}")'
	@ssh $(CLOUD) 'cd $(REMOTE_DIR) && python3 ops/deploy/check-d3b2-consumer-state.py $(D3B2_STATE) $(D3B2_RELEASE_SHA)' \
		&& echo "D3B2.1-State: complete (Release gebunden)" || echo "D3B2.1-State: nicht complete / Release-Mismatch"

phase3-upgrade: sync-dc ## Kontrolliertes site-dc Phase-3-Upgrade; Target ERST nach Erfolg installieren
	ssh $(DC) 'cd $(REMOTE_DIR) && ./ops/deploy/upgrade-phase-3-runtime.sh run'
	@ssh $(DC) 'cd $(REMOTE_DIR) && python3 ops/deploy/check-phase-3-runtime-state.py $(PHASE3_STATE)' \
		|| { echo "ABBRUCH: kein Publisher-Target installiert — Phase-3-State nicht complete."; exit 1; }
	$(MAKE) --no-print-directory install-publisher-target

phase3-activation-preflight: check-d3b2-release _validate-d3b23 sync ## D3B2.3: Cross-Site + site-dc Preflight, keine Aktivierung
	DC_HOST="$(DC_HOST)" CLOUD_HOST="$(CLOUD_HOST)" SSH_USER="$(SSH_USER)" \
	D3B23_EXPECTED_PENDING="$(D3B23_EXPECTED_PENDING)" \
	D3B23_EXPECTED_CONSUMER_RELEASE_SHA="$(D3B23_EXPECTED_CONSUMER_RELEASE_SHA)" \
		./ops/deploy/check-d3b2.3-cross-site.sh
	ssh $(DC) 'cd $(REMOTE_DIR) && D3B23_EXPECTED_PENDING=$(D3B23_EXPECTED_PENDING) ./ops/deploy/activate-phase-3-runtime.sh preflight'

phase3-activation-enable: _validate-d3b23-enable phase3-activation-preflight ## D3B2.3: Publisher bewusst aktivieren
	ssh $(DC) 'cd $(REMOTE_DIR) && D3B23_EXPECTED_PENDING=$(D3B23_EXPECTED_PENDING) D3B23_ACK_ACTIVATE=1 ./ops/deploy/activate-phase-3-runtime.sh enable'

phase3-activation-disable: check-env ## D3B2.3: Publisher jederzeit kontrolliert deaktivieren
	ssh $(DC) 'cd $(REMOTE_DIR) && ./ops/deploy/activate-phase-3-runtime.sh disable'

phase3-activation-state: check-env ## D3B2.3: Activation-State read-only anzeigen
	@ssh $(DC) 'cd $(REMOTE_DIR) && ./ops/deploy/activate-phase-3-runtime.sh state'

up: sync ## Stacks starten — site-dc + Publisher-Target NUR nach erfolgreichem Phase-3-State (fail closed)
	@ssh $(DC) 'cd $(REMOTE_DIR) && python3 ops/deploy/check-phase-3-runtime-state.py $(PHASE3_STATE)' \
		|| { echo "ABBRUCH: site-dc nicht gestartet, kein Publisher-Target — Phase-3-Runtime-State fehlt/ungueltig."; \
		     echo "Zuerst 'make phase3-upgrade' ausfuehren (siehe docs/runbook-phase-3-runtime-upgrade.md)."; exit 1; }
	ssh $(DC) 'cd $(REMOTE_DIR)/sites/dc && docker compose up -d'
	$(MAKE) --no-print-directory install-publisher-target
	ssh $(CLOUD) 'cd $(REMOTE_DIR)/sites/cloud && docker compose up -d'
	ssh $(CLOUD) 'cd $(REMOTE_DIR)/monitoring && docker compose up -d'
	ssh $(CLOUD) 'cd $(REMOTE_DIR) && ./ops/deploy/deploy-consumer.sh'

down: check-env ## Compose-Stacks beider Sites + Monitoring stoppen (k3d-Cluster bleibt)
	-ssh $(DC) 'cd $(REMOTE_DIR)/sites/dc && docker compose down'
	-ssh $(CLOUD) 'cd $(REMOTE_DIR)/monitoring && docker compose down'
	-ssh $(CLOUD) 'cd $(REMOTE_DIR)/sites/cloud && docker compose down'

check: check-env ## Prometheus-Targets und Consumer-Status pruefen
	@ssh $(CLOUD) 'curl -s "http://localhost:9090/api/v1/targets?state=active"' \
		| python3 -c 'import sys,json; t=json.load(sys.stdin)["data"]["activeTargets"]; up=sum(x["health"]=="up" for x in t); print(f"Prometheus-Targets up: {up}/{len(t)}"); [print("  down:", x["labels"]["job"], x.get("lastError","")) for x in t if x["health"]!="up"]'
	@ssh $(CLOUD) 'kubectl -n inventory rollout status deployment/inventory-consumer --timeout=10s'

demo-incident: check-env ## Strecken-Latenz einschalten (Toxiproxy)
	ssh $(CLOUD) 'cd $(REMOTE_DIR) && ./ops/chaos/degrade-link.sh'

demo-restore: check-env ## Strecken-Latenz aufheben
	ssh $(CLOUD) 'cd $(REMOTE_DIR) && ./ops/chaos/restore-link.sh'
