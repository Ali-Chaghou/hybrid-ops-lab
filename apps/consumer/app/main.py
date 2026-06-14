"""Consumer-Service: HTTP fuer Health/Metrics, SQS-Poll im Hintergrund."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from app.consumer import consumer

logging.basicConfig(level=logging.INFO)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    consumer.start()
    yield
    consumer.stop()


app = FastAPI(title="inventory-consumer", lifespan=lifespan)


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


@app.get("/readyz")
def readyz() -> Response:
    return Response(status_code=200 if consumer.healthy else 503)


@app.get("/metrics")
def metrics() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
