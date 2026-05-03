"""
APScheduler setup for background jobs.

Three jobs:
  detection_loop  — interval 30 s: run detection for all monitored services
  on_call_briefing — cron 08:00 daily: send Slack briefing
  dlq_retry       — interval 60 s: retry one DLQ item
"""

from __future__ import annotations

import json
from typing import Any

import structlog

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Job implementations
# ---------------------------------------------------------------------------

async def _detection_tick(db_pool: Any, alerter: Any) -> None:
    """One detection cycle across all monitored services."""
    from app.core.config import settings
    from app.detection.patterns import PatternMatcher
    from app.detection.scorer import RiskScorer
    from app.detection.trend import TrendAnalyser
    from app.detection.window import SlidingWindowDetector
    from app.ingestion.base import Signal

    for service in settings.monitored_services:
        try:
            async with db_pool.acquire() as db:
                rows = await db.fetch(
                    """
                    SELECT ts, metrics FROM signals
                    WHERE service = $1
                      AND signal_type = 'metric'
                      AND ts > NOW() - INTERVAL '30 minutes'
                    ORDER BY ts ASC
                    """,
                    service,
                )

            if not rows:
                continue

            window = SlidingWindowDetector()
            trend = TrendAnalyser()
            patterns = PatternMatcher()
            scorer = RiskScorer(window, trend, patterns)

            for row in rows:
                m = (
                    row["metrics"]
                    if isinstance(row["metrics"], dict)
                    else json.loads(row["metrics"])
                )
                sig = Signal(service=service, ts=row["ts"], signal_type="metric", metrics=m)
                window.add_signal(sig)
                trend.add_signal(sig)
                patterns.add_signal(sig)

            rs = scorer.score(service)
            top_signal = rs.top_signals[0] if rs.top_signals else None

            async with db_pool.acquire() as db:
                await db.execute(
                    """
                    INSERT INTO risk_scores
                        (service, score, trend_score, pattern_score, llm_score,
                         top_signal, time_to_incident_min)
                    VALUES ($1, $2, $3, $4, $5, $6, $7)
                    """,
                    service,
                    rs.score,
                    rs.trend_score,
                    rs.pattern_score,
                    rs.anomaly_score,
                    top_signal,
                    rs.time_to_incident_min,
                )

            if rs.score > 85:
                await alerter.send_runbook_alert(service, [], rs.score)
            elif rs.score > 70:
                await alerter.send_early_warning(
                    service, rs.score, rs.top_signals, rs.time_to_incident_min
                )

        except Exception as exc:
            logger.warning("detection_tick_service_failed", service=service, error=str(exc))


async def _on_call_briefing(db_pool: Any, alerter: Any) -> None:
    """Query top-3 services and send the daily on-call Slack briefing."""
    try:
        async with db_pool.acquire() as db:
            rows = await db.fetch(
                """
                SELECT DISTINCT ON (service)
                    service, score, top_signal, time_to_incident_min
                FROM risk_scores
                ORDER BY service, ts DESC
                """
            )

        top_services = sorted(
            [
                {
                    "service": r["service"],
                    "score": r["score"],
                    "top_signal": r["top_signal"] or "—",
                    "time_to_incident_min": r["time_to_incident_min"],
                }
                for r in rows
            ],
            key=lambda x: x["score"],
            reverse=True,
        )[:3]

        await alerter.send_on_call_briefing(top_services)
    except Exception as exc:
        logger.warning("on_call_briefing_failed", error=str(exc))


async def _dlq_retry(redis_client: Any, db_pool: Any, agent: Any) -> None:
    """Pop one item from the DLQ and retry the incident analysis."""
    item = await redis_client.pop_from_dlq()
    if item is None:
        return

    retry_count = int(item.get("retry_count", 0))
    service = item.get("service", "unknown")

    if retry_count >= 3:
        logger.error("dlq_permanently_failed", service=service, item=item)
        return

    try:
        from app.detection.scorer import RiskScore

        rs = RiskScore(
            service=service,
            score=float(item.get("score", 85.0)),
            anomaly_score=float(item.get("anomaly_score", 0.0)),
            trend_score=float(item.get("trend_score", 0.0)),
            pattern_score=float(item.get("pattern_score", 0.0)),
            time_to_incident_min=item.get("time_to_incident_min"),
            top_signals=item.get("top_signals", []),
            scenario=item.get("scenario"),
        )
        await agent.investigate(rs)
        logger.info("dlq_retry_succeeded", service=service, retry_count=retry_count)
    except Exception as exc:
        logger.warning("dlq_retry_failed", service=service, retry_count=retry_count, error=str(exc))
        item["retry_count"] = retry_count + 1
        item["last_error"] = str(exc)
        await redis_client.push_to_dlq(item)


# ---------------------------------------------------------------------------
# Scheduler factory
# ---------------------------------------------------------------------------

def create_scheduler(
    db_pool: Any,
    alerter: Any,
    redis_client: Any,
    agent: Any,
) -> Any:
    """Build and return a configured AsyncIOScheduler (not yet started)."""
    from apscheduler.schedulers.asyncio import AsyncIOScheduler

    scheduler = AsyncIOScheduler()

    scheduler.add_job(
        _detection_tick,
        "interval",
        seconds=30,
        args=[db_pool, alerter],
        id="detection_loop",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        _on_call_briefing,
        "cron",
        hour=8,
        minute=0,
        args=[db_pool, alerter],
        id="on_call_briefing",
    )
    scheduler.add_job(
        _dlq_retry,
        "interval",
        seconds=60,
        args=[redis_client, db_pool, agent],
        id="dlq_retry",
        max_instances=1,
        coalesce=True,
    )

    return scheduler
