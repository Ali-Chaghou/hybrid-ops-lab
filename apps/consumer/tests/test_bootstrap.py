"""Rollen-Bootstrap: idempotent, Least-Privilege, sicherer Abbruch, keine Secrets."""
from __future__ import annotations

import psycopg
import pytest
from ops.db import bootstrap


def _attrs(conn, role):
    return conn.execute(
        "SELECT rolsuper, rolcreatedb, rolcreaterole, rolbypassrls, rolcanlogin "
        "FROM pg_roles WHERE rolname=%s", (role,)
    ).fetchone()


def test_first_bootstrap_creates_roles(pg_server):
    # Rollen existieren bereits aus der Session-Fixture -> ensure_roles ist idempotent.
    created = bootstrap.ensure_roles(pg_server["maint"])
    assert created == []  # schon vorhanden (Session-Fixture nutzt dasselbe Tool)
    with psycopg.connect(pg_server["maint"]) as c:
        for role in bootstrap.ALL_ROLES:
            assert _attrs(c, role) is not None


def test_runtime_roles_are_least_privilege(pg_server):
    bootstrap.ensure_roles(pg_server["maint"])
    with psycopg.connect(pg_server["maint"]) as c:
        for role in bootstrap.ALL_ROLES:
            rolsuper, rolcreatedb, rolcreaterole, rolbypassrls, rolcanlogin = _attrs(c, role)
            assert rolsuper is False
            assert rolcreatedb is False
            assert rolcreaterole is False
            assert rolbypassrls is False
            assert rolcanlogin is True


def test_roles_have_no_replication(pg_server):
    # NOREPLICATION wird gegen die ECHTE Datenbank verifiziert (pg_roles.rolreplication),
    # nicht per Source-/Text-Assertion. Deckt u. a. die D3A-Publisher-Rolle ab.
    expected = {"inventory_admin", "inventory_app", "inventory_publisher",
                "consumer_admin", "consumer_app"}
    assert expected <= set(bootstrap.ALL_ROLES)  # alle geforderten Rollen werden verwaltet
    bootstrap.ensure_roles(pg_server["maint"])
    with psycopg.connect(pg_server["maint"]) as c:
        for role in bootstrap.ALL_ROLES:
            rolreplication = c.execute(
                "SELECT rolreplication FROM pg_roles WHERE rolname=%s", (role,)
            ).fetchone()[0]
            assert rolreplication is False, role


def test_idempotent_second_run(pg_server):
    bootstrap.ensure_roles(pg_server["maint"])
    bootstrap.ensure_roles(pg_server["maint"])  # zweiter Lauf darf nicht scheitern
    with psycopg.connect(pg_server["maint"]) as c:
        assert _attrs(c, "consumer_app") is not None


def test_password_is_set_but_not_echoed(pg_server, capsys):
    secret = "s3cr3t-not-in-output"
    bootstrap.ensure_roles(pg_server["maint"], {"consumer_app": secret})
    out = capsys.readouterr()
    assert secret not in out.out and secret not in out.err
    # Passwort gesetzt? (md5/scram-Hash vorhanden, nie Klartext)
    with psycopg.connect(pg_server["maint"]) as c:
        h = c.execute("SELECT rolpassword FROM pg_authid WHERE rolname='consumer_app'").fetchone()[0]
    assert h is not None and secret not in h


def test_missing_env_aborts(monkeypatch, capsys):
    monkeypatch.delenv("PG_ADMIN_DSN", raising=False)
    rc = bootstrap._main([])
    assert rc == 2  # sicherer Abbruch
    err = capsys.readouterr().err
    assert "PG_ADMIN_DSN" in err


def test_empty_password_rejected(pg_server):
    with pytest.raises(ValueError):
        bootstrap.ensure_roles(pg_server["maint"], {"consumer_app": ""})


def test_placeholder_password_rejected(pg_server):
    with pytest.raises(ValueError):
        bootstrap.ensure_roles(pg_server["maint"], {"consumer_app": "change-me"})


def test_env_empty_password_aborts(monkeypatch):
    # gesetzte, aber leere CONSUMER_APP_PASSWORD -> Validierung lehnt ab.
    pw = bootstrap._passwords_from_env({"CONSUMER_APP_PASSWORD": ""})
    assert pw == {"consumer_app": ""}
    with pytest.raises(ValueError):
        bootstrap._validate_password("consumer_app", "")
