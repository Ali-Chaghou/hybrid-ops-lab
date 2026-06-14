# Desktop-orchestriertes Makefile fuer das hybrid-ops-lab.
# Der Desktop hat kein Docker/sudo; alle Container-/k8s-Schritte laufen per ssh
# auf den VMs. Host-Adressen kommen aus make.env (gitignored; Vorlage:
# make.env.example). rsync laeuft vom Desktop, docker/kubectl auf den VMs.
# Bewusst KEIN rsync --delete: die VMs halten gitignorte .env/targets-Dateien,
# die es auf dem Desktop nicht gibt.

-include make.env

SSH_USER ?= ops
REMOTE_DIR := ~/hybrid-ops-lab
RSYNC := rsync -a --exclude '.git' --exclude '.venv' --exclude 'infra'

DC := $(SSH_USER)@$(DC_HOST)
CLOUD := $(SSH_USER)@$(CLOUD_HOST)

.DEFAULT_GOAL := help
.PHONY: help check-env sync up down check demo-incident demo-restore

help: ## Diese Hilfe anzeigen
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-15s\033[0m %s\n", $$1, $$2}'

check-env:
	@test -n "$(DC_HOST)" || { echo "DC_HOST nicht gesetzt - make.env aus make.env.example anlegen."; exit 1; }
	@test -n "$(CLOUD_HOST)" || { echo "CLOUD_HOST nicht gesetzt - make.env aus make.env.example anlegen."; exit 1; }

sync: check-env ## Code auf beide VMs rsyncen (ohne infra/, .git, .venv)
	$(RSYNC) ./ $(DC):$(REMOTE_DIR)/
	$(RSYNC) ./ $(CLOUD):$(REMOTE_DIR)/

up: sync ## Beide Sites + Monitoring hochfahren und Consumer deployen
	ssh $(DC) 'cd $(REMOTE_DIR)/sites/dc && docker compose up -d --build'
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
