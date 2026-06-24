"""Gate D3B1: statische Wiring-/Config-Pruefungen (kein laufendes System).

Compose-Service, Secret-Isolation, Route-Trennung, Prometheus-Scrape, Alerts,
.env.example/.gitignore und Unveraenderlichkeit von upgrade-site-dc.sh + Migrationen.
"""
from __future__ import annotations

import hashlib
import json
import pathlib

import pytest

yaml = pytest.importorskip("yaml")

REPO = pathlib.Path(__file__).resolve().parents[3]
DC_COMPOSE = REPO / "sites/dc/docker-compose.yml"
DC_ENV_EXAMPLE = REPO / "sites/dc/.env.example"
PROM = REPO / "monitoring/prometheus/prometheus.yml"
ALERTS = REPO / "monitoring/prometheus/rules/alerts.yml"
TARGET_EXAMPLE = REPO / "monitoring/prometheus/targets/publisher.json.example"
GITIGNORE = REPO / ".gitignore"
MAKEFILE = REPO / "Makefile"


def _read(p: pathlib.Path) -> str:
    return p.read_text(encoding="utf-8")


def _sha(p: pathlib.Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


# --- Compose-Service --------------------------------------------------------

def _publisher_svc():
    return yaml.safe_load(_read(DC_COMPOSE))["services"]["publisher"]


def test_publisher_service_exists_own_image_context():
    svc = _publisher_svc()
    assert svc["image"] == "hol-publisher:dev"
    assert svc["build"]["context"] == "../../apps/publisher"
    assert svc["build"]["dockerfile"] == "Dockerfile"


def test_publisher_enabled_hardcoded_false_no_substitution():
    raw = _read(DC_COMPOSE)
    assert 'PUBLISHER_ENABLED: "false"' in raw
    assert "PUBLISHER_ENABLED: ${" not in raw  # keine Env-Substitution
    # nirgends im gesamten Compose ein aktivierender Wert
    assert "PUBLISHER_ENABLED: \"true\"" not in raw


def test_publisher_host_port_and_healthcheck():
    svc = _publisher_svc()
    assert svc["ports"] == ["${PUBLISHER_HOST_PORT:-8001}:8000"]
    assert "/healthz" in " ".join(svc["healthcheck"]["test"])


def test_publisher_depends_on_db_and_migrate():
    dep = _publisher_svc()["depends_on"]
    assert dep["db"]["condition"] == "service_healthy"
    assert dep["inventory-migrate"]["condition"] == "service_completed_successfully"


def test_publisher_no_privileged_volumes_socket_hostnet():
    svc = _publisher_svc()
    assert "volumes" not in svc
    assert svc.get("privileged") in (None, False)
    assert "network_mode" not in svc
    raw = json.dumps(svc)
    assert "/var/run/docker.sock" not in raw


def test_publisher_uses_own_route_vars_not_inventory_sqs():
    env = _publisher_svc()["environment"]
    assert "${PUBLISHER_SQS_ENDPOINT_URL" in env["SQS_ENDPOINT_URL"]
    assert "${PUBLISHER_SQS_QUEUE_URL" in env["SQS_QUEUE_URL"]
    # Inventory-Runtime bekommt die Publisher-Route NICHT.
    inv = yaml.safe_load(_read(DC_COMPOSE))["services"]["inventory"]["environment"]
    assert "PUBLISHER_SQS" not in json.dumps(inv)


# --- Secret-Isolation -------------------------------------------------------

def test_publisher_password_only_to_bootstrap_and_publisher():
    svcs = yaml.safe_load(_read(DC_COMPOSE))["services"]
    def has_pub_pw(name):
        return "INVENTORY_PUBLISHER_PASSWORD" in json.dumps(svcs[name].get("environment", {}))
    assert has_pub_pw("db-bootstrap")
    assert has_pub_pw("publisher")
    for other in ("db", "db-prepare", "inventory-migrate", "inventory", "node_exporter"):
        assert not has_pub_pw(other), other


def test_env_example_publisher_placeholder_empty():
    raw = _read(DC_ENV_EXAMPLE)
    assert "INVENTORY_PUBLISHER_PASSWORD=" in raw
    # Platzhalter leer (keine echten Werte) + Route-Vars vorhanden.
    for line in raw.splitlines():
        if line.startswith("INVENTORY_PUBLISHER_PASSWORD="):
            assert line.strip() == "INVENTORY_PUBLISHER_PASSWORD="
    assert "PUBLISHER_SQS_ENDPOINT_URL=" in raw
    assert "PUBLISHER_HOST_PORT=8001" in raw


# --- Prometheus / Alerts ----------------------------------------------------

def test_prometheus_has_publisher_filesd_job():
    cfg = yaml.safe_load(_read(PROM))
    job = next(s for s in cfg["scrape_configs"] if s["job_name"] == "publisher")
    assert job["file_sd_configs"][0]["files"] == ["/etc/prometheus/targets/publisher.json"]


def test_publisher_target_example_valid_and_no_real_ip():
    data = json.loads(_read(TARGET_EXAMPLE))
    assert isinstance(data, list) and "targets" in data[0]
    assert "PUBLISHER_METRICS_HOST" in _read(TARGET_EXAMPLE)


def test_publisher_target_json_is_gitignored():
    gi = _read(GITIGNORE)
    assert "monitoring/prometheus/targets/*.json" in gi


def test_publisher_alerts_present_and_enabled_gated():
    groups = {g["name"]: g for g in yaml.safe_load(_read(ALERTS))["groups"]}
    assert "publisher" in groups
    rules = {r["alert"]: r for r in groups["publisher"]["rules"]}
    assert "PublisherDown" in rules
    # PublisherDown nutzt up (funktioniert ohne Prozess-Metrik).
    assert 'up{job="publisher"} == 0' in rules["PublisherDown"]["expr"]
    # Fachliche Alerts sind enabled-gegatet.
    for name in ("PublisherEnabledNotReady", "PublisherPollErrors", "PublisherPublishErrors",
                 "PublisherBacklogStuck", "PublisherOldestPendingAge", "PublisherStaleClaims"):
        assert "publisher_enabled == 1" in rules[name]["expr"], name
    # Stale-Claims-Dauer > 60s-Lease.
    assert rules["PublisherStaleClaims"]["for"] in ("3m", "5m")
    # for-Dauer ueberall vorhanden.
    for r in groups["publisher"]["rules"]:
        assert "for" in r


def test_publisher_alerts_reference_only_existing_metrics():
    # Importiert die Publisher-Metriken aus ihrer echten Datei (eigener Modulname).
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "publisher_metrics_under_test", REPO / "apps/publisher/app/metrics.py")
    mod = importlib.util.module_from_spec(spec)
    # metrics.py importiert nur prometheus_client -> laedt standalone.
    spec.loader.exec_module(mod)
    from prometheus_client import REGISTRY  # noqa
    known = set()
    for fam in mod.__dict__.values():
        name = getattr(fam, "_name", None)
        if name:
            known.add(name)
            known.add(name + "_total")
    known |= {"up"}
    import re
    groups = yaml.safe_load(_read(ALERTS))["groups"]
    pub = next(g for g in groups if g["name"] == "publisher")
    referenced = set()
    for r in pub["rules"]:
        for tok in re.findall(r"\b(up|publisher_[a-z0-9_]+)\b", r["expr"]):
            referenced.add(tok)
    assert referenced
    assert referenced <= known, sorted(referenced - known)


