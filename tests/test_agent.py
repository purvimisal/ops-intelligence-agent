"""
Tests for the agent layer (Day 3).

- All tests mock the Anthropic and OpenAI clients — no real API calls.
- DB tool tests use a real asyncpg connection to a test database.
  Tests requiring a DB connection are skipped if Postgres is unavailable.
- Circuit breaker tests are fully in-memory (no DB or LLM needed).
"""

from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

from app.agent.circuit_breaker import CircuitBreaker, CBState
from app.agent.prompts import PROMPT_VERSION, SYSTEM_PROMPT, DEGRADED_PROMPT
from app.agent.tools import TOOL_SCHEMAS
from app.detection.scorer import RiskScore

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_risk_score(score: float = 85.0, service: str = "kafka-consumer") -> RiskScore:
    return RiskScore(
        service=service,
        score=score,
        anomaly_score=score * 0.3,
        trend_score=score * 0.35,
        pattern_score=score * 0.35,
        time_to_incident_min=15.0,
        top_signals=[
            "consumer_lag at 45000 exceeded CRITICAL threshold 20000",
            "consumer_lag trend: 8.3 min to threshold (slope +1450/step, R²=0.97, HIGH)",
        ],
        scenario="kafka_lag_cascade",
    )


def _make_tool_use_block(name: str, inputs: dict, tool_id: str = "tid_001") -> MagicMock:
    block = MagicMock()
    block.type = "tool_use"
    block.name = name
    block.input = inputs
    block.id = tool_id
    return block


def _make_text_block(text: str) -> MagicMock:
    block = MagicMock()
    block.type = "text"
    block.text = text
    return block


def _make_anthropic_response(stop_reason: str, content: list) -> MagicMock:
    resp = MagicMock()
    resp.stop_reason = stop_reason
    resp.content = content
    return resp


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_db_pool():
    """Mock asyncpg pool that yields a mock connection."""
    conn = AsyncMock()
    pool = MagicMock()
    pool.acquire = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
    return pool, conn


@pytest.fixture
def mock_anthropic():
    client = AsyncMock()
    client.messages = AsyncMock()
    return client


@pytest.fixture
def mock_openai():
    """Kept for backward compat in a few tests that still reference it."""
    client = AsyncMock()
    return client


@pytest.fixture
def mock_embedder():
    embedder = AsyncMock()
    embedder.embed = AsyncMock(return_value=[0.1] * 768)
    embedder.embed_query = AsyncMock(return_value=[0.1] * 768)
    embedder.dimension = 768
    return embedder


@pytest.fixture
def cb():
    return CircuitBreaker(failure_threshold=5, recovery_timeout_sec=60.0)


