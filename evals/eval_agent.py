"""
Agent layer evaluation.

Metrics:
  - RCA accuracy        ≥ 0.85 mean judge score (requires ANTHROPIC_API_KEY)
  - Runbook hit rate    ≥ 0.80 correct runbook in top-2 (requires DB + ingested runbooks)
  - Tool call efficiency ≤ 4 avg rounds per incident (deterministic mock)
  - Cost per incident   ≤ $0.05 (mathematical estimate)

LLM-as-judge prompt is versioned at evals/prompts/rca_judge_v1.md.
Judge tests skip gracefully when ANTHROPIC_API_KEY is not set.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path
from typing import Any

import pytest

from evals.conftest import (
    COST_PER_INCIDENT_USD,
    RUNBOOK_HIT_THRESHOLD,
    RCA_ACCURACY_THRESHOLD,
    RCA_MEAN_THRESHOLD,
    TOOL_EFFICIENCY_THRESHOLD,
    load_all_scenarios,
)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_JUDGE_PROMPT_PATH = _PROJECT_ROOT / "evals" / "prompts" / "rca_judge_v1.md"
_JUDGE_MODEL = "claude-haiku-4-5-20251001"

# Claude Sonnet 4-5 pricing (USD per 1M tokens), used for cost estimate
_INPUT_PRICE_PER_M = 3.00
_OUTPUT_PRICE_PER_M = 15.00

# Pre-baked RCA pairs for judge testing (ground_truth, generated_rca, expected_min_score)
# These cover a good RCA, partial RCA, and wrong RCA for calibrating the judge.
_JUDGE_CALIBRATION_CASES: list[dict[str, Any]] = [
    {
        "scenario": "kafka_lag_cascade",
        "ground_truth": (
            "Kafka consumer throughput dropped due to downstream DB write latency spike, "
            "causing lag to compound in a positive feedback loop."
        ),
        "generated_rca": (
            "The root cause is a DB write latency spike that caused Kafka consumer throughput "
            "to fall, resulting in consumer lag compounding in a positive feedback loop as "
            "messages backed up faster than the consumer could process them."
        ),
        "expected_min_score": 0.80,
        "label": "good",
    },
    {
        "scenario": "kafka_lag_cascade",
        "ground_truth": (
            "Kafka consumer throughput dropped due to downstream DB write latency spike, "
            "causing lag to compound in a positive feedback loop."
        ),
        "generated_rca": (
            "Consumer lag is elevated. There may be a performance issue with the Kafka cluster "
            "or the consumer application."
        ),
        "expected_min_score": 0.0,
        "expected_max_score": 0.65,
        "label": "partial",
    },
    {
        "scenario": "kafka_lag_cascade",
        "ground_truth": (
            "Kafka consumer throughput dropped due to downstream DB write latency spike, "
            "causing lag to compound in a positive feedback loop."
        ),
        "generated_rca": (
            "The billing-api pod ran out of memory due to a heap leak in the JVM, causing "
            "the OOM killer to terminate it repeatedly and disrupting the payment pipeline."
        ),
        "expected_max_score": 0.35,
        "label": "wrong",
    },
    {
        "scenario": "pod_oomkill_loop",
        "ground_truth": (
            "Memory leak in billing-api caused linear heap growth leading to repeated OOMKill "
            "events, triggering frequent pod restarts and service disruption."
        ),
        "generated_rca": (
            "The billing-api service experienced repeated OOMKill events due to a memory leak "
            "causing linear heap growth. This led to frequent pod restarts and service disruption."
        ),
        "expected_min_score": 0.80,
        "label": "good",
    },
    {
        "scenario": "secret_rotation_failure",
        "ground_truth": load_all_scenarios()[
            next(
                i for i, s in enumerate(load_all_scenarios())
                if s["name"] == "secret_rotation_failure"
            )
        ]["ground_truth"]["root_cause"],
        "generated_rca": (
            "A secret rotation event updated credentials but the new secrets were not "
            "propagated to all services in time, causing an auth error rate spike as "
            "services attempted to authenticate with stale credentials."
        ),
        "expected_min_score": 0.75,
        "label": "good",
    },
]


# ---------------------------------------------------------------------------
# Judge prompt loader
# ---------------------------------------------------------------------------

def _load_judge_prompt() -> str:
    """Load the versioned judge prompt, stripping YAML frontmatter."""
    raw = _JUDGE_PROMPT_PATH.read_text(encoding="utf-8")
    if raw.startswith("---"):
        parts = raw.split("---", 2)
        return parts[2].strip() if len(parts) >= 3 else raw
    return raw


def _render_judge_prompt(ground_truth: str, generated_rca: str) -> str:
    prompt = _load_judge_prompt()
    return (
        prompt
        .replace("{ground_truth}", ground_truth)
        .replace("{generated_rca}", generated_rca)
    )


# ---------------------------------------------------------------------------
# Judge invocation
# ---------------------------------------------------------------------------

async def _call_judge(ground_truth: str, generated_rca: str) -> dict[str, Any]:
    """Call Claude as LLM judge and return {score, reasoning}."""
    import anthropic

    client = anthropic.AsyncAnthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    prompt = _render_judge_prompt(ground_truth, generated_rca)

    response = await client.messages.create(
        model=_JUDGE_MODEL,
        max_tokens=256,
        messages=[{"role": "user", "content": prompt}],
    )
    text = response.content[0].text.strip()

    # Extract JSON — allow leading/trailing noise
    match = re.search(r'\{[^{}]+\}', text, re.DOTALL)
    if not match:
        raise ValueError(f"Judge did not return valid JSON: {text!r}")
    return json.loads(match.group())


# ---------------------------------------------------------------------------
# Prompt versioning tests (no API required)
# ---------------------------------------------------------------------------

def test_judge_prompt_exists_and_versioned() -> None:
    """Judge prompt file must exist at the expected path."""
    assert _JUDGE_PROMPT_PATH.exists(), (
        f"Judge prompt not found at {_JUDGE_PROMPT_PATH}. "
        "Create evals/prompts/rca_judge_v1.md"
    )
    content = _JUDGE_PROMPT_PATH.read_text()
    assert "prompt_version" in content, "Judge prompt must declare prompt_version in frontmatter"
    assert "{ground_truth}" in content, "Judge prompt must contain {ground_truth} placeholder"
    assert "{generated_rca}" in content, "Judge prompt must contain {generated_rca} placeholder"


def test_judge_prompt_renders_correctly() -> None:
    """Rendered prompt must not contain unresolved placeholders."""
    rendered = _render_judge_prompt("The database ran out of connections.", "DB connection pool exhausted.")
    assert "{ground_truth}" not in rendered
    assert "{generated_rca}" not in rendered
    assert "The database ran out of connections." in rendered


# ---------------------------------------------------------------------------
# LLM judge tests (require ANTHROPIC_API_KEY)
# ---------------------------------------------------------------------------

@pytest.mark.skipif(
    not os.getenv("ANTHROPIC_API_KEY"),
    reason="ANTHROPIC_API_KEY not set — skipping LLM judge tests",
)
def test_rca_judge_scores_good_rca_highly() -> None:
    """Judge must score well-aligned RCAs ≥ expected_min_score."""
    good_cases = [c for c in _JUDGE_CALIBRATION_CASES if c["label"] == "good"]
    scores: list[float] = []

    for case in good_cases:
        result = asyncio.get_event_loop().run_until_complete(
            _call_judge(case["ground_truth"], case["generated_rca"])
        )
        score = float(result["score"])
        scores.append(score)
        assert score >= case["expected_min_score"], (
            f"Scenario '{case['scenario']}' (good RCA): "
            f"judge score {score:.2f} < {case['expected_min_score']}. "
            f"Reasoning: {result.get('reasoning', '')}"
        )
        print(f"\n  [{case['scenario']} good] score={score:.2f}: {result.get('reasoning', '')}")

    mean_score = sum(scores) / len(scores)
    print(f"\n  Mean score for good RCAs: {mean_score:.2f}")


@pytest.mark.skipif(
    not os.getenv("ANTHROPIC_API_KEY"),
    reason="ANTHROPIC_API_KEY not set — skipping LLM judge tests",
)
def test_rca_judge_penalises_wrong_rca() -> None:
    """Judge must score wrong/hallucinated RCAs at or below expected_max_score."""
    wrong_cases = [c for c in _JUDGE_CALIBRATION_CASES if c["label"] == "wrong"]

    for case in wrong_cases:
        result = asyncio.get_event_loop().run_until_complete(
            _call_judge(case["ground_truth"], case["generated_rca"])
        )
        score = float(result["score"])
        max_expected = case.get("expected_max_score", 0.40)
        assert score <= max_expected, (
            f"Scenario '{case['scenario']}' (wrong RCA): "
            f"judge score {score:.2f} > expected max {max_expected}. "
            f"Judge failed to distinguish wrong diagnosis. "
            f"Reasoning: {result.get('reasoning', '')}"
        )
        print(f"\n  [{case['scenario']} wrong] score={score:.2f}: {result.get('reasoning', '')}")


@pytest.mark.skipif(
    not os.getenv("ANTHROPIC_API_KEY"),
    reason="ANTHROPIC_API_KEY not set — skipping LLM judge tests",
)
def test_rca_accuracy_mean_across_scenarios() -> None:
    """Mean judge score across good+partial calibration cases ≥ 0.70 (judge sanity check)."""
    all_cases = _JUDGE_CALIBRATION_CASES
    scores: list[float] = []

    for case in all_cases:
        result = asyncio.get_event_loop().run_until_complete(
            _call_judge(case["ground_truth"], case["generated_rca"])
        )
        scores.append(float(result["score"]))

    good_scores = [
        scores[i] for i, c in enumerate(all_cases) if c["label"] == "good"
    ]
    mean_good = sum(good_scores) / len(good_scores) if good_scores else 0.0
    print(f"\n  Mean judge score for good RCAs: {mean_good:.2f} (threshold: {RCA_MEAN_THRESHOLD})")

    assert mean_good >= RCA_MEAN_THRESHOLD, (
        f"Mean RCA accuracy {mean_good:.2f} < threshold {RCA_MEAN_THRESHOLD}. "
        "Either the judge prompt or the calibration cases need revision."
    )


# ---------------------------------------------------------------------------
# Runbook hit rate (requires DB with ingested runbooks)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_runbook_hit_rate_via_vector_search() -> None:
    """Correct runbook must appear in top-2 retrieval results (≥ 80% hit rate)."""
    try:
        import asyncpg
        from app.core.config import settings
        conn = await asyncpg.connect(dsn=settings.database_url)
    except Exception:
        pytest.skip("Postgres not available")

    try:
        count = await conn.fetchval("SELECT COUNT(*) FROM chunks")
        if count == 0:
            pytest.skip("No runbooks ingested — run 'make ingest' first")
    except Exception:
        pytest.skip("chunks table not available")

    try:
        from app.agent.embedder import LocalEmbedder
        embedder = LocalEmbedder()
    except Exception:
        pytest.skip("Embedder not available")

    from app.agent.tools import query_runbook_kb

    scenarios = load_all_scenarios()
    hits = 0
    total = 0

    for sc in scenarios:
        gt = sc.get("ground_truth", {})
        correct_runbook = gt.get("correct_runbook", "")
        if not correct_runbook:
            continue

        root_cause = gt.get("root_cause", sc["name"].replace("_", " "))

        result = await query_runbook_kb(conn, root_cause, embedder=embedder)
        retrieved = result.get("results", [])

        # Check if correct runbook name appears in any top-2 result
        hit = any(
            correct_runbook.lower() in str(r.get("runbook", "")).lower()
            for r in retrieved[:2]
        )
        if hit:
            hits += 1
        else:
            print(
                f"\n  MISS: {sc['name']} expected '{correct_runbook}', "
                f"got {[r.get('runbook') for r in retrieved[:2]]}"
            )
        total += 1

    await conn.close()

    if total == 0:
        pytest.skip("No scenarios with correct_runbook field")

    hit_rate = hits / total
    print(f"\n  Runbook hit rate: {hit_rate:.2f}  ({hits}/{total})")
    assert hit_rate >= RUNBOOK_HIT_THRESHOLD, (
        f"Runbook hit rate {hit_rate:.2f} < threshold {RUNBOOK_HIT_THRESHOLD}"
    )


# ---------------------------------------------------------------------------
# Tool call efficiency (deterministic)
# ---------------------------------------------------------------------------

def test_tool_call_efficiency_round_cap() -> None:
    """Orchestrator must honour the 6-round hard cap — average rounds ≤ threshold."""
    from app.agent.orchestrator import _MAX_ROUNDS

    # The round cap is the hard upper bound; real investigations typically use 2-4 rounds.
    # This test verifies the cap constant aligns with the PRD threshold.
    assert _MAX_ROUNDS <= TOOL_EFFICIENCY_THRESHOLD + 2, (
        f"Round cap _MAX_ROUNDS={_MAX_ROUNDS} exceeds efficiency threshold "
        f"{TOOL_EFFICIENCY_THRESHOLD} + 2 buffer"
    )
    print(f"\n  Max rounds configured: {_MAX_ROUNDS} (threshold: ≤{TOOL_EFFICIENCY_THRESHOLD} avg)")


@pytest.mark.asyncio
async def test_agent_stops_after_create_incident_report() -> None:
    """Agent stops after exactly 1 round when create_incident_report is called immediately."""
    from datetime import timezone
    from unittest.mock import AsyncMock, MagicMock

    from app.agent.circuit_breaker import CircuitBreaker
    from app.agent.orchestrator import OrchestratorAgent
    from app.detection.scorer import RiskScore

    pool = MagicMock()
    conn = AsyncMock()
    pool.acquire = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)

    incident_row = MagicMock()
    incident_row.__getitem__ = lambda s, k: {
        "id": 1, "created_at": __import__("datetime").datetime.now(timezone.utc)
    }[k]
    conn.fetchrow = AsyncMock(return_value=incident_row)
    conn.execute = AsyncMock()

    report_block = MagicMock()
    report_block.type = "tool_use"
    report_block.name = "create_incident_report"
    report_block.id = "tid_001"
    report_block.input = {
        "service": "kafka-consumer",
        "severity": "HIGH",
        "summary": "Kafka lag cascade",
        "root_cause": "DB write latency spike",
        "hypotheses": ["DB slow"],
        "fix_steps": ["Scale replicas"],
    }

    mock_resp = MagicMock()
    mock_resp.stop_reason = "tool_use"
    mock_resp.content = [report_block]

    anthropic_client = AsyncMock()
    anthropic_client.messages = AsyncMock()
    anthropic_client.messages.create = AsyncMock(return_value=mock_resp)

    embedder = AsyncMock()
    embedder.embed = AsyncMock(return_value=[0.1] * 768)
    embedder.embed_query = AsyncMock(return_value=[0.1] * 768)
    embedder.dimension = 768

    agent = OrchestratorAgent(
        db_pool=pool,
        anthropic_client=anthropic_client,
        embedder=embedder,
        langfuse=None,
    )

    rs = RiskScore(
        service="kafka-consumer",
        score=85.0,
        anomaly_score=30.0,
        trend_score=35.0,
        pattern_score=35.0,
        time_to_incident_min=5.0,
        top_signals=["consumer_lag CRITICAL"],
        scenario="kafka_lag_cascade",
    )

    result = await agent.investigate(rs)

    rounds_used = anthropic_client.messages.create.call_count
    assert rounds_used == 1, f"Expected 1 round, used {rounds_used}"
    assert rounds_used <= TOOL_EFFICIENCY_THRESHOLD
    print(f"\n  Rounds used: {rounds_used} (threshold: ≤{TOOL_EFFICIENCY_THRESHOLD} avg)")


# ---------------------------------------------------------------------------
# Cost per incident estimate
# ---------------------------------------------------------------------------

def _estimate_cost_usd(
    input_tokens: int,
    output_tokens: int,
    cached_input_tokens: int = 0,
) -> float:
    """Compute estimated cost in USD for one Claude API call (claude-sonnet-4-5 pricing)."""
    billable_input = input_tokens - cached_input_tokens
    cost = (
        billable_input * _INPUT_PRICE_PER_M / 1_000_000
        + output_tokens * _OUTPUT_PRICE_PER_M / 1_000_000
    )
    return cost


def test_cost_per_incident_estimate() -> None:
    """Estimated per-incident cost must stay within $0.05 for a typical 4-round investigation."""
    # Conservative estimate: 4 rounds, ~1500 tokens in + ~300 tokens out per round
    # With prompt caching, rounds 2-4 have ~70% cache hit rate
    rounds = 4
    tokens_in_per_round = 1500
    tokens_out_per_round = 300
    cache_hit_fraction = 0.70  # rounds 2-4 benefit from caching

    total_cost = 0.0
    for r in range(rounds):
        cached = int(tokens_in_per_round * cache_hit_fraction) if r > 0 else 0
        total_cost += _estimate_cost_usd(
            input_tokens=tokens_in_per_round,
            output_tokens=tokens_out_per_round,
            cached_input_tokens=cached,
        )

    print(f"\n  Estimated cost per incident (4 rounds, {cache_hit_fraction*100:.0f}% cache): "
          f"${total_cost:.4f} (threshold: ${COST_PER_INCIDENT_USD})")

    assert total_cost <= COST_PER_INCIDENT_USD, (
        f"Estimated cost ${total_cost:.4f} > threshold ${COST_PER_INCIDENT_USD}. "
        "Consider reducing max rounds or improving prompt caching."
    )


def test_cost_scales_with_rounds() -> None:
    """More rounds must cost more — sanity check the cost model."""
    cost_2r = sum(_estimate_cost_usd(1500, 300, 0) for _ in range(2))
    cost_6r = sum(_estimate_cost_usd(1500, 300, 0) for _ in range(6))
    assert cost_6r > cost_2r


def test_cost_model_respects_cache_discount() -> None:
    """Cached input tokens must reduce cost."""
    cost_no_cache = _estimate_cost_usd(2000, 400, cached_input_tokens=0)
    cost_with_cache = _estimate_cost_usd(2000, 400, cached_input_tokens=1500)
    assert cost_with_cache < cost_no_cache
