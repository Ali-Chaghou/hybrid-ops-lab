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

.DEFAULT_GOAL := help
.PHONY: help check-env render-publisher-target install-publisher-target sync cloud-up phase3-upgrade up down check demo-incident demo-restore

help: ## Diese Hilfe anzeigen
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}'

check-env:
	@test -n "$(DC_HOST)" || { echo "DC_HOST nicht gesetzt - make.env aus make.env.example anlegen."; exit 1; }
	@test -n "$(CLOUD_HOST)" || { echo "CLOUD_HOST nicht gesetzt - make.env aus make.env.example anlegen."; exit 1; }

render-publisher-target: check-env ## Prometheus-Publisher-Target (file_sd) NUR lokal erzeugen
	PUBLISHER_METRICS_HOST="$(DC_HOST)" PUBLISHER_HOST_PORT="$(PUBLISHER_HOST_PORT)" \
		./ops/deploy/render-publisher-target.sh

install-publisher-target: render-publisher-target ## Target lokal erzeugen + NUR die Datei nach site-cloud syncen
	$(RSYNC) ./monitoring/prometheus/targets/publisher.json \
		$(CLOUD):$(REMOTE_DIR)/monitoring/prometheus/targets/publisher.json

sync: check-env ## NUR Repository-Code auf beide VMs rsyncen (KEIN Publisher-Target)
	$(RSYNC) ./ $(DC):$(REMOTE_DIR)/
	$(RSYNC) ./ $(CLOUD):$(REMOTE_DIR)/

cloud-up: sync ## NUR site-cloud + Monitoring + Consumer (kein site-dc, KEIN Publisher-Target)
	ssh $(CLOUD) 'cd $(REMOTE_DIR)/sites/cloud && docker compose up -d'
	ssh $(CLOUD) 'cd $(REMOTE_DIR)/monitoring && docker compose up -d'
	ssh $(CLOUD) 'cd $(REMOTE_DIR) && ./ops/deploy/deploy-consumer.sh'

phase3-upgrade: sync ## Kontrolliertes site-dc Phase-3-Upgrade; Target ERST nach Erfolg installieren
	ssh $(DC) 'cd $(REMOTE_DIR) && ./ops/deploy/upgrade-phase-3-runtime.sh run'
	@ssh $(DC) 'cd $(REMOTE_DIR) && python3 ops/deploy/check-phase-3-runtime-state.py $(PHASE3_STATE)' \
		|| { echo "ABBRUCH: kein Publisher-Target installiert — Phase-3-State nicht complete."; exit 1; }
	$(MAKE) --no-print-directory install-publisher-target

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
