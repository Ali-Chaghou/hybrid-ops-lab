"""Gate D2: statische Konfigurationspruefungen fuer DLQ/Redrive und Monitoring.

Keine Live-Infra: prueft ElasticMQ-/Terraform-/Prometheus-/Alert-Konfiguration als
Text bzw. via YAML/JSON-Parser. Stellt u. a. sicher, dass Alert-Regeln nur
tatsaechlich exponierte Consumer-Metriken referenzieren.
"""
from __future__ import annotations

import json
import os
import pathlib
import re

import pytest

os.environ.setdefault("DATABASE_URL", "host=/nonexistent user=x dbname=x")
os.environ.setdefault("SQS_QUEUE_URL", "http://localhost:9324/q")

yaml = pytest.importorskip("yaml")

REPO = pathlib.Path(__file__).resolve().parents[3]
ELASTICMQ = REPO / "sites/cloud/elasticmq.conf"
MOD = REPO / "infra/modules/event-queue"
ENV = REPO / "infra/environments/cloud"
PROM = REPO / "monitoring/prometheus/prometheus.yml"
ALERTS = REPO / "monitoring/prometheus/rules/alerts.yml"
TARGET_EXAMPLE = REPO / "monitoring/prometheus/targets/consumer.json.example"
MON_COMPOSE = REPO / "monitoring/docker-compose.yml"
CLUSTER_SCRIPT = REPO / "ops/bootstrap/create-site-cloud-cluster.sh"
DEPLOY_SCRIPT = REPO / "ops/deploy/deploy-consumer.sh"
ADR = REPO / "docs/decisions/007-dlq-and-redrive.md"


def _read(p: pathlib.Path) -> str:
    return p.read_text(encoding="utf-8")


# --- ElasticMQ ---------------------------------------------------------------

def test_elasticmq_has_dlq_and_redrive():
    conf = _read(ELASTICMQ)
    assert '"inventory-movements-dlq"' in conf
    assert "deadLettersQueue" in conf
    assert re.search(r"maxReceiveCount\s*=\s*5", conf)
    # Klammer-Balance grob pruefen (HOCON).
    assert conf.count("{") == conf.count("}")


# --- Terraform-Modul ---------------------------------------------------------

def test_terraform_main_queue_has_redrive_policy():
    main = _read(MOD / "main.tf")
    assert "redrive_policy" in main
    assert "deadLetterTargetArn" in main
    assert "maxReceiveCount" in main
    assert 'resource "aws_sqs_queue" "dlq"' in main
    assert "${var.queue_name}-dlq" in main
    # SSE auf beiden Queues beibehalten.
    assert main.count("sqs_managed_sse_enabled") >= 2


def test_terraform_dlq_outputs_present():
    out = _read(MOD / "outputs.tf")
    assert "dlq_url" in out and "dlq_arn" in out
    env_out = _read(ENV / "outputs.tf")
    assert "dlq_url" in env_out and "dlq_arn" in env_out


def test_terraform_new_vars_have_defaults_module_users_not_broken():
    var = _read(MOD / "variables.tf")
    # max_receive_count + DLQ-Retention muessen Defaults haben (sonst brechen Aufrufer).
    assert re.search(r'variable "max_receive_count"', var)
    assert re.search(r"default\s*=\s*5", var)
    assert re.search(r'variable "dlq_message_retention_seconds"', var)
    # Bestehender Aufrufer (environments/cloud) setzt weiterhin nur queue_name.
    env_main = _read(ENV / "main.tf")
    assert "queue_name" in env_main
    assert "max_receive_count" not in env_main  # nicht erzwungen


def test_max_receive_count_is_bounded():
    var = _read(MOD / "variables.tf")
    assert "validation" in var
    assert ">= 2" in var and "<= 100" in var


# --- Prometheus-Scrape -------------------------------------------------------

def test_prometheus_has_consumer_scrape_job():
    cfg = yaml.safe_load(_read(PROM))
    jobs = {s["job_name"]: s for s in cfg["scrape_configs"]}
    assert "consumer" in jobs
    files = jobs["consumer"]["file_sd_configs"][0]["files"]
    assert any("consumer.json" in f for f in files)


def test_consumer_target_example_is_valid_json():
    data = json.loads(_read(TARGET_EXAMPLE))
    assert isinstance(data, list) and data and "targets" in data[0]
    # Keine echte IP fest codiert (Platzhalter).
    assert "CONSUMER_METRICS_HOST" in _read(TARGET_EXAMPLE)


# --- Alerts referenzieren reale Metriken ------------------------------------

