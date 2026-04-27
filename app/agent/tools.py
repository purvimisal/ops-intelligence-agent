"""
Agent tool functions and their Anthropic schema definitions.

Each tool function is async and accepts a DB connection as its first argument.
Tools that need the OpenAI client (query_runbook_kb) or Slack (create_incident_report)
accept those as additional keyword arguments.

TOOL_SCHEMAS is the list passed directly to the Anthropic messages.create() call.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

import httpx
import structlog

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Anthropic tool schema definitions
# ---------------------------------------------------------------------------

TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "search_logs",
        "description": (
            "Search recent log lines for a service. Returns up to 10 matching log "
            "entries. Use this to find specific error messages, stack traces, or "
            "unusual events that explain an anomaly."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "service": {
                    "type": "string",
                    "description": "Service name to search logs for.",
                },
                "query": {
                    "type": "string",
                    "description": "Substring or keyword to search for in log messages.",
                },
                "window_minutes": {
                    "type": "integer",
                    "description": "How many minutes back to search (e.g. 30).",
                },
            },
            "required": ["service", "query", "window_minutes"],
        },
    },
    {
        "name": "get_pipeline_metrics",
        "description": (
            "Retrieve recent metric snapshots for a service: throughput, consumer_lag, "
            "error_rate, memory_pct, cpu_pct. Includes trend direction (RISING/FALLING/STABLE) "
            "computed by comparing first-half vs second-half of the window."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "service": {
                    "type": "string",
                    "description": "Service name to retrieve metrics for.",
                },
                "window_minutes": {
                    "type": "integer",
                    "description": "How many minutes of history to return (e.g. 30).",
                },
            },
            "required": ["service", "window_minutes"],
        },
    },
    {
        "name": "query_runbook_kb",
        "description": (
            "Search the runbook knowledge base using semantic similarity. Returns up to 2 "
            "runbook sections most relevant to the described issue. Use this to find "
            "specific remediation steps and prevention guidance."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "issue_description": {
                    "type": "string",
                    "description": (
                        "Natural language description of the observed issue "
                        "(e.g. 'kafka consumer lag growing rapidly, throughput dropping')."
                    ),
                },
            },
            "required": ["issue_description"],
        },
    },
    {
        "name": "get_service_health",
        "description": (
            "Get the latest health snapshot for a service: pod restart count, CPU %, "
            "memory %, and last deployment timestamp. Use this as your first tool call "
            "to establish a baseline understanding of service state."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "service": {
                    "type": "string",
                    "description": "Service name to retrieve health information for.",
                },
            },
            "required": ["service"],
        },
    },
    {
        "name": "create_incident_report",
        "description": (
            "Create a structured incident report. This MUST be your final tool call. "
            "Summarise the root cause, list ranked hypotheses, and provide concrete "
            "fix steps. Triggers a Slack alert if a webhook is configured."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "service": {
                    "type": "string",
                    "description": "Service name the incident is for.",
                },
                "severity": {
                    "type": "string",
                    "enum": ["CRITICAL", "HIGH", "MEDIUM", "LOW"],
                    "description": "Incident severity.",
                },
                "summary": {
                    "type": "string",
                    "description": "One-sentence summary of the incident.",
                },
                "root_cause": {
                    "type": "string",
                    "description": "Most likely root cause based on investigation findings.",
                },
                "hypotheses": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Ranked list of root cause hypotheses (most likely first).",
                },
                "fix_steps": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Ordered list of concrete remediation steps.",
                },
            },
            "required": [
                "service", "severity", "summary", "root_cause", "hypotheses", "fix_steps"
            ],
        },
    },
]


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

async def search_logs(
    db: Any,
    service: str,
    query: str,
    window_minutes: int,
) -> dict[str, Any]:
    """
    Query signals table for log entries matching query string within the time window.
    Returns the 10 most recent matching log lines.
    """
    rows = await db.fetch(
        """
        SELECT ts, level, message, trace_id, error_code
        FROM signals
        WHERE service = $1
          AND signal_type = 'log'
          AND ts > NOW() - make_interval(mins => $2)
          AND (message ILIKE $3 OR error_code ILIKE $3)
        ORDER BY ts DESC
        LIMIT 10
        """,
        service,
        window_minutes,
        f"%{query}%",
    )
    logs = [
        {
            "ts": str(r["ts"]),
            "level": r["level"],
            "message": r["message"],
            "trace_id": r["trace_id"],
            "error_code": r["error_code"],
        }
        for r in rows
    ]
    logger.info("tool_search_logs", service=service, query=query, result_count=len(logs))
    return {"service": service, "query": query, "window_minutes": window_minutes, "logs": logs}


async def get_pipeline_metrics(
    db: Any,
    service: str,
    window_minutes: int,
) -> dict[str, Any]:
    """
    Retrieve metric snapshots and compute trend direction per metric.
    Trend is computed by comparing mean of first half vs second half of the window.
    """
    rows = await db.fetch(
        """
        SELECT ts, metrics
        FROM signals
        WHERE service = $1
          AND signal_type = 'metric'
          AND ts > NOW() - make_interval(mins => $2)
          AND metrics IS NOT NULL
        ORDER BY ts ASC
        """,
        service,
        window_minutes,
    )

    if not rows:
        return {"service": service, "window_minutes": window_minutes, "snapshot_count": 0, "latest": {}, "trends": {}}

    metric_keys = ["throughput", "consumer_lag", "error_rate", "memory_pct", "cpu_pct", "disk_pct"]
    series: dict[str, list[float]] = {k: [] for k in metric_keys}

    snapshots = []
    for r in rows:
        m = r["metrics"] if isinstance(r["metrics"], dict) else json.loads(r["metrics"])
        snap: dict[str, Any] = {"ts": str(r["ts"])}
        for k in metric_keys:
            v = float(m.get(k, 0.0))
            series[k].append(v)
            snap[k] = round(v, 4)
        snapshots.append(snap)

    trends: dict[str, str] = {}
    for k, vals in series.items():
        if len(vals) < 4:
            trends[k] = "INSUFFICIENT_DATA"
            continue
        mid = len(vals) // 2
        first_mean = sum(vals[:mid]) / mid
        second_mean = sum(vals[mid:]) / (len(vals) - mid)
        if first_mean == 0:
            trends[k] = "STABLE"
        else:
            pct_change = (second_mean - first_mean) / abs(first_mean)
            if pct_change > 0.05:
                trends[k] = "RISING"
            elif pct_change < -0.05:
                trends[k] = "FALLING"
            else:
                trends[k] = "STABLE"

    latest = snapshots[-1] if snapshots else {}
    logger.info("tool_get_pipeline_metrics", service=service, snapshot_count=len(snapshots))
    return {
        "service": service,
        "window_minutes": window_minutes,
        "snapshot_count": len(snapshots),
        "latest": latest,
        "trends": trends,
    }


async def query_runbook_kb(
    db: Any,
    issue_description: str,
    embedder: Any = None,
) -> dict[str, Any]:
    """
    Embed the issue description and perform cosine similarity search against chunks table.
    Returns top-2 most relevant runbook sections.
    embedder must have: async embed(text: str) -> list[float]
    """
    if embedder is None:
        return {
            "issue_description": issue_description,
            "results": [],
            "error": "Embedder not configured",
        }

    try:
        embedding = await embedder.embed_query(issue_description)
    except Exception as exc:
        logger.warning("tool_query_runbook_kb_embed_failed", error=str(exc))
        return {"issue_description": issue_description, "results": [], "error": str(exc)}

    # pgvector cosine distance operator: <=>
    embedding_str = "[" + ",".join(str(x) for x in embedding) + "]"
    rows = await db.fetch(
        """
        SELECT runbook, section, content,
               1 - (embedding <=> $1::vector) AS similarity
        FROM chunks
        WHERE embedding IS NOT NULL
        ORDER BY embedding <=> $1::vector
        LIMIT 2
        """,
        embedding_str,
    )

    results = [
        {
            "runbook": r["runbook"],
            "section": r["section"],
            "content": r["content"][:800],
            "similarity": round(float(r["similarity"]), 4),
        }
        for r in rows
    ]
    logger.info("tool_query_runbook_kb", result_count=len(results))
    return {"issue_description": issue_description, "results": results}


async def get_service_health(
    db: Any,
    service: str,
) -> dict[str, Any]:
    """
    Return the latest health snapshot for a service from the signals table:
    pod_restarts, cpu_pct, memory_pct, last_deployment_at (approximated from scenario).
    """
    row = await db.fetchrow(
        """
        SELECT ts, metrics
        FROM signals
        WHERE service = $1
          AND signal_type = 'metric'
          AND metrics IS NOT NULL
        ORDER BY ts DESC
        LIMIT 1
        """,
        service,
    )

    if not row:
        return {"service": service, "error": "no metric signals found"}

    m = row["metrics"] if isinstance(row["metrics"], dict) else json.loads(row["metrics"])
    result = {
        "service": service,
        "as_of": str(row["ts"]),
        "pod_restarts": int(m.get("pod_restarts", 0)),
        "cpu_pct": round(float(m.get("cpu_pct", 0.0)), 2),
        "memory_pct": round(float(m.get("memory_pct", 0.0)), 2),
        "disk_pct": round(float(m.get("disk_pct", 0.0)), 2),
        "throughput": round(float(m.get("throughput", 0.0)), 2),
        "consumer_lag": round(float(m.get("consumer_lag", 0.0)), 2),
        "error_rate": round(float(m.get("error_rate", 0.0)), 5),
    }
    logger.info("tool_get_service_health", service=service, cpu=result["cpu_pct"], memory=result["memory_pct"])
    return result


async def create_incident_report(
    db: Any,
    service: str,
    severity: str,
    summary: str,
    root_cause: str,
    hypotheses: list[str],
    fix_steps: list[str],
    risk_score: float | None = None,
    scenario: str | None = None,
    langfuse_url: str | None = None,
    slack_webhook_url: str | None = None,
) -> dict[str, Any]:
    """
    Insert a structured incident report into the incidents table.
    Optionally fires a Slack webhook notification.
    Returns incident_id and created_at.
    """
    row = await db.fetchrow(
        """
        INSERT INTO incidents
            (service, severity, summary, root_cause, hypotheses, fix_steps,
             risk_score, scenario, langfuse_url)
        VALUES ($1, $2, $3, $4, $5::jsonb, $6::jsonb, $7, $8, $9)
        RETURNING id, created_at
        """,
        service,
        severity,
        summary,
        root_cause,
        json.dumps(hypotheses),
        json.dumps(fix_steps),
        risk_score,
        scenario,
        langfuse_url,
    )

    incident_id = row["id"]
    created_at = row["created_at"]

    logger.info(
        "incident_created",
        incident_id=incident_id,
        service=service,
        severity=severity,
    )

    # Slack notification (best-effort)
    if slack_webhook_url:
        try:
            payload = {
                "text": f":rotating_light: *Incident #{incident_id}* — {service} ({severity})\n{summary}\n*Root cause:* {root_cause}",
            }
            async with httpx.AsyncClient(timeout=5.0) as client:
                await client.post(slack_webhook_url, json=payload)
        except Exception as exc:
            logger.warning("slack_webhook_failed", error=str(exc))

    return {
        "incident_id": incident_id,
        "service": service,
        "severity": severity,
        "summary": summary,
        "root_cause": root_cause,
        "hypotheses": hypotheses,
        "fix_steps": fix_steps,
        "created_at": str(created_at),
    }
