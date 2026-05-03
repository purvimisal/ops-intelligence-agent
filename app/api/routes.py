"""
FastAPI route handlers — all 7 endpoints under /api/v1.
"""

from __future__ import annotations

import time
from typing import Any

import structlog
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response, StreamingResponse
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)
from pydantic import BaseModel

from app.api.streaming import analyse_stream  # imported at module level so tests can patch it

logger = structlog.get_logger(__name__)

router = APIRouter()

# ---------------------------------------------------------------------------
# Prometheus metric definitions (module-level singletons)
# ---------------------------------------------------------------------------

RISK_SCORE_GAUGE = Gauge("risk_score_gauge", "Current risk score per service", ["service"])
DETECTION_LATENCY = Histogram(
    "detection_latency_seconds", "Detection pipeline latency in seconds"
)
INCIDENTS_TOTAL = Counter(
    "incidents_total", "Total incidents created", ["service", "severity"]
)
DLQ_LENGTH = Gauge("dlq_length", "Current number of items in the dead-letter queue")


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class AnalyseRequest(BaseModel):
    service: str
    scenario: str


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/health")
async def health(request: Request) -> dict[str, Any]:
    pool = getattr(request.app.state, "pool", None)
    redis_client = getattr(request.app.state, "redis", None)
    start_time = getattr(request.app.state, "start_time", time.monotonic())

    # DB check
    db_status = "unavailable"
    if pool is not None:
        try:
            async with pool.acquire() as conn:
                await conn.execute("SELECT 1")
            db_status = "ok"
        except Exception as exc:
            logger.warning("health_db_check_failed", error=str(exc))

    # Redis check
    redis_status = "unavailable"
    if redis_client is not None:
        redis_status = "ok" if await redis_client.ping() else "unavailable"

    return {
        "status": "ok",
        "db": db_status,
        "redis": redis_status,
        "llm": "available",
        "uptime_seconds": round(time.monotonic() - start_time, 1),
    }


@router.get("/risk")
async def list_risk(request: Request) -> list[dict[str, Any]]:
    pool = getattr(request.app.state, "pool", None)
    if pool is None:
        return []

    async with pool.acquire() as db:
        rows = await db.fetch(
            """
            SELECT DISTINCT ON (service)
                service, score, top_signal, time_to_incident_min, ts AS computed_at
            FROM risk_scores
            ORDER BY service, ts DESC
            """
        )

    results = []
    for r in rows:
        score = float(r["score"])
        RISK_SCORE_GAUGE.labels(service=r["service"]).set(score)
        results.append(
            {
                "service": r["service"],
                "score": round(score, 2),
                "top_signal": r["top_signal"],
                "time_to_incident_min": r["time_to_incident_min"],
                "computed_at": r["computed_at"].isoformat(),
            }
        )
    return results


@router.get("/risk/history/{service}")
async def risk_history(service: str, request: Request) -> list[dict[str, Any]]:
    pool = getattr(request.app.state, "pool", None)
    if pool is None:
        return []

    async with pool.acquire() as db:
        rows = await db.fetch(
            """
            SELECT score, ts AS computed_at
            FROM risk_scores
            WHERE service = $1
            ORDER BY ts DESC
            LIMIT 100
            """,
            service,
        )

    return [
        {"score": round(float(r["score"]), 2), "computed_at": r["computed_at"].isoformat()}
        for r in rows
    ]


@router.get("/incidents")
async def list_incidents(request: Request) -> list[dict[str, Any]]:
    pool = getattr(request.app.state, "pool", None)
    if pool is None:
        return []

    async with pool.acquire() as db:
        rows = await db.fetch(
            """
            SELECT id, service, severity, summary, created_at
            FROM incidents
            ORDER BY created_at DESC
            LIMIT 10
            """
        )

    return [
        {
            "id": r["id"],
            "service": r["service"],
            "severity": r["severity"],
            "summary": r["summary"],
            "created_at": r["created_at"].isoformat(),
        }
        for r in rows
    ]


@router.get("/incidents/{incident_id}")
async def get_incident(incident_id: int, request: Request) -> dict[str, Any]:
    pool = getattr(request.app.state, "pool", None)
    if pool is None:
        raise HTTPException(status_code=503, detail="Database unavailable")

    async with pool.acquire() as db:
        row = await db.fetchrow(
            """
            SELECT id, service, severity, summary, root_cause,
                   hypotheses, fix_steps, risk_score, created_at
            FROM incidents
            WHERE id = $1
            """,
            incident_id,
        )

    if row is None:
        raise HTTPException(status_code=404, detail=f"Incident {incident_id} not found")

    return {
        "id": row["id"],
        "service": row["service"],
        "severity": row["severity"],
        "summary": row["summary"],
        "root_cause": row["root_cause"],
        "hypotheses": row["hypotheses"],
        "fix_steps": row["fix_steps"],
        "risk_score": row["risk_score"],
        "created_at": row["created_at"].isoformat(),
    }


@router.post("/incidents/analyse")
async def analyse_incident(body: AnalyseRequest, request: Request) -> StreamingResponse:
    pool = getattr(request.app.state, "pool", None)

    from app.agent.orchestrator import OrchestratorAgent

    agent = OrchestratorAgent(db_pool=pool, langfuse=None)

    return StreamingResponse(
        analyse_stream(body.service, body.scenario, pool, agent),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/metrics")
async def prometheus_metrics(request: Request) -> Response:
    redis_client = getattr(request.app.state, "redis", None)
    if redis_client is not None:
        try:
            DLQ_LENGTH.set(await redis_client.dlq_length())
        except Exception:
            pass

    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)
