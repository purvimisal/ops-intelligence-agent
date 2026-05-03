"""
Shared fixtures and helpers for the eval suite.

All eval files import from here.  No API calls, no DB — pure in-memory helpers
for detection pipeline setup and scenario loading.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from app.detection.patterns import PatternMatcher
from app.detection.scorer import RiskScore, RiskScorer
from app.detection.trend import TrendAnalyser
from app.detection.window import AnomalyEvent, SlidingWindowDetector
from app.ingestion.base import Signal

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_SCENARIOS_DIR = _PROJECT_ROOT / "scenarios"
_METRIC_KEYS = [
    "throughput", "consumer_lag", "error_rate",
    "memory_pct", "cpu_pct", "disk_pct",
]

# ---------------------------------------------------------------------------
# Thresholds (mirrors PRD eval framework)
# ---------------------------------------------------------------------------

PRECISION_THRESHOLD = 0.85
RECALL_THRESHOLD = 0.90
FPR_THRESHOLD = 0.10
MTTD_SEC_THRESHOLD = 60.0      # processing latency, not lead-time
RCA_ACCURACY_THRESHOLD = 0.75  # per-incident judge score to count as "passed"
RCA_MEAN_THRESHOLD = 0.85      # mean accuracy across scenarios
RUNBOOK_HIT_THRESHOLD = 0.80
TOOL_EFFICIENCY_THRESHOLD = 4.0   # avg tool calls (rounds) per incident
COST_PER_INCIDENT_USD = 0.05
TOIL_REDUCTION_THRESHOLD = 0.80
PREEMPTIVE_SUCCESS_THRESHOLD = 0.60


# ---------------------------------------------------------------------------
# Scenario loading
# ---------------------------------------------------------------------------

def load_all_scenarios() -> list[dict[str, Any]]:
    scenarios = []
    for fp in sorted(_SCENARIOS_DIR.glob("*.json")):
        with open(fp) as f:
            scenarios.append(json.load(f))
    return scenarios


def scenario_to_signals(scenario: dict[str, Any]) -> list[Signal]:
    signals = []
    for step in scenario["signal_shape"]:
        metrics = {k: step.get(k, 0.0) for k in _METRIC_KEYS}
        metrics["pod_restarts"] = step.get("pod_restarts", 0)
        signals.append(Signal(
            signal_type="metric",
            service=step["service"],
            ts=datetime.fromisoformat(step["timestamp"]),
            metrics=metrics,
            scenario=scenario["name"],
        ))
    return signals


def make_clean_signals(service: str, n: int = 30) -> list[Signal]:
    """Return n normal-metric signals that should not trigger any anomaly."""
    from datetime import timezone, timedelta
    base_ts = datetime(2026, 4, 1, 0, 0, 0, tzinfo=timezone.utc)
    signals = []
    for i in range(n):
        signals.append(Signal(
            signal_type="metric",
            service=service,
            ts=base_ts + timedelta(seconds=i * 30),
            metrics={
                "error_rate": 0.010,
                "consumer_lag": 400.0,
                "memory_pct": 48.0,
                "cpu_pct": 38.0,
                "disk_pct": 40.0,
                "throughput": 900.0,
                "pod_restarts": 0,
            },
        ))
    return signals


# ---------------------------------------------------------------------------
# Detection replay helper
# ---------------------------------------------------------------------------

def replay_for_anomalies(
    signals: list[Signal],
) -> tuple[bool, list[RiskScore]]:
    """
    Feed signals through the detection pipeline.

    Returns:
        detected: True if any AnomalyEvent fires at any step
        risk_scores: list of RiskScore, one per signal
    """
    wd = SlidingWindowDetector()
    ta = TrendAnalyser()
    pm = PatternMatcher()
    scorer = RiskScorer(wd, ta, pm)

    detected = False
    risk_scores: list[RiskScore] = []

    for sig in signals:
        wd.add_signal(sig)
        ta.add_signal(sig)
        pm.add_signal(sig)

        events = wd.check(sig.service)
        if events:
            detected = True

        rs = scorer.score(sig.service, scenario=sig.scenario)
        risk_scores.append(rs)

    return detected, risk_scores


# ---------------------------------------------------------------------------
# Pytest fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def all_scenarios() -> list[dict[str, Any]]:
    return load_all_scenarios()


@pytest.fixture(scope="session")
def api_key() -> str | None:
    return os.getenv("ANTHROPIC_API_KEY", "")


@pytest.fixture(scope="session")
def has_api_key(api_key) -> bool:
    return bool(api_key)
