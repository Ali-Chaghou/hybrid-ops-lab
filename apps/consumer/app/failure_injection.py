"""Kontrollierte Lab-Failure-Injection: nach Commit, vor Queue-Delete.

Kein echter Prozess-Crash — der Consumer-Prozess laeuft weiter, die Nachricht
wird nur nicht geloescht und nach Visibility-Timeout redelivered. One-shot pro
Prozesslaufzeit (ein Neustart re-armiert genau eine Injection). Ausschliesslich
fuer das Lab; nur ueber Env aktivierbar, kein HTTP-Endpunkt, keine Nutzereingabe.
"""
from __future__ import annotations

import os
import threading


class FailureInjector:
    """Feuert hoechstens einmal pro Instanz/Prozesslauf, nur wenn aktiviert."""

    def __init__(self, enabled: bool) -> None:
        self._enabled = bool(enabled)
        self._fired = False
        self._lock = threading.Lock()

    @classmethod
    def from_env(cls, env: dict | None = None) -> "FailureInjector":
        src = os.environ if env is None else env
        raw = src.get("LAB_FAIL_AFTER_COMMIT_ONCE", "")
        return cls(raw.strip().lower() in {"1", "true", "yes", "on"})

    @property
    def enabled(self) -> bool:
        return self._enabled

    def should_fail(self) -> bool:
        """True genau beim ERSTEN Aufruf, wenn aktiviert; danach immer False."""
        if not self._enabled:
            return False
        with self._lock:
            if self._fired:
                return False
            self._fired = True
            return True
