"""
FastAPI application entry point.

Lifespan: DB pool → Redis → SlackAlerter → OrchestratorAgent → Scheduler
All startup failures are logged and tolerated — the API starts degraded rather
than crashing, so the detection layer remains reachable.
"""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from typing import Any

import structlog
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.alerts.scheduler import create_scheduler
from app.alerts.slack import SlackAlerter
from app.api.routes import router
from app.core.config import settings
from app.core.db import close_pool, get_pool, init_db
from app.core.redis_client import RedisClient

logger = structlog.get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.start_time = time.monotonic()

    # --- Database ---
    try:
        await init_db()
        app.state.pool = await get_pool()
        logger.info("api_db_ready")
    except Exception as exc:
        logger.error("api_db_init_failed", error=str(exc))
        app.state.pool = None

    # --- Redis ---
    redis_client = RedisClient(settings.redis_url)
    await redis_client.connect()
    app.state.redis = redis_client

    # --- Alerter ---
    alerter = SlackAlerter(settings.slack_webhook_url)
    app.state.alerter = alerter

    # --- Agent (needed by DLQ retry job) ---
    agent: Any = None
    if app.state.pool is not None:
        try:
            from app.agent.orchestrator import OrchestratorAgent

            agent = OrchestratorAgent(db_pool=app.state.pool, langfuse=None)
        except Exception as exc:
            logger.warning("agent_init_failed", error=str(exc))

    # --- Scheduler ---
    app.state.scheduler = None
    if app.state.pool is not None:
        try:
            scheduler = create_scheduler(app.state.pool, alerter, redis_client, agent)
            scheduler.start()
            app.state.scheduler = scheduler
            logger.info("scheduler_started")
        except Exception as exc:
            logger.warning("scheduler_start_failed", error=str(exc))

    yield

    # --- Shutdown ---
    if app.state.scheduler is not None:
        try:
            app.state.scheduler.shutdown(wait=False)
        except Exception:
            pass

    await redis_client.close()

    if app.state.pool is not None:
        await close_pool()

    logger.info("api_shutdown_complete")


# ---------------------------------------------------------------------------
# App construction
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Ops Intelligence Agent",
    description="AI-powered ops agent: anomaly detection + LLM-driven RCA",
    version="0.4.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://localhost:5173",
        "http://localhost:8000",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def request_logger(request: Request, call_next: Any) -> Any:
    start = time.monotonic()
    response = await call_next(request)
    duration_ms = round((time.monotonic() - start) * 1000, 1)
    logger.info(
        "http_request",
        method=request.method,
        path=request.url.path,
        status_code=response.status_code,
        duration_ms=duration_ms,
    )
    return response


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.error(
        "unhandled_exception",
        path=request.url.path,
        method=request.method,
        error=str(exc),
        error_type=type(exc).__name__,
    )
    return JSONResponse(
        status_code=500,
        content={"error": "Internal server error", "path": request.url.path},
    )


app.include_router(router, prefix="/api/v1")