# --- Makefile-Guards (statisch) ---------------------------------------------

def test_makefile_targets_and_state_guard():
    mk = _read(MAKEFILE)
    for tgt in ("cloud-up:", "phase3-upgrade:", "render-publisher-target:"):
        assert tgt in mk
    # up validiert den State vor site-dc-Compose.
    assert "check-phase-3-runtime-state.py" in mk
    # kein Make-Ziel aktiviert den Publisher.
    assert "PUBLISHER_ENABLED=true" not in mk and "PUBLISHER_ENABLED:=true" not in mk
    # cloud-up beruehrt site-dc nicht.
    cloud_block = mk.split("cloud-up:", 1)[1].split("\nphase3-upgrade:", 1)[0]
    assert "sites/dc" not in cloud_block


# --- Finding A: synthetische ElasticMQ-Credentials NUR am Publisher ---------

def test_publisher_has_synthetic_emulator_credentials():
    env = _publisher_svc()["environment"]
    assert env["AWS_ACCESS_KEY_ID"] == "test"
    assert env["AWS_SECRET_ACCESS_KEY"] == "test"
    assert env["AWS_EC2_METADATA_DISABLED"] == "true"


def test_emulator_credentials_only_on_publisher():
    svcs = yaml.safe_load(_read(DC_COMPOSE))["services"]
    for other in ("db", "db-bootstrap", "db-prepare", "inventory-migrate", "inventory", "node_exporter"):
        blob = json.dumps(svcs[other].get("environment", {}))
        assert "AWS_ACCESS_KEY_ID" not in blob, other
        assert "AWS_SECRET_ACCESS_KEY" not in blob, other


