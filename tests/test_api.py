"""
Tests for the FastAPI REST service (Day 4).

Uses FastAPI TestClient with mocked DB, Redis, and scheduler so no real
infrastructure is required.  The lifespan is exercised but all external
connections are patched at the app.api.main module level.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_pool_and_conn():
    conn = AsyncMock()
    conn.execute = AsyncMock()
    conn.fetch = AsyncMock(return_value=[])
    conn.fetchrow = AsyncMock(return_value=None)

    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
    return pool, conn


def _make_mock_redis():
    r = MagicMock()
    r.available = True
    r.connect = AsyncMock()
    r.close = AsyncMock()
    r.ping = AsyncMock(return_value=True)
    r.dlq_length = AsyncMock(return_value=0)
    return r


@pytest.fixture
def client():
    """
    TestClient with all external services mocked.
    Patches are applied around the lifespan so startup never touches
    real Postgres, Redis, or APScheduler.
    """
    pool, conn = _make_pool_and_conn()
    mock_redis = _make_mock_redis()
    mock_sched = MagicMock()
    mock_sched.start = MagicMock()
    mock_sched.shutdown = MagicMock()

    with (
        patch("app.api.main.init_db", AsyncMock()),
        patch("app.api.main.get_pool", AsyncMock(return_value=pool)),
        patch("app.api.main.close_pool", AsyncMock()),
        patch("app.api.main.RedisClient", return_value=mock_redis),
        patch("app.api.main.create_scheduler", return_value=mock_sched),
    ):
        from app.api.main import app

        with TestClient(app, raise_server_exceptions=False) as c:
            c._conn = conn
            c._pool = pool
            yield c


# ---------------------------------------------------------------------------
# GET /health
# ---------------------------------------------------------------------------


def test_health_ok(client):
    response = client.get("/api/v1/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert "db" in body
    assert "redis" in body
    assert "llm" in body
    assert "uptime_seconds" in body
    assert isinstance(body["uptime_seconds"], (int, float))


# ---------------------------------------------------------------------------
# GET /risk
# ---------------------------------------------------------------------------


def test_risk_returns_list(client):
    response = client.get("/api/v1/risk")
    assert response.status_code == 200
    assert isinstance(response.json(), list)


def test_risk_with_data(client):
    now = datetime.now(timezone.utc)
    mock_row = MagicMock()
    mock_row.__getitem__ = lambda s, k: {
        "service": "kafka-consumer",
        "score": 82.5,
        "top_signal": "consumer_lag at 45000",
        "time_to_incident_min": 12.0,
        "computed_at": now,
    }[k]

    client._conn.fetch = AsyncMock(return_value=[mock_row])
    response = client.get("/api/v1/risk")
    assert response.status_code == 200
    # Pool is mocked at module level; just verify shape
    data = response.json()
    assert isinstance(data, list)


# ---------------------------------------------------------------------------
# GET /incidents
# ---------------------------------------------------------------------------


def test_incidents_returns_list(client):
    response = client.get("/api/v1/incidents")
    assert response.status_code == 200
    assert isinstance(response.json(), list)


# ---------------------------------------------------------------------------
# GET /incidents/{id}
# ---------------------------------------------------------------------------


def test_incident_not_found_returns_404(client):
    response = client.get("/api/v1/incidents/99999")
    assert response.status_code == 404
    body = response.json()
    assert "detail" in body
    assert "99999" in body["detail"]


def test_incident_found(client):
    now = datetime.now(timezone.utc)
    mock_row = {
        "id": 1,
        "service": "kafka-consumer",
        "severity": "HIGH",
        "summary": "Lag detected",
        "root_cause": "DB slow",
        "hypotheses": ["DB write latency"],
        "fix_steps": ["Scale up"],
        "risk_score": 82.5,
        "created_at": now,
    }
    row_mock = MagicMock()
    row_mock.__getitem__ = lambda s, k: mock_row[k]

    client._conn.fetchrow = AsyncMock(return_value=row_mock)
    response = client.get("/api/v1/incidents/1")
    assert response.status_code == 200
    body = response.json()
    assert body["id"] == 1
    assert body["severity"] == "HIGH"


# ---------------------------------------------------------------------------
# POST /incidents/analyse
# ---------------------------------------------------------------------------


def test_analyse_missing_body_returns_422(client):
    response = client.post("/api/v1/incidents/analyse")
    assert response.status_code == 422


def test_analyse_missing_field_returns_422(client):
    response = client.post("/api/v1/incidents/analyse", json={"service": "kafka-consumer"})
    assert response.status_code == 422


def test_analyse_streams_sse(client):
    """Verify content-type is text/event-stream; no real Claude call made."""

    async def mock_stream(*args, **kwargs):
        yield 'data: {"type": "status", "message": "test"}\n\n'
        yield 'data: {"type": "done", "incident_id": 1, "severity": "HIGH"}\n\n'

    with patch("app.api.routes.analyse_stream", mock_stream):
        response = client.post(
            "/api/v1/incidents/analyse",
            json={"service": "kafka-consumer", "scenario": "kafka_lag_cascade"},
        )
    assert response.status_code == 200
    assert "text/event-stream" in response.headers["content-type"]


# ---------------------------------------------------------------------------
# GET /metrics
# ---------------------------------------------------------------------------


def test_metrics_endpoint_ok(client):
    response = client.get("/api/v1/metrics")
    assert response.status_code == 200
    # prometheus_client text format
    text = response.text
    assert "risk_score_gauge" in text


def test_metrics_content_type(client):
    response = client.get("/api/v1/metrics")
    assert "text/plain" in response.headers["content-type"]


# ---------------------------------------------------------------------------
# Global exception handler
# ---------------------------------------------------------------------------


def test_global_exception_handler_hides_traceback(client):
    """An unhandled exception must return clean JSON with no Python traceback."""
    # Make the DB fetch in /risk raise an unhandled RuntimeError
    client._conn.fetch = AsyncMock(side_effect=RuntimeError("intentional error for test"))

    try:
        response = client.get("/api/v1/risk")
        assert response.status_code == 500
        body = response.json()
        assert "error" in body
        assert "Internal server error" in body["error"]
        # Must not expose raw Python traceback or exception class names
        assert "Traceback" not in response.text
        assert "RuntimeError" not in response.text
    finally:
        client._conn.fetch = AsyncMock(return_value=[])
