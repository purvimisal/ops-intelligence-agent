"""
Slack alerter using Block Kit messages via incoming webhooks.

If SLACK_WEBHOOK_URL is empty, every method logs the payload with structlog
and returns immediately — no HTTP call, no exception.
"""

from __future__ import annotations

from typing import Any

import httpx
import structlog

logger = structlog.get_logger(__name__)


class SlackAlerter:
    def __init__(self, webhook_url: str = "") -> None:
        self._webhook_url = webhook_url or ""

    # ------------------------------------------------------------------
    # Public alert methods
    # ------------------------------------------------------------------

    async def send_early_warning(
        self,
        service: str,
        risk_score: float,
        top_signals: list[str],
        time_to_incident_min: float | None,
    ) -> None:
        ttm_text = (
            f"~{time_to_incident_min:.0f} min to threshold breach"
            if time_to_incident_min is not None
            else "Time to incident: unknown"
        )
        bullets = "\n".join(f"• {s}" for s in top_signals[:3]) or "No signals captured"

        payload: dict[str, Any] = {
            "blocks": [
                {
                    "type": "header",
                    "text": {"type": "plain_text", "text": f"⚠️ Early Warning: {service}"},
                },
                {
                    "type": "section",
                    "fields": [
                        {"type": "mrkdwn", "text": f"*Risk Score*\n⚠️ *{risk_score:.1f}/100*"},
                        {"type": "mrkdwn", "text": f"*Estimate*\n{ttm_text}"},
                    ],
                },
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": f"*Top Signals*\n{bullets}"},
                },
            ]
        }
        logger.info("slack_early_warning", service=service, risk_score=risk_score)
        await self._post(payload)

    async def send_runbook_alert(
        self,
        service: str,
        matched_runbooks: list[str],
        risk_score: float,
    ) -> None:
        if matched_runbooks:
            rb_text = "\n".join(f"• {rb}" for rb in matched_runbooks)
        else:
            rb_text = "• No specific runbook matched — consult general ops runbook"

        payload: dict[str, Any] = {
            "blocks": [
                {
                    "type": "header",
                    "text": {"type": "plain_text", "text": f"🔴 Runbook Alert: {service}"},
                },
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": (
                            f"Risk score *{risk_score:.1f}/100* exceeded critical threshold (85).\n\n"
                            f"*Matched Runbooks*\n{rb_text}"
                        ),
                    },
                },
            ]
        }
        logger.info("slack_runbook_alert", service=service, risk_score=risk_score)
        await self._post(payload)

    async def send_incident_created(
        self,
        incident_id: int,
        service: str,
        severity: str,
        summary: str,
    ) -> None:
        emoji = {"CRITICAL": "🚨", "HIGH": "🔴", "MEDIUM": "🟡", "LOW": "🟢"}.get(severity, "⚪")

        payload: dict[str, Any] = {
            "blocks": [
                {
                    "type": "header",
                    "text": {
                        "type": "plain_text",
                        "text": f"{emoji} Incident #{incident_id} Created",
                    },
                },
                {
                    "type": "section",
                    "fields": [
                        {"type": "mrkdwn", "text": f"*Service*\n{service}"},
                        {"type": "mrkdwn", "text": f"*Severity*\n{emoji} {severity}"},
                    ],
                },
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": f"*Summary*\n{summary}"},
                },
            ]
        }
        logger.info("slack_incident_created", incident_id=incident_id, service=service)
        await self._post(payload)

    async def send_on_call_briefing(self, top_services: list[dict[str, Any]]) -> None:
        blocks: list[dict[str, Any]] = [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": "📋 On-Call Briefing — Top Services at Risk",
                },
            }
        ]

        for svc in top_services[:3]:
            service = svc.get("service", "unknown")
            score = float(svc.get("score", 0.0))
            top_signal = svc.get("top_signal", "—")
            ttm = svc.get("time_to_incident_min")
            ttm_text = f"{ttm:.0f} min" if ttm is not None else "—"

            blocks.append(
                {
                    "type": "section",
                    "fields": [
                        {"type": "mrkdwn", "text": f"*{service}*\nScore: {score:.1f}/100"},
                        {"type": "mrkdwn", "text": f"*ETA to incident*\n{ttm_text}"},
                    ],
                }
            )
            blocks.append(
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": f"↳ {top_signal}"},
                }
            )
            blocks.append({"type": "divider"})

        if not top_services:
            blocks.append(
                {"type": "section", "text": {"type": "mrkdwn", "text": "No elevated risk scores."}}
            )

        payload: dict[str, Any] = {"blocks": blocks}
        logger.info("slack_on_call_briefing", service_count=len(top_services[:3]))
        await self._post(payload)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _post(self, payload: dict[str, Any]) -> None:
        if not self._webhook_url:
            logger.info("slack_alert_no_webhook", payload=payload)
            return
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.post(self._webhook_url, json=payload)
                resp.raise_for_status()
        except Exception as exc:
            logger.warning("slack_post_failed", error=str(exc))
