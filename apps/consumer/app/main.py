"""Consumer-Service: HTTP fuer Health/Metrics, SQS-Poll im Hintergrund.

Startup ist fail closed: ohne erreichbare DB UND gueltiges Schema startet der
Consumer nicht (verify_schema wirft). Erst danach beginnt der Poll-Loop.
Readiness spiegelt zusaetzlich die laufende DB-Erreichbarkeit wider.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from app.consumer import consumer

logging.basicConfig(level=logging.INFO)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Fail closed: bei fehlendem/falschem Schema oder nicht erreichbarer DB wirft
    # verify_schema -> der Prozess startet kontrolliert nicht.
    consumer.verify_schema()
    consumer.start()
    try:
        yield
    finally:
        # Cleanup auch bei Exceptions waehrend des Lifecycles garantieren.
        consumer.stop()


app = FastAPI(title="inventory-consumer", lifespan=lifespan)


@app.get("/healthz")
def healthz() -> Response:
    # Liveness an den Poller-Thread gekoppelt: toter/nicht gestarteter Poller -> 503,
    # damit die k8s-Liveness-Probe einen haengenden/gestorbenen Loop neu startet.
    return Response(status_code=200 if consumer.healthy else 503)


@app.get("/readyz")
def readyz() -> Response:
    return Response(status_code=200 if consumer.ready else 503)


@app.get("/metrics")
def metrics() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