def _known_metric_names() -> set[str]:
    # Importiert die Consumer-Metriken und sammelt die exponierten Sample-Namen.
    import app.main  # noqa: F401 (registriert alle Metriken im Default-REGISTRY)
    from prometheus_client import REGISTRY

    names: set[str] = set()
    for metric in REGISTRY.collect():
        # Family-Name auch dann erfassen, wenn ein gelabelter Counter noch KEINE
        # Sample-Children hat (sonst order-abhaengig).
        names.add(metric.name)
        if metric.type == "counter":
            names.add(metric.name + "_total")
        for sample in metric.samples:
            names.add(sample.name)
    # Prometheus-synthetische / Blackbox-Metriken, die nicht der Consumer erzeugt.
    names |= {"up", "probe_duration_seconds", "probe_success"}
    return names


def test_alert_rules_reference_existing_metrics():
    known = _known_metric_names()
    rules_doc = yaml.safe_load(_read(ALERTS))
    referenced: set[str] = set()
    for group in rules_doc["groups"]:
        for rule in group["rules"]:
            expr = rule.get("expr", "")
            for tok in re.findall(r"\b(up|consumer_[a-z0-9_]+|probe_[a-z_]+)\b", expr):
                referenced.add(tok)
    assert referenced, "keine Metriken in Alerts gefunden"
    unknown = referenced - known
    assert not unknown, f"Alerts referenzieren unbekannte Metriken: {sorted(unknown)}"


def test_new_consumer_queue_alerts_present():
    names = {
        r["alert"]
        for g in yaml.safe_load(_read(ALERTS))["groups"]
        for r in g["rules"]
    }
    assert {"ConsumerDown", "ConsumerNotReady", "DLQNotEmpty", "MainQueueBacklog"} <= names


def test_alert_for_durations_are_present_and_sane():
    for g in yaml.safe_load(_read(ALERTS))["groups"]:
        for r in g["rules"]:
            assert "for" in r, f"Alert {r['alert']} ohne for-Dauer"


def test_backlog_threshold_is_lab_appropriate():
    rule = next(
        r
        for g in yaml.safe_load(_read(ALERTS))["groups"]
        for r in g["rules"]
        if r["alert"] == "MainQueueBacklog"
    )
    m = re.search(r"consumer_queue_depth_approximate\s*>\s*(\d+)", rule["expr"])
    assert m, "Backlog-Schwelle nicht gefunden"
    assert int(m.group(1)) <= 50, "Schwelle fuer ein kleines Lab zu hoch"
    assert rule["for"] in ("5m", "10m", "15m")


# --- Reachability: k3d-Portabbildung + Prometheus-Mounts --------------------

def test_cluster_script_publishes_metrics_nodeport():
    s = _read(CLUSTER_SCRIPT)
    assert "k3d cluster create" in s
    assert re.search(r"--port\s+\"?\$\{METRICS_NODEPORT[^\"]*\}:\$\{METRICS_NODEPORT[^\"]*\}@server:0\"?", s) \
        or "30090:30090@server:0" in s


def test_deploy_checks_nodeport_and_writes_target_atomically():
    s = _read(DEPLOY_SCRIPT)
    assert "docker port" in s and "FEHLER: NodePort" in s   # fail closed
    assert "mktemp" in s and "mv -f" in s                    # atomarer Write
    assert "consumer.json" in s
    assert "host.docker.internal:%s" in s or "host.docker.internal:${METRICS_NODEPORT}" in s


def test_prometheus_mounts_targets_readonly_and_path_matches():
    compose = yaml.safe_load(_read(MON_COMPOSE))
    vols = compose["services"]["prometheus"]["volumes"]
    assert any(v.endswith("/etc/prometheus/targets:ro") for v in vols), "targets nicht ro gemountet"
    assert any(v.endswith("/etc/prometheus/rules:ro") for v in vols)
    assert any("prometheus.yml:/etc/prometheus/prometheus.yml:ro" in v for v in vols)
    # file_sd-Pfad liegt unter dem Mount und zeigt auf die AKTIVE Datei (nicht .example).
    cfg = yaml.safe_load(_read(PROM))
    consumer_job = next(s for s in cfg["scrape_configs"] if s["job_name"] == "consumer")
    files = consumer_job["file_sd_configs"][0]["files"]
    assert files == ["/etc/prometheus/targets/consumer.json"]
    assert not any("example" in f for f in files)


def test_prometheus_can_resolve_host_docker_internal():
    compose = yaml.safe_load(_read(MON_COMPOSE))
    extra = compose["services"]["prometheus"].get("extra_hosts", [])
    assert any("host.docker.internal:host-gateway" in e for e in extra)


def test_adr_documents_queue_metrics_depend_on_consumer():
    adr = _read(ADR)
    assert "ConsumerDown" in adr
    assert "Stirbt der Consumer" in adr or "haengt am lebenden Consumer" in adr


# --- keine Secrets in den geaenderten Config-Dateien ------------------------

def test_no_obvious_secrets_in_configs():
    pat = re.compile(r"(AKIA|ASIA)[0-9A-Z]{16}|BEGIN [A-Z ]*PRIVATE KEY")
    for p in (ELASTICMQ, MOD / "main.tf", PROM, ALERTS, TARGET_EXAMPLE):
        assert not pat.search(_read(p))
