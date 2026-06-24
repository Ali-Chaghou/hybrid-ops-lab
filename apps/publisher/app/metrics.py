"""Prometheus-Metriken des Publishers. /metrics liefert die Exposition.

Bewusst niedrig-kardinal: KEINE Labels mit event_id/movement_id/message_id/
claim_owner/Queue-URL/Fehlertext.
"""
from __future__ import annotations

from prometheus_client import Counter, Gauge

# Zustands-Gauges (zur Scrape-Zeit aktualisiert).
PUBLISHER_ENABLED = Gauge("publisher_enabled", "1 wenn der Publisher aktiviert ist, sonst 0.")
PUBLISHER_LIVE = Gauge("publisher_live", "1 wenn der Prozess/Poller lebt, sonst 0.")
PUBLISHER_READY = Gauge("publisher_ready", "1 wenn bereit (deaktiviert ODER DB/Schema ok), sonst 0.")

# Backlog/Claim-Sicht (vom Publisher per SQL gemessen).
AVAILABLE_PENDING = Gauge(
    "publisher_available_pending",
    "Anzahl sofort publizierbarer Outbox-Zeilen (status=pending, available_at<=now).",
)
CLAIMED_ROWS = Gauge(
    "publisher_claimed_rows",
    "Anzahl aktuell geclaimter Outbox-Zeilen (claim_owner gesetzt).",
)
OLDEST_AVAILABLE_AGE = Gauge(
    "publisher_oldest_available_age_seconds",
    "Alter (s) der aeltesten sofort publizierbaren Outbox-Zeile (0 wenn keine).",
)
LAST_PUBLISH_TIMESTAMP = Gauge(
    "publisher_last_publish_timestamp_seconds",
    "Unix-Zeit des letzten erfolgreichen Publishs.",
)

# Counter.
POLLS_OK = Counter("publisher_polls_ok_total", "Erfolgreiche Poll-Zyklen.")
POLL_ERRORS = Counter("publisher_poll_errors_total", "Fehlgeschlagene Poll-/DB-Operationen.")
CLAIMED_TOTAL = Counter("publisher_claimed_total", "Insgesamt geclaimte Outbox-Zeilen.")
PUBLISH_SUCCESS = Counter("publisher_publish_success_total", "Erfolgreiche Queue-Publishes.")
PUBLISH_ERRORS = Counter("publisher_publish_errors_total", "Fehlgeschlagene Queue-Publishes.")
RETRIES = Counter("publisher_retries_total", "Als Retry zurueckgestellte Zeilen (Backoff).")
FINALIZE_CONFLICTS = Counter(
    "publisher_finalize_conflicts_total",
    "Finalisierungen, die 0 Zeilen trafen (stale Claim / Fencing).",
)
