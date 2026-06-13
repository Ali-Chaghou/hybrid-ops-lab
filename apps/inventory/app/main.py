"""inventory-Service (site-dc).

Klassische REST-App: nimmt Lagerbewegungen an, persistiert sie in Postgres
und publiziert (ab Phase 3) ein Event an die Queue.
"""
from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel, Field

from app import db
from app.events import publish_movement
from app.metrics import MOVEMENTS_CREATED, REQUEST_DURATION

logging.basicConfig(level=logging.INFO)


class MovementIn(BaseModel):
    sku: str = Field(min_length=1, max_length=64)
    quantity: int = Field(ge=1)
    warehouse: str = Field(min_length=1, max_length=64)


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.pool.open()
    db.ensure_schema()
    yield
    db.pool.close()


app = FastAPI(title="inventory", lifespan=lifespan)


@app.middleware("http")
async def measure_requests(request, call_next):
    start = time.perf_counter()
    response = await call_next(request)
    REQUEST_DURATION.labels(
        method=request.method,
        path=request.url.path,
        status=str(response.status_code),
    ).observe(time.perf_counter() - start)
    return response


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.get("/readyz")
def readyz():
    if db.check_db():
        return {"status": "ready"}
    return JSONResponse(status_code=503, content={"status": "not ready"})


@app.post("/movements", status_code=201)
def create_movement(movement: MovementIn):
    record = db.insert_movement(movement.sku, movement.quantity, movement.warehouse)
    MOVEMENTS_CREATED.inc()
    publish_movement(record)
    return record


@app.get("/movements")
def get_movements(limit: int = 20):
    limit = max(1, min(limit, 100))
    return db.list_movements(limit)


@app.get("/metrics")
def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
