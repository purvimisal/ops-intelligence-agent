"""
OrchestratorAgent — the core Claude tool-use agent loop.

Takes a RiskScore from the detection layer, builds an investigation context,
calls Claude claude-sonnet-4-5 with the 5 tool definitions, executes tool
calls, and terminates with a structured incident report.

Activation threshold: risk_score.score >= 70  (configurable)
Hard round cap: 6 rounds before forcing create_incident_report
Graceful degradation: circuit breaker skips LLM on repeated failures
LLM tracing: each session wrapped in a Langfuse trace (skipped if keys absent)
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import structlog

from app.agent.circuit_breaker import CircuitBreaker
from app.agent.prompts import PROMPT_VERSION, SYSTEM_PROMPT
from app.agent.tools import (
    TOOL_SCHEMAS,
    create_incident_report,
    get_pipeline_metrics,
    get_service_health,
    query_runbook_kb,
    search_logs,
)
from app.core.config import settings
from app.detection.scorer import RiskScore

logger = structlog.get_logger(__name__)

_ACTIVATION_THRESHOLD = 70.0
_MAX_ROUNDS = 6
_MODEL = "claude-sonnet-4-5"

# Sentinel for optional langfuse argument (None is a valid value meaning "no tracing")
_UNSET = object()


def _make_langfuse() -> Any | None:
    """Return a Langfuse client or None if keys are missing/invalid."""
    if not settings.langfuse_public_key or not settings.langfuse_secret_key:
        return None
    try:
        from langfuse import Langfuse

        return Langfuse(
            public_key=settings.langfuse_public_key,
            secret_key=settings.langfuse_secret_key,
            host=settings.langfuse_host,
        )
    except Exception as exc:
        logger.warning("langfuse_init_failed", error=str(exc))
        return None


class OrchestratorAgent:
    """
    Async agent that investigates a high-risk service using Claude tool use.

    Parameters
    ----------
    db_pool:              asyncpg connection pool (acquired per investigation)
    anthropic_client:     pre-constructed AsyncAnthropic client (injectable for tests)
    embedder:             object with async embed(text) -> list[float]; defaults to LocalEmbedder
    circuit_breaker:      injectable CircuitBreaker (new instance by default)
    langfuse:             injectable Langfuse client; pass None to disable tracing;
                          omit to auto-initialise from settings
    activation_threshold: minimum risk score to trigger investigation
    """

    def __init__(
        self,
        db_pool: Any,
        anthropic_client: Any = None,
        embedder: Any = None,
        circuit_breaker: CircuitBreaker | None = None,
        langfuse: Any = _UNSET,
        activation_threshold: float = _ACTIVATION_THRESHOLD,
    ) -> None:
        self._pool = db_pool
        self._circuit_breaker = circuit_breaker if circuit_breaker is not None else CircuitBreaker()
        self._activation_threshold = activation_threshold
        self._langfuse = langfuse if langfuse is not _UNSET else _make_langfuse()

        if anthropic_client is not None:
            self._anthropic = anthropic_client
        else:
            import anthropic
            self._anthropic = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)

        if embedder is not None:
            self._embedder = embedder
        else:
            from app.agent.embedder import LocalEmbedder
            self._embedder = LocalEmbedder()

    async def investigate(self, risk_score: RiskScore) -> dict[str, Any]:
        """
        Run a full investigation for the given RiskScore.

        Returns a dict with at minimum: service, severity, summary,
        root_cause, hypotheses, fix_steps.  incident_id is set when a
        report was persisted to the DB; None in degraded/skipped paths.
        """
        if risk_score.score < self._activation_threshold:
            logger.info(
                "orchestrator_skipped",
                service=risk_score.service,
                score=risk_score.score,
                threshold=self._activation_threshold,
            )
            return {
                "skipped": True,
                "reason": (
                    f"risk_score {risk_score.score:.1f} below "
                    f"threshold {self._activation_threshold}"
                ),
            }

        if self._circuit_breaker.is_open():
            logger.warning("orchestrator_degraded_mode", service=risk_score.service)
            return self._build_degraded_report(risk_score)

        trace = None
        trace_url: str | None = None
        if self._langfuse is not None:
            try:
                trace = self._langfuse.trace(
                    name="rca_investigation",
                    id=str(uuid.uuid4()),
                    input={
                        "service": risk_score.service,
                        "score": risk_score.score,
                        "top_signals": risk_score.top_signals,
                    },
                    metadata={"prompt_version": PROMPT_VERSION},
                )
                get_url = getattr(trace, "get_trace_url", None)
                trace_url = get_url() if callable(get_url) else None
            except Exception as exc:
                logger.warning("langfuse_trace_failed", error=str(exc))

        try:
            result = await self._run_loop(risk_score, trace)
            self._circuit_breaker.record_success()
            if trace_url and isinstance(result, dict):
                result["langfuse_url"] = trace_url
            if trace is not None:
                try:
                    trace.update(output=result, status_message="completed")
                    self._langfuse.flush()
                except Exception:
                    pass
            return result
        except Exception as exc:
            self._circuit_breaker.record_failure()
            logger.error(
                "orchestrator_llm_error", service=risk_score.service, error=str(exc)
            )
            if self._circuit_breaker.is_open():
                return self._build_degraded_report(risk_score)
            raise

    async def _run_loop(
        self, risk_score: RiskScore, trace: Any
    ) -> dict[str, Any]:
        """Execute the Claude agent loop, returning the final incident report dict."""
        messages: list[dict[str, Any]] = [
            {
                "role": "user",
                "content": self._build_user_message(risk_score),
            }
        ]
        final_report: dict[str, Any] | None = None

        async with self._pool.acquire() as db:
            for round_num in range(1, _MAX_ROUNDS + 1):
                # On the last round, nudge Claude to produce the report
                if round_num == _MAX_ROUNDS and final_report is None:
                    messages.append({
                        "role": "user",
                        "content": (
                            "This is your final round. You MUST call "
                            "create_incident_report now with your best assessment."
                        ),
                    })

                response = await self._anthropic.messages.create(
                    model=_MODEL,
                    max_tokens=4096,
                    system=SYSTEM_PROMPT,
                    tools=TOOL_SCHEMAS,
                    messages=messages,
                )

                logger.info(
                    "orchestrator_round",
                    round=round_num,
                    stop_reason=response.stop_reason,
                    service=risk_score.service,
                )

                messages.append({"role": "assistant", "content": response.content})

                if response.stop_reason in ("end_turn", "max_tokens"):
                    break

                if response.stop_reason != "tool_use":
                    break

                tool_results: list[dict[str, Any]] = []
                for block in response.content:
                    if getattr(block, "type", None) != "tool_use":
                        continue

                    span = None
                    if trace is not None:
                        try:
                            span = trace.span(name=f"tool_{block.name}", input=block.input)
                        except Exception:
                            pass

                    if block.name == "create_incident_report":
                        # create_incident_report needs extra context not in block.input
                        final_report = await create_incident_report(
                            db=db,
                            slack_webhook_url=settings.slack_webhook_url or None,
                            risk_score=risk_score.score,
                            scenario=risk_score.scenario,
                            **block.input,
                        )
                        tool_result = final_report
                    else:
                        tool_result = await self._dispatch_tool(db, block.name, block.input)

                    if span is not None:
                        try:
                            span.end(output=tool_result)
                        except Exception:
                            pass

                    logger.info(
                        "tool_called",
                        tool=block.name,
                        round=round_num,
                        service=risk_score.service,
                    )
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": json.dumps(tool_result, default=str),
                    })

                messages.append({"role": "user", "content": tool_results})

                if final_report is not None:
                    break

        if final_report is not None:
            return final_report

        # Fallback: extract any text from last assistant turn
        return self._extract_text_fallback(risk_score, messages)

    async def _dispatch_tool(
        self, db: Any, name: str, inputs: dict[str, Any]
    ) -> dict[str, Any]:
        """Route a tool call to the appropriate async function."""
        if name == "search_logs":
            return await search_logs(db, **inputs)
        if name == "get_pipeline_metrics":
            return await get_pipeline_metrics(db, **inputs)
        if name == "query_runbook_kb":
            return await query_runbook_kb(db, embedder=self._embedder, **inputs)
        if name == "get_service_health":
            return await get_service_health(db, **inputs)
        logger.warning("unknown_tool_called", tool=name)
        return {"error": f"unknown tool: {name}"}

    def _build_user_message(self, rs: RiskScore) -> str:
        lines = [
            f"Service '{rs.service}' has a risk score of {rs.score:.1f}/100.",
            f"  Anomaly: {rs.anomaly_score:.1f}  |  Trend: {rs.trend_score:.1f}  |  Pattern: {rs.pattern_score:.1f}",
        ]
        if rs.time_to_incident_min is not None:
            lines.append(
                f"  Estimated time to threshold breach: {rs.time_to_incident_min:.1f} min"
            )
        if rs.top_signals:
            lines.append("\nTop detection signals:")
            for sig in rs.top_signals:
                lines.append(f"  • {sig}")
        lines.append("\nPlease investigate and produce a structured incident report.")
        return "\n".join(lines)

    def _build_degraded_report(self, risk_score: RiskScore) -> dict[str, Any]:
        """Minimal RCA from detection signals only — no LLM calls."""
        severity = (
            "HIGH" if risk_score.score >= 85
            else "MEDIUM" if risk_score.score >= 70
            else "LOW"
        )
        top = (
            risk_score.top_signals[0]
            if risk_score.top_signals
            else "elevated risk score detected"
        )
        hypotheses = (
            [f"Detection layer signal: {s}" for s in risk_score.top_signals[:3]]
            or ["Anomalous metric behaviour — manual investigation required"]
        )
        logger.warning(
            "degraded_report_generated",
            service=risk_score.service,
            score=risk_score.score,
        )
        return {
            "incident_id": None,
            "service": risk_score.service,
            "severity": severity,
            "summary": (
                f"{risk_score.service} risk score {risk_score.score:.1f}/100 — {top}"
            ),
            "root_cause": (
                "Root cause analysis unavailable (LLM circuit breaker open). "
                f"Detection signals: {'; '.join(risk_score.top_signals[:2])}"
            ),
            "hypotheses": hypotheses,
            "fix_steps": [
                "Review detection signals in Grafana dashboard",
                "Check recent deployments for correlation",
                "Consult relevant runbook for the identified signal pattern",
                "Escalate to on-call engineer if risk score remains elevated",
            ],
            "degraded_mode": True,
        }

    def _extract_text_fallback(
        self, risk_score: RiskScore, messages: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """Extract any assistant text when no structured report was created."""
        for msg in reversed(messages):
            if msg.get("role") != "assistant":
                continue
            content = msg.get("content", "")
            if isinstance(content, list):
                texts = [
                    getattr(b, "text", "") for b in content
                    if getattr(b, "type", None) == "text"
                ]
                text = " ".join(t for t in texts if t).strip()
            elif isinstance(content, str):
                text = content.strip()
            else:
                text = ""
            if text:
                return {
                    "incident_id": None,
                    "service": risk_score.service,
                    "severity": "HIGH" if risk_score.score >= 85 else "MEDIUM",
                    "summary": text[:200],
                    "root_cause": text,
                    "hypotheses": [],
                    "fix_steps": [],
                }
        return self._build_degraded_report(risk_score)