# ---------------------------------------------------------------------------
# DB connection fixture — skips if Postgres unavailable
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def db_conn():
    """Real asyncpg connection to test database. Skips if Postgres not available."""
    try:
        import asyncpg
        from app.core.config import settings
        conn = await asyncpg.connect(dsn=settings.database_url)
        # ensure tables exist
        await conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS signals (
                id BIGSERIAL PRIMARY KEY, ts TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                service TEXT NOT NULL, signal_type TEXT NOT NULL,
                level TEXT, message TEXT, trace_id TEXT, duration_ms FLOAT,
                error_code TEXT, metrics JSONB, scenario TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS incidents (
                id BIGSERIAL PRIMARY KEY, service TEXT NOT NULL,
                severity TEXT NOT NULL, summary TEXT NOT NULL,
                root_cause TEXT, hypotheses JSONB, fix_steps JSONB,
                risk_score FLOAT, scenario TEXT, langfuse_url TEXT,
                status TEXT NOT NULL DEFAULT 'open',
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                resolved_at TIMESTAMPTZ
            )
        """)
        # Drop chunks if it exists with the wrong vector dimension
        await conn.execute("""
            DO $$
            BEGIN
                IF EXISTS (
                    SELECT 1 FROM pg_attribute a
                    JOIN pg_class c ON c.oid = a.attrelid
                    WHERE c.relname = 'chunks' AND a.attname = 'embedding'
                      AND a.atttypmod <> 768 AND a.attnum > 0
                ) THEN DROP TABLE chunks CASCADE; END IF;
            END $$
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS chunks (
                id BIGSERIAL PRIMARY KEY, runbook TEXT NOT NULL,
                section TEXT NOT NULL, content TEXT NOT NULL,
                embedding vector(768), content_hash TEXT UNIQUE,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        yield conn
        # cleanup test data
        await conn.execute("DELETE FROM chunks WHERE runbook LIKE 'test_%'")
        await conn.execute("DELETE FROM incidents WHERE scenario = 'test_scenario'")
        await conn.execute("DELETE FROM signals WHERE scenario = 'test_scenario'")
        await conn.close()
    except Exception:
        pytest.skip("Postgres not available")


# ---------------------------------------------------------------------------
# Circuit breaker tests
# ---------------------------------------------------------------------------

class TestCircuitBreaker:
    def test_initial_state_is_closed(self, cb):
        assert cb.state == CBState.CLOSED.value
        assert cb.is_closed()
        assert not cb.is_open()

    def test_opens_after_threshold_failures(self, cb):
        for _ in range(4):
            cb.record_failure()
        assert cb.state == CBState.CLOSED.value  # not yet open

        cb.record_failure()  # 5th failure
        assert cb.state == CBState.OPEN.value
        assert cb.is_open()

    def test_success_resets_failure_count(self, cb):
        for _ in range(3):
            cb.record_failure()
        cb.record_success()
        assert cb.consecutive_failures == 0
        assert cb.state == CBState.CLOSED.value

    def test_success_closes_open_circuit(self, cb):
        for _ in range(5):
            cb.record_failure()
        assert cb.is_open()
        cb.record_success()
        assert cb.state == CBState.CLOSED.value

    def test_transitions_to_half_open_after_timeout(self, cb):
        for _ in range(5):
            cb.record_failure()
        assert cb.is_open()
        # Simulate timeout elapsed
        cb._opened_at = time.monotonic() - 61.0
        assert cb.state == CBState.HALF_OPEN.value
        assert cb.is_half_open()

    def test_half_open_failure_reopens(self, cb):
        for _ in range(5):
            cb.record_failure()
        cb._opened_at = time.monotonic() - 61.0  # force HALF_OPEN
        assert cb.is_half_open()
        cb.record_failure()
        assert cb.state == CBState.OPEN.value

    def test_half_open_success_closes(self, cb):
        for _ in range(5):
            cb.record_failure()
        cb._opened_at = time.monotonic() - 61.0  # force HALF_OPEN
        cb.record_success()
        assert cb.state == CBState.CLOSED.value

    def test_open_circuit_does_not_increment_past_threshold(self, cb):
        for _ in range(8):
            cb.record_failure()
        assert cb.is_open()

    def test_custom_threshold(self):
        cb = CircuitBreaker(failure_threshold=2, recovery_timeout_sec=10.0)
        cb.record_failure()
        assert cb.state == CBState.CLOSED.value
        cb.record_failure()
        assert cb.state == CBState.OPEN.value


# ---------------------------------------------------------------------------
# Orchestrator activation threshold
# ---------------------------------------------------------------------------

class TestOrchestratorThreshold:
    @pytest.mark.asyncio
    async def test_skips_below_threshold(self, mock_db_pool, mock_anthropic, mock_embedder):
        from app.agent.orchestrator import OrchestratorAgent

        pool, _ = mock_db_pool
        agent = OrchestratorAgent(
            db_pool=pool,
            anthropic_client=mock_anthropic,
            embedder=mock_embedder,

            langfuse=None,
        )
        rs = _make_risk_score(score=65.0)
        result = await agent.investigate(rs)
        assert result.get("skipped") is True
        mock_anthropic.messages.create.assert_not_called()

    @pytest.mark.asyncio
    async def test_activates_at_threshold(self, mock_db_pool, mock_anthropic, mock_embedder):
        from app.agent.orchestrator import OrchestratorAgent

        pool, conn = mock_db_pool
        # Single round: tool_use -> create_incident_report -> end
        incident_row = MagicMock()
        incident_row.__getitem__ = lambda s, k: {"id": 1, "created_at": datetime.now(timezone.utc)}[k]
        conn.fetchrow = AsyncMock(return_value=incident_row)

        report_block = _make_tool_use_block(
            "create_incident_report",
            {
                "service": "kafka-consumer",
                "severity": "HIGH",
                "summary": "Kafka lag cascade detected",
                "root_cause": "Consumer throughput dropped",
                "hypotheses": ["DB write latency"],
                "fix_steps": ["Scale replicas"],
            },
            tool_id="tid_ir",
        )
        mock_anthropic.messages.create = AsyncMock(
            return_value=_make_anthropic_response("tool_use", [report_block])
        )

        agent = OrchestratorAgent(
            db_pool=pool,
            anthropic_client=mock_anthropic,
            embedder=mock_embedder,
            langfuse=None,
        )
        rs = _make_risk_score(score=70.0)
        result = await agent.investigate(rs)
        assert result.get("skipped") is None
        assert mock_anthropic.messages.create.called

    @pytest.mark.asyncio
    async def test_custom_threshold_respected(
        self, mock_db_pool, mock_anthropic, mock_embedder
    ):
        from app.agent.orchestrator import OrchestratorAgent

        pool, _ = mock_db_pool
        agent = OrchestratorAgent(
            db_pool=pool,
            anthropic_client=mock_anthropic,
            embedder=mock_embedder,
            langfuse=None,
            activation_threshold=90.0,
        )
        rs = _make_risk_score(score=85.0)
        result = await agent.investigate(rs)
        assert result.get("skipped") is True


# ---------------------------------------------------------------------------
# Orchestrator round cap
# ---------------------------------------------------------------------------

class TestOrchestratorRoundCap:
    @pytest.mark.asyncio
    async def test_hard_cap_6_rounds(self, mock_db_pool, mock_anthropic, mock_embedder):
        """Agent stops after 6 rounds even if create_incident_report never called."""
        from app.agent.orchestrator import OrchestratorAgent

        pool, conn = mock_db_pool
        conn.fetch = AsyncMock(return_value=[])
        conn.fetchrow = AsyncMock(return_value=None)

        # Each call returns a tool_use that is NOT create_incident_report
        health_block = _make_tool_use_block(
            "get_service_health", {"service": "kafka-consumer"}, "tid_h"
        )
        tool_resp = _make_anthropic_response("tool_use", [health_block])

        # On round 6 (after nudge), return end_turn
        final_resp = _make_anthropic_response("end_turn", [_make_text_block("Analysis done.")])

        call_count = 0

        async def _create(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            # First 5 calls: return tool_use; 6th: end_turn
            if call_count < 6:
                return tool_resp
            return final_resp

        mock_anthropic.messages.create = _create

        agent = OrchestratorAgent(
            db_pool=pool,
            anthropic_client=mock_anthropic,
            embedder=mock_embedder,
            langfuse=None,
        )
        rs = _make_risk_score(score=80.0)
        result = await agent.investigate(rs)
        assert call_count <= 6
        assert "service" in result

    @pytest.mark.asyncio
    async def test_stops_after_create_incident_report(
        self, mock_db_pool, mock_anthropic, mock_embedder
    ):
        """Agent stops as soon as create_incident_report tool is called."""
        from app.agent.orchestrator import OrchestratorAgent

        pool, conn = mock_db_pool
        incident_row = MagicMock()
        incident_row.__getitem__ = lambda s, k: {"id": 42, "created_at": datetime.now(timezone.utc)}[k]
        conn.fetchrow = AsyncMock(return_value=incident_row)
        conn.execute = AsyncMock()

        report_block = _make_tool_use_block(
            "create_incident_report",
            {
                "service": "kafka-consumer",
                "severity": "HIGH",
                "summary": "Lag detected",
                "root_cause": "DB write latency",
                "hypotheses": ["DB slow"],
                "fix_steps": ["Scale up"],
            },
            tool_id="tid_rpt",
        )
        mock_anthropic.messages.create = AsyncMock(
            return_value=_make_anthropic_response("tool_use", [report_block])
        )

        agent = OrchestratorAgent(
            db_pool=pool,
            anthropic_client=mock_anthropic,
            embedder=mock_embedder,
            langfuse=None,
        )
        rs = _make_risk_score(score=85.0)
        result = await agent.investigate(rs)
        # Only one round needed
        assert mock_anthropic.messages.create.call_count == 1
        assert result["service"] == "kafka-consumer"


# ---------------------------------------------------------------------------
# Circuit breaker degraded mode
# ---------------------------------------------------------------------------

class TestOrchestratorDegradedMode:
    @pytest.mark.asyncio
    async def test_open_cb_returns_degraded_report(
        self, mock_db_pool, mock_anthropic, mock_embedder
    ):
        from app.agent.orchestrator import OrchestratorAgent

        pool, _ = mock_db_pool
        cb = CircuitBreaker(failure_threshold=1)
        cb.record_failure()  # opens immediately
        assert cb.is_open()

        agent = OrchestratorAgent(
            db_pool=pool,
            anthropic_client=mock_anthropic,
            embedder=mock_embedder,
            circuit_breaker=cb,
            langfuse=None,
        )
        rs = _make_risk_score(score=90.0)
        result = await agent.investigate(rs)

        assert result["degraded_mode"] is True
        assert result["incident_id"] is None
        assert "circuit breaker" in result["root_cause"].lower()
        mock_anthropic.messages.create.assert_not_called()

    @pytest.mark.asyncio
    async def test_llm_failure_opens_cb_and_returns_degraded(
        self, mock_db_pool, mock_anthropic, mock_embedder
    ):
        from app.agent.orchestrator import OrchestratorAgent

        pool, _ = mock_db_pool
        cb = CircuitBreaker(failure_threshold=1)

        mock_anthropic.messages.create = AsyncMock(side_effect=RuntimeError("API down"))

        agent = OrchestratorAgent(
            db_pool=pool,
            anthropic_client=mock_anthropic,
            embedder=mock_embedder,
            circuit_breaker=cb,
            langfuse=None,
        )
        rs = _make_risk_score(score=80.0)
        result = await agent.investigate(rs)

        assert cb.is_open()
        assert result["degraded_mode"] is True

    @pytest.mark.asyncio
    async def test_degraded_report_severity_mapping(
        self, mock_db_pool, mock_anthropic, mock_embedder
    ):
        from app.agent.orchestrator import OrchestratorAgent

        pool, _ = mock_db_pool
        cb = CircuitBreaker(failure_threshold=1)
        cb.record_failure()

        agent = OrchestratorAgent(
            db_pool=pool,
            anthropic_client=mock_anthropic,
            embedder=mock_embedder,
            circuit_breaker=cb,
            langfuse=None,
        )
        assert (await agent.investigate(_make_risk_score(90.0)))["severity"] == "HIGH"
        assert (await agent.investigate(_make_risk_score(70.0)))["severity"] == "MEDIUM"


# ---------------------------------------------------------------------------
# Tool function tests — require real DB
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_search_logs_empty(db_conn):
    from app.agent.tools import search_logs

    result = await search_logs(db_conn, "nonexistent-svc", "ERROR", 60)
    assert "logs" in result
    assert isinstance(result["logs"], list)


@pytest.mark.asyncio
async def test_search_logs_with_data(db_conn):
    from app.agent.tools import search_logs

    # Insert a test log signal
    await db_conn.execute(
        """
        INSERT INTO signals (ts, service, signal_type, level, message, scenario)
        VALUES (NOW(), 'test-svc', 'log', 'ERROR', 'connection timeout occurred', 'test_scenario')
        """
    )
    result = await search_logs(db_conn, "test-svc", "timeout", 5)
    assert any("timeout" in (r.get("message") or "") for r in result["logs"])


@pytest.mark.asyncio
async def test_get_pipeline_metrics_empty(db_conn):
    from app.agent.tools import get_pipeline_metrics

    result = await get_pipeline_metrics(db_conn, "nonexistent-svc", 60)
    assert result["snapshot_count"] == 0


@pytest.mark.asyncio
async def test_get_pipeline_metrics_with_data(db_conn):
    from app.agent.tools import get_pipeline_metrics

    metrics = {
        "throughput": 800.0, "consumer_lag": 500.0, "error_rate": 0.01,
        "memory_pct": 45.0, "cpu_pct": 30.0, "disk_pct": 40.0,
    }
    for i in range(6):
        await db_conn.execute(
            """
            INSERT INTO signals (ts, service, signal_type, metrics, scenario)
            VALUES (NOW() - ($1 || ' seconds')::interval, 'test-svc', 'metric', $2::jsonb, 'test_scenario')
            """,
            str(i * 30),
            json.dumps(metrics),
        )
    result = await get_pipeline_metrics(db_conn, "test-svc", 10)
    assert result["snapshot_count"] > 0
    assert "trends" in result


@pytest.mark.asyncio
async def test_get_service_health_empty(db_conn):
    from app.agent.tools import get_service_health

    result = await get_service_health(db_conn, "nonexistent-svc")
    assert "error" in result


@pytest.mark.asyncio
async def test_get_service_health_with_data(db_conn):
    from app.agent.tools import get_service_health

    metrics = {
        "throughput": 900.0, "consumer_lag": 200.0, "error_rate": 0.003,
        "memory_pct": 50.0, "cpu_pct": 35.0, "disk_pct": 42.0,
        "pod_restarts": 2,
    }
    await db_conn.execute(
        """
        INSERT INTO signals (ts, service, signal_type, metrics, scenario)
        VALUES (NOW(), 'health-svc', 'metric', $1::jsonb, 'test_scenario')
        """,
        json.dumps(metrics),
    )
    result = await get_service_health(db_conn, "health-svc")
    assert result["pod_restarts"] == 2
    assert result["memory_pct"] == 50.0


@pytest.mark.asyncio
async def test_query_runbook_kb_no_embedder(db_conn):
    from app.agent.tools import query_runbook_kb

    result = await query_runbook_kb(db_conn, "kafka lag", embedder=None)
    assert "error" in result


@pytest.mark.asyncio
async def test_query_runbook_kb_with_mock_embedder(db_conn, mock_embedder):
    from app.agent.tools import query_runbook_kb

    # Insert a test chunk with a 768-dim embedding
    embedding_str = "[" + ",".join(["0.1"] * 768) + "]"
    await db_conn.execute(
        """
        INSERT INTO chunks (runbook, section, content, embedding, content_hash)
        VALUES ('test_runbook.md', 'Test Section', 'kafka lag consumer scaling steps',
                $1::vector, $2)
        ON CONFLICT (content_hash) DO NOTHING
        """,
        embedding_str,
        hashlib.sha256(b"kafka lag consumer scaling steps").hexdigest(),
    )

    result = await query_runbook_kb(
        db_conn, "kafka consumer lag growing", embedder=mock_embedder
    )
    assert "results" in result


@pytest.mark.asyncio
async def test_create_incident_report(db_conn):
    from app.agent.tools import create_incident_report

    result = await create_incident_report(
        db=db_conn,
        service="kafka-consumer",
        severity="HIGH",
        summary="Kafka lag cascade test incident",
        root_cause="DB write latency caused consumer stall",
        hypotheses=["DB slow", "Network issue"],
        fix_steps=["Scale replicas", "Check DB connection pool"],
        risk_score=87.5,
        scenario="test_scenario",
    )
    assert "incident_id" in result
    assert result["incident_id"] is not None
    assert result["service"] == "kafka-consumer"


# ---------------------------------------------------------------------------
# Runbook ingester tests — require real DB
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_runbook_ingester_chunks_correctly(
    db_conn, mock_embedder, tmp_path
):
    from app.agent.runbook_ingester import RunbookIngester, _split_into_sections, _chunk_text

    # Unit test chunking without DB
    text = "# Runbook\n\n## Symptoms\n\nHigh error rate.\n\n## Root Cause\n\nDB connection pool exhausted."
    sections = _split_into_sections(text)
    assert len(sections) == 2
    assert sections[0][0] == "Symptoms"
    assert sections[1][0] == "Root Cause"


@pytest.mark.asyncio
async def test_runbook_ingester_ingest_and_idempotent(
    db_conn, mock_embedder, tmp_path
):
    from app.agent.runbook_ingester import RunbookIngester

    # Create a small test runbook
    runbook = tmp_path / "test_runbook.md"
    runbook.write_text(
        "# Test Runbook\n\n## Symptoms\n\nService is slow.\n\n## Fix\n\nRestart the pod.",
        encoding="utf-8",
    )

    ingester = RunbookIngester(db=db_conn, embedder=mock_embedder)

    results1 = await ingester.ingest_all(tmp_path)
    count_after_first = sum(results1.values())
    assert count_after_first > 0

    # Second run should insert 0 new chunks (idempotent)
    results2 = await ingester.ingest_all(tmp_path)
    count_after_second = sum(results2.values())
    assert count_after_second == 0


@pytest.mark.asyncio
async def test_runbook_ingester_section_overlap(tmp_path):
    from app.agent.runbook_ingester import _chunk_text

    long_text = "A" * 3000  # exceeds 2048 char limit
    chunks = _chunk_text(long_text)
    assert len(chunks) >= 2


# ---------------------------------------------------------------------------
# Tool schemas validation
# ---------------------------------------------------------------------------

class TestToolSchemas:
    def test_all_5_tools_defined(self):
        names = {t["name"] for t in TOOL_SCHEMAS}
        assert names == {
            "search_logs",
            "get_pipeline_metrics",
            "query_runbook_kb",
            "get_service_health",
            "create_incident_report",
        }

    def test_all_schemas_have_required_fields(self):
        for schema in TOOL_SCHEMAS:
            assert "name" in schema
            assert "description" in schema
            assert "input_schema" in schema
            assert schema["input_schema"]["type"] == "object"
            assert "required" in schema["input_schema"]

    def test_create_incident_report_enum_severity(self):
        schema = next(t for t in TOOL_SCHEMAS if t["name"] == "create_incident_report")
        sev_schema = schema["input_schema"]["properties"]["severity"]
        assert "enum" in sev_schema
        assert set(sev_schema["enum"]) == {"CRITICAL", "HIGH", "MEDIUM", "LOW"}


# ---------------------------------------------------------------------------
# Prompts smoke tests
# ---------------------------------------------------------------------------

class TestPrompts:
    def test_system_prompt_mentions_tools(self):
        for tool in ["search_logs", "get_pipeline_metrics", "query_runbook_kb",
                     "get_service_health", "create_incident_report"]:
            assert tool in SYSTEM_PROMPT

    def test_system_prompt_mentions_create_incident_report_required(self):
        assert "create_incident_report" in SYSTEM_PROMPT
        assert "MUST" in SYSTEM_PROMPT or "must" in SYSTEM_PROMPT.lower()

    def test_degraded_prompt_exists(self):
        assert len(DEGRADED_PROMPT) > 50

    def test_prompt_version_defined(self):
        assert PROMPT_VERSION.startswith("v")