def test_emulator_credentials_are_not_real_secrets():
    # Nur die oeffentlichen Lab-Platzhalter, keine AKIA-/lange Zufallswerte.
    env = _publisher_svc()["environment"]
    for key in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
        assert env[key] == "test"
    # publisher bleibt fest disabled.
    assert env["PUBLISHER_ENABLED"] == "false"


# --- Finding B: Publisher-Target-Lifecycle (kein Voralarm) ------------------

def _mk_block(name: str) -> str:
    """Rezeptzeilen eines Make-Ziels (bis zum naechsten nicht-eingerueckten Block)."""
    mk = _read(MAKEFILE)
    lines = mk.splitlines()
    out, capturing = [], False
    for ln in lines:
        if ln.startswith(name + ":"):
            capturing = True
            out.append(ln)
            continue
        if capturing:
            if ln and not ln[0].isspace() and not ln.startswith("\t"):
                break
            out.append(ln)
    return "\n".join(out)


def test_rsync_excludes_publisher_target():
    mk = _read(MAKEFILE)
    assert "--exclude 'monitoring/prometheus/targets/publisher.json'" in mk


def test_sync_does_not_render_or_install_target():
    block = _mk_block("sync")
    # Keine Target-Abhaengigkeit/-Erzeugung im generischen Code-Sync.
    assert "render-publisher-target" not in block
    assert "install-publisher-target" not in block


def test_cloud_up_does_not_install_target():
    block = _mk_block("cloud-up")
    assert "install-publisher-target" not in block
    assert "render-publisher-target" not in block


def test_phase3_upgrade_installs_target_only_after_successful_upgrade():
    block = _mk_block("phase3-upgrade")
    i_run = block.index("upgrade-phase-3-runtime.sh run")
    i_check = block.index("check-phase-3-runtime-state.py")
    i_install = block.index("install-publisher-target")
    # Reihenfolge: Upgrade -> State-Check -> Target-Installation.
    assert i_run < i_check < i_install


def test_up_installs_target_only_after_state_check():
    block = _mk_block("up")
    i_check = block.index("check-phase-3-runtime-state.py")
    i_install = block.index("install-publisher-target")
    assert i_check < i_install


def test_install_target_renders_and_syncs_only_the_file_to_cloud():
    block = _mk_block("install-publisher-target")
    assert "render-publisher-target" in block  # erzeugt lokal atomar
    assert "monitoring/prometheus/targets/publisher.json" in block
    assert "$(CLOUD)" in block and "$(DC)" not in block  # NUR nach site-cloud


# --- Unveraenderlichkeit (byte-identisch) -----------------------------------

_BASELINE = {
    "ops/deploy/upgrade-site-dc.sh":
        "b635516852d92cd720a16ea2f9f36fffc21ba03fb7dfda8d41fea8b1d47823fc",
    "apps/inventory/migrations/0001_create_stock_movements.sql":
        "b8b6e6fd15a7ced67d46573ed054257ea404e5a3c9ce27034bd10e486fb53ebc",
    "apps/inventory/migrations/0002_add_stable_event_id.sql":
        "dda71afa2f43f27b78eb1a358add65e27d1c39b2e36a9ed82d6ceb6b07974ef6",
    "apps/inventory/migrations/0003_create_event_outbox.sql":
        "1ed19f3924b43aa93f22435ffc65825d6329ff277f55137cd40fa62abbbd6402",
    "apps/inventory/migrations/0004_add_outbox_claim_fields.sql":
        "ddacf15f877aada3486d5b5b53c31ee3f2fd326fb4904396c4f7a7b126489d93",
}


@pytest.mark.parametrize("rel,sha", list(_BASELINE.items()))
def test_protected_files_unchanged(rel, sha):
    assert _sha(REPO / rel) == sha, f"{rel} wurde veraendert (in D3B1 verboten)"
