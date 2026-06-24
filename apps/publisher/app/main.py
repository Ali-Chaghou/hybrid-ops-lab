"""Publisher-Service: HTTP nur fuer Health/Metrics, Poll-Loop im Hintergrund.

Keine fachliche HTTP-API. Standardmaessig deaktiviert: dann startet kein Poller und
es wird keine DB-/Queue-Verbindung aufgebaut — der Prozess bleibt gesund und idle.
Bei aktiviertem Publisher ist der Startup fail closed (Pflichtwerte + Schema 0004).
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from app.metrics import PUBLISHER_ENABLED, PUBLISHER_LIVE, PUBLISHER_READY
from app.publisher import publisher

logging.basicConfig(level=logging.INFO)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    publisher.start()  # deaktiviert -> No-op (idle); aktiviert -> fail-closed Startup
    try:
        yield
    finally:
        publisher.stop()


app = FastAPI(title="outbox-publisher", lifespan=lifespan)


@app.get("/healthz")
def healthz() -> Response:
    return Response(status_code=200 if publisher.live else 503)


@app.get("/readyz")
def readyz() -> Response:
    return Response(status_code=200 if publisher.ready else 503)


@app.get("/metrics")
def metrics() -> Response:
    PUBLISHER_ENABLED.set(1 if publisher.enabled else 0)
    PUBLISHER_LIVE.set(1 if publisher.live else 0)
    PUBLISHER_READY.set(1 if publisher.ready else 0)
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
