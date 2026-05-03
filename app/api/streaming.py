"""
SSE async generator for the /incidents/analyse endpoint.

Yields ``data: {...}\\n\\n`` formatted strings. Event types:
  status  — progress update
  token   — incremental text chunk from the RCA summary
  done    — final result with incident_id and severity
  error   — mid-stream failure (stream ends after this)
"""

from __future__ import annotations

import json
from typing import Any, AsyncGenerator

import structlog

logger = structlog.get_logger(__name__)


def _event(payload: dict[str, Any]) -> str:
    return f"data: {json.dumps(payload)}\n\n"


async def analyse_stream(
    service: str,
    scenario: str,
    db_pool: Any,
    agent: Any,
) -> AsyncGenerator[str, None]:
    """
    Full detection + agent RCA pipeline streamed as SSE.

    The caller wraps the return value in FastAPI StreamingResponse with
    media_type="text/event-stream".
    """
    try:
        yield _event({"type": "status", "message": "Building scenario..."})

        from app.detection import DetectionPipeline
        from app.ingestion.generator import SyntheticIngester, build_scenario

        try:
            fixture = build_scenario(scenario)
        except ValueError:
            yield _event({"type": "error", "message": f"Unknown scenario: {scenario}"})
            return

        signal_count = len(fixture.signal_shape)
        yield _event(
            {
                "type": "status",
                "message": f"Running detection on {signal_count} signals for {fixture.service}...",
            }
        )

        signals = [s.to_signal() for s in fixture.signal_shape]
        pipeline = DetectionPipeline(SyntheticIngester(signals=signals))

        rs = None
        async for rs in pipeline.run():
            pass

        if rs is None:
            yield _event({"type": "error", "message": "No signals processed by detection pipeline"})
            return

        yield _event(
            {
                "type": "status",
                "message": f"Detection complete — risk score {rs.score:.1f}/100",
            }
        )

        if rs.score < 70:
            yield _event(
                {
                    "type": "done",
                    "incident_id": None,
                    "severity": "LOW",
                    "service": service,
                    "cost_usd": 0.0,
                    "message": (
                        f"Score {rs.score:.1f} below agent activation threshold (70). "
                        "No incident created."
                    ),
                }
            )
            return

        yield _event({"type": "status", "message": "Agent investigating..."})

        result = await agent.investigate(rs)

        # Stream summary word-by-word in small chunks
        summary = result.get("summary", "")
        if summary:
            words = summary.split()
            for i in range(0, len(words), 3):
                chunk = " ".join(words[i : i + 3])
                yield _event({"type": "token", "content": chunk + " "})

        yield _event(
            {
                "type": "done",
                "incident_id": result.get("incident_id"),
                "severity": result.get("severity", "MEDIUM"),
                "service": result.get("service", service),
                "cost_usd": round(float(result.get("cost_usd", 0.0)), 4),
                "degraded_mode": bool(result.get("degraded_mode", False)),
            }
        )

    except Exception as exc:
        logger.error("analyse_stream_error", service=service, scenario=scenario, error=str(exc))
        yield _event({"type": "error", "message": str(exc)})
