"""
Tests for the Slack alerter (Day 4).

All tests are async via pytest-asyncio.
HTTP calls are intercepted by overriding the _post() method on instances —
no real Slack webhook is required.
"""

from __future__ import annotations

import pytest

from app.alerts.slack import SlackAlerter


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _capture_alerter(webhook: str = "https://hooks.slack.com/fake") -> tuple[SlackAlerter, list]:
    """Return an alerter whose _post() captures payloads instead of POSTing."""
    captured: list[dict] = []

    alerter = SlackAlerter(webhook)

    async def _capture(payload):
        captured.append(payload)

    alerter._post = _capture  # type: ignore[method-assign]
    return alerter, captured


# ---------------------------------------------------------------------------
# Graceful degradation — no webhook
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_webhook_early_warning_no_raise():
    alerter = SlackAlerter("")
    await alerter.send_early_warning("svc", 75.0, ["lag critical"], 10.0)


@pytest.mark.asyncio
async def test_no_webhook_runbook_alert_no_raise():
    alerter = SlackAlerter("")
    await alerter.send_runbook_alert("svc", ["runbook_kafka.md"], 91.0)


@pytest.mark.asyncio
async def test_no_webhook_incident_created_no_raise():
    alerter = SlackAlerter("")
    await alerter.send_incident_created(42, "svc", "HIGH", "Test summary")


@pytest.mark.asyncio
async def test_no_webhook_briefing_no_raise():
    alerter = SlackAlerter("")
    await alerter.send_on_call_briefing([{"service": "svc", "score": 80.0}])


# ---------------------------------------------------------------------------
# send_early_warning — Block Kit structure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_early_warning_payload_has_blocks():
    alerter, captured = _capture_alerter()
    await alerter.send_early_warning("kafka-consumer", 82.5, ["consumer_lag at 45000"], 8.0)

    assert len(captured) == 1
    payload = captured[0]
    assert "blocks" in payload
    assert len(payload["blocks"]) > 0


@pytest.mark.asyncio
async def test_early_warning_has_header_block():
    alerter, captured = _capture_alerter()
    await alerter.send_early_warning("kafka-consumer", 82.5, ["lag spike"], 5.0)

    blocks = captured[0]["blocks"]
    headers = [b for b in blocks if b.get("type") == "header"]
    assert len(headers) >= 1
    assert "kafka-consumer" in headers[0]["text"]["text"]


@pytest.mark.asyncio
async def test_early_warning_contains_risk_score():
    alerter, captured = _capture_alerter()
    await alerter.send_early_warning("pricing-engine", 77.3, ["error_rate rising"], 15.0)

    payload_str = str(captured[0])
    assert "77.3" in payload_str


@pytest.mark.asyncio
async def test_early_warning_contains_time_estimate():
    alerter, captured = _capture_alerter()
    await alerter.send_early_warning("audit-service", 71.0, ["cpu_pct at 89%"], 20.0)

    payload_str = str(captured[0])
    assert "20" in payload_str  # time-to-incident


@pytest.mark.asyncio
async def test_early_warning_no_ttm():
    alerter, captured = _capture_alerter()
    await alerter.send_early_warning("svc", 73.0, [], None)

    assert len(captured) == 1
    assert "blocks" in captured[0]


# ---------------------------------------------------------------------------
# send_runbook_alert
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_runbook_alert_contains_runbook_names():
    alerter, captured = _capture_alerter()
    await alerter.send_runbook_alert(
        "kafka-consumer", ["runbook_kafka_consumer_lag.md", "runbook_replication_lag.md"], 88.0
    )

    payload_str = str(captured[0])
    assert "runbook_kafka_consumer_lag.md" in payload_str
    assert "runbook_replication_lag.md" in payload_str


@pytest.mark.asyncio
async def test_runbook_alert_no_runbooks_graceful():
    alerter, captured = _capture_alerter()
    await alerter.send_runbook_alert("svc", [], 90.0)

    assert len(captured) == 1
    assert "blocks" in captured[0]


# ---------------------------------------------------------------------------
# send_incident_created
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_incident_created_contains_id_and_severity():
    alerter, captured = _capture_alerter()
    await alerter.send_incident_created(99, "payment-processor", "CRITICAL", "DB down")

    payload_str = str(captured[0])
    assert "99" in payload_str
    assert "CRITICAL" in payload_str
    assert "payment-processor" in payload_str


@pytest.mark.asyncio
async def test_incident_created_severity_emojis():
    alerter, captured = _capture_alerter()
    for severity, emoji in [("CRITICAL", "🚨"), ("HIGH", "🔴"), ("MEDIUM", "🟡"), ("LOW", "🟢")]:
        captured.clear()
        await alerter.send_incident_created(1, "svc", severity, "summary")
        assert emoji in str(captured[0])


# ---------------------------------------------------------------------------
# send_on_call_briefing — top-3 formatting
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_on_call_briefing_header_block():
    alerter, captured = _capture_alerter()
    await alerter.send_on_call_briefing([])

    blocks = captured[0]["blocks"]
    headers = [b for b in blocks if b.get("type") == "header"]
    assert len(headers) >= 1
    assert "Briefing" in headers[0]["text"]["text"]


@pytest.mark.asyncio
async def test_on_call_briefing_top3_only():
    alerter, captured = _capture_alerter()
    services = [
        {"service": f"svc-{i}", "score": float(90 - i)}
        for i in range(5)  # 5 services
    ]
    await alerter.send_on_call_briefing(services)

    payload_str = str(captured[0])
    assert "svc-0" in payload_str
    assert "svc-1" in payload_str
    assert "svc-2" in payload_str
    # 4th and 5th should be excluded (only top-3 are rendered)
    assert "svc-3" not in payload_str
    assert "svc-4" not in payload_str


@pytest.mark.asyncio
async def test_on_call_briefing_all_services_present():
    alerter, captured = _capture_alerter()
    top3 = [
        {"service": "kafka-consumer", "score": 85.0, "top_signal": "lag critical"},
        {"service": "pricing-engine", "score": 72.0},
        {"service": "auth-service", "score": 55.0},
    ]
    await alerter.send_on_call_briefing(top3)

    payload_str = str(captured[0])
    assert "kafka-consumer" in payload_str
    assert "pricing-engine" in payload_str
    assert "auth-service" in payload_str


@pytest.mark.asyncio
async def test_on_call_briefing_empty_no_raise():
    alerter, captured = _capture_alerter()
    await alerter.send_on_call_briefing([])
    assert len(captured) == 1


# ---------------------------------------------------------------------------
# Async-safety: all public methods are awaitable
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_all_methods_are_async():
    alerter = SlackAlerter("")
    import asyncio

    # All should complete without blocking
    await asyncio.gather(
        alerter.send_early_warning("s", 75.0, [], None),
        alerter.send_runbook_alert("s", [], 90.0),
        alerter.send_incident_created(1, "s", "HIGH", "test"),
        alerter.send_on_call_briefing([]),
    )
