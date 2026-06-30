from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
CONTROLLER = REPO_ROOT / "ops/deploy/activate-phase-3-runtime.sh"
BASE_COMPOSE = REPO_ROOT / "sites/dc/docker-compose.yml"
ENABLE_COMPOSE = REPO_ROOT / "sites/dc/docker-compose.publisher-enabled.yml"
CROSS_SITE = REPO_ROOT / "ops/deploy/check-d3b2.3-cross-site.sh"
MAKEFILE = REPO_ROOT / "Makefile"


def test_enable_override_is_explicit_and_minimal() -> None:
    assert ENABLE_COMPOSE.read_text() == (
        "services:\n"
        "  publisher:\n"
        "    environment:\n"
        '      PUBLISHER_ENABLED: "true"\n'
    )
    assert 'PUBLISHER_ENABLED: "false"' in BASE_COMPOSE.read_text()


def test_activation_controller_contains_fail_closed_gates() -> None:
    text = CONTROLLER.read_text()

    required = (
        'D3B23_ACK_ACTIVATE:-',
        'D3B23_EXPECTED_PENDING:-',
        "verify_expected_backlog",
        "verify_queue_route",
        "emergency_disable",
        "wait_verified enabled",
        "write_state enabled true",
    )
    for item in required:
        assert item in text


def test_activation_controller_has_no_forbidden_mutations() -> None:
    text = CONTROLLER.read_text().lower()

    forbidden = (
        "send_message",
        "purge_queue",
        "delete_queue",
        "insert into event_outbox",
        "update event_outbox",
        "delete from event_outbox",
        "docker compose down",
        "rm -rf",
    )
    for item in forbidden:
        assert item not in text


def test_cross_site_preflight_is_read_only_and_fail_closed() -> None:
    text = CROSS_SITE.read_text()

    required = (
        "D3B23_EXPECTED_PENDING",
        "D3B23_EXPECTED_CONSUMER_RELEASE_SHA",
        "check-d3b2-consumer-state.py",
        "check-phase-3-runtime-state.py",
        "check-queue-empty.py",
        "select-consumer-pod.py",
        "PublisherStaleClaims",
        "site-cloud-read-only=ok",
        "site-dc-read-only=ok",
        'main "$@"',
    )
    for item in required:
        assert item in text

    forbidden = (
        "send_message",
        "purge_queue",
        "delete_queue",
        "docker compose down",
        "docker compose up",
        "kubectl apply",
        "kubectl delete",
        "insert into event_outbox",
        "update event_outbox",
        "delete from event_outbox",
    )
    lowered = text.lower()
    for item in forbidden:
        assert item not in lowered


def test_makefile_exposes_controlled_activation_targets() -> None:
    text = MAKEFILE.read_text()

    required = (
        "D3B23_EXPECTED_PENDING ?=",
        "D3B23_EXPECTED_CONSUMER_RELEASE_SHA ?=",
        "D3B23_ACK_ACTIVATE ?= 0",
        ".NOTPARALLEL: phase3-activation-preflight phase3-activation-enable",
        "phase3-activation-preflight: check-d3b2-release _validate-d3b23 sync",
        "phase3-activation-enable: _validate-d3b23-enable phase3-activation-preflight",
        "D3B23_ACK_ACTIVATE=1 ./ops/deploy/activate-phase-3-runtime.sh enable",
        "phase3-activation-disable: check-env",
        "./ops/deploy/activate-phase-3-runtime.sh disable",
        "phase3-activation-state: check-env",
    )
    for item in required:
        assert item in text
