"""Per-Message-Handler: validieren -> atomar verarbeiten -> (Injection) -> Delete.

Phase 1 kapselt die Verarbeitung EINER Nachricht ohne die SQS-Poll-Schleife.
`delete_fn` ist ein Callback (in Tests ein Fake; Phase 2 verdrahtet den echten
SQS-Delete). Der Handler faengt Validierungs- und DB-Fehler ab und liefert ein
Outcome zurueck — er beendet die Consumer-Schleife nicht. Strukturierte Logs
enthalten event_id/movement_id, aber NIE Payload, Secrets oder Connection-Strings.
"""
from __future__ import annotations

import logging
from typing import Callable

import psycopg

from app import store
from app.envelope import Envelope, EnvelopeError, validate
from app.failure_injection import FailureInjector
from app.store import Outcome

log = logging.getLogger("consumer.handler")

# Outcome -> (Metrik-Methode am Recorder)
_OUTCOME_METRIC = {
    Outcome.APPLIED: "applied",
    Outcome.TRANSPORT_DUPLICATE: "transport_duplicate",
    Outcome.BUSINESS_DUPLICATE: "business_duplicate",
}
_INTEGRITY_KIND = {
    Outcome.EVENT_ID_CONFLICT: "event_id",
    Outcome.BUSINESS_CONFLICT: "movement",
}


class _NullMetrics:
    def __getattr__(self, _name):
        return lambda *a, **k: None


def handle_message(
    raw: bytes,
    conn: psycopg.Connection,
    delete_fn: Callable[[Envelope], None],
    injector: FailureInjector | None = None,
    metrics=None,
) -> Outcome:
    m = metrics if metrics is not None else _NullMetrics()
    injector = injector if injector is not None else FailureInjector(False)
    m.received()

    # 1. Strikte Validierung — keine Reparatur, keine Ersatz-ID, kein Delete.
    try:
        env = validate(raw)
    except EnvelopeError as exc:
        m.validation_failure(exc.reason)
        log.warning("event rejected: reason=%s", exc.reason)
        return Outcome.VALIDATION_ERROR

    # 2. Atomare Verarbeitung.
    try:
        outcome = store.process_event(conn, env)
    except psycopg.Error:
        m.database_failure()
        # Connection-String/Payload bewusst NICHT loggen.
        log.warning("db failure while processing event_id=%s", env.event_id)
        return Outcome.DB_FAILURE

    # 3. Metriken/Logs nach Ergebnis.
    if outcome in _OUTCOME_METRIC:
        getattr(m, _OUTCOME_METRIC[outcome])()
    elif outcome in _INTEGRITY_KIND:
        m.integrity_conflict(_INTEGRITY_KIND[outcome])
    log.info(
        "processed event_id=%s movement_id=%s outcome=%s",
        env.event_id, env.movement_id, outcome.value,
    )

    # 4. Loeschen nur bei erfolgreich abgeschlossener Verarbeitung.
    if store.should_delete(outcome):
        # Failure Injection ausschliesslich nach erstem ECHTEN Effekt (APPLIED).
        if outcome is Outcome.APPLIED and injector.should_fail():
            m.failure_injection()
            log.warning("lab failure injection after commit, before delete: event_id=%s", env.event_id)
            return Outcome.FAILURE_INJECTED
        try:
            delete_fn(env)
        except Exception:  # Delete-Fehler != fehlgeschlagene fachliche Transaktion
            m.delete_failure()
            log.warning("delete failed after commit (redelivery possible): event_id=%s", env.event_id)
            return Outcome.DELETE_FAILURE

    return outcome
