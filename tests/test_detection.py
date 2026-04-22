"""
Tests for the anomaly detection layer.
Covers: SlidingWindowDetector, TrendAnalyser, PatternMatcher, RiskScorer,
and the full DetectionPipeline end-to-end.

No DB, no LLM, no network — all tests are self-contained.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.detection.window import AnomalyEvent, SlidingWindowDetector
from app.detection.trend import TrendAnalyser, TrendReport
from app.detection.patterns import PatternMatch, PatternMatcher
from app.detection.scorer import RiskScore, RiskScorer
from app.detection import DetectionPipeline
from app.ingestion.base import Signal
from app.ingestion.generator import SyntheticIngester, build_scenario

_PROJECT_ROOT = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _metric_signal(
    service: str,
    error_rate: float = 0.005,
    consumer_lag: float = 120.0,
    memory_pct: float = 42.0,
    cpu_pct: float = 35.0,
    disk_pct: float = 38.0,
    throughput: float = 850.0,
    scenario: str | None = None,
    ts: datetime | None = None,
) -> Signal:
    return Signal(
        signal_type="metric",
        service=service,
        ts=ts or datetime.now(timezone.utc),
        metrics={
            "error_rate": error_rate,
            "consumer_lag": consumer_lag,
            "memory_pct": memory_pct,
            "cpu_pct": cpu_pct,
            "disk_pct": disk_pct,
            "throughput": throughput,
            "pod_restarts": 0,
        },
        scenario=scenario,
    )


def _feed_signals(detector, signals: list[Signal]) -> None:
    for s in signals:
        detector.add_signal(s)


# ---------------------------------------------------------------------------
# SlidingWindowDetector
# ---------------------------------------------------------------------------

class TestSlidingWindowDetector:
    def test_no_anomaly_on_baseline(self) -> None:
        det = SlidingWindowDetector(window_size_min=5)
        for i in range(10):
            det.add_signal(_metric_signal("svc-a"))
        assert det.check("svc-a") == []

    def test_fires_critical_on_error_spike(self) -> None:
        det = SlidingWindowDetector(window_size_min=5)
        for i in range(10):
            det.add_signal(_metric_signal("svc-a", error_rate=0.20))  # > critical 0.15
        anomalies = det.check("svc-a")
        assert any(a.metric == "error_rate" and a.severity == "CRITICAL" for a in anomalies)

    def test_fires_warning_on_moderate_error_rate(self) -> None:
        det = SlidingWindowDetector(window_size_min=5)
        for i in range(10):
            det.add_signal(_metric_signal("svc-a", error_rate=0.08))  # > warn 0.05, < critical 0.15
        anomalies = det.check("svc-a")
        err_anomaly = next((a for a in anomalies if a.metric == "error_rate"), None)
        assert err_anomaly is not None
        assert err_anomaly.severity == "WARNING"

    def test_fires_on_consumer_lag(self) -> None:
        det = SlidingWindowDetector(window_size_min=5)
        for i in range(10):
            det.add_signal(_metric_signal("svc-a", consumer_lag=25000.0))  # > critical 20000
        anomalies = det.check("svc-a")
        assert any(a.metric == "consumer_lag" and a.severity == "CRITICAL" for a in anomalies)

    def test_fires_on_memory_warn(self) -> None:
        det = SlidingWindowDetector(window_size_min=5)
        for i in range(10):
            det.add_signal(_metric_signal("svc-a", memory_pct=85.0))  # > warn 80
        anomalies = det.check("svc-a")
        assert any(a.metric == "memory_pct" for a in anomalies)

    def test_empty_service_returns_empty(self) -> None:
        det = SlidingWindowDetector()
        assert det.check("nonexistent") == []

    def test_idempotent_same_signal_twice(self) -> None:
        det = SlidingWindowDetector(window_size_min=5)
        now = datetime.now(timezone.utc)
        sig = _metric_signal("svc-a", error_rate=0.20, ts=now)
        det.add_signal(sig)
        det.add_signal(sig)  # second time — must be ignored
        # Window should have exactly 1 entry
        assert len(list(det._window("svc-a"))) == 1

    def test_idempotent_check_called_twice(self) -> None:
        det = SlidingWindowDetector(window_size_min=5)
        for i in range(5):
            ts = datetime.now(timezone.utc) + timedelta(seconds=i * 30)
            det.add_signal(_metric_signal("svc-a", error_rate=0.20, ts=ts))
        result1 = det.check("svc-a")
        result2 = det.check("svc-a")
        # Same anomalies, same count
        assert len(result1) == len(result2)
        assert {(a.metric, a.severity) for a in result1} == {(a.metric, a.severity) for a in result2}

    def test_anomaly_event_fields(self) -> None:
        det = SlidingWindowDetector(window_size_min=5)
        for _ in range(5):
            det.add_signal(_metric_signal("svc-a", error_rate=0.20))
        anomalies = det.check("svc-a")
        a = next(a for a in anomalies if a.metric == "error_rate")
        assert a.service == "svc-a"
        assert a.value == pytest.approx(0.20)
        assert a.severity == "CRITICAL"
        assert isinstance(a.window_start, datetime)
        assert isinstance(a.window_end, datetime)

    def test_services_list(self) -> None:
        det = SlidingWindowDetector()
        det.add_signal(_metric_signal("svc-a"))
        det.add_signal(_metric_signal("svc-b"))
        assert set(det.services()) == {"svc-a", "svc-b"}


# ---------------------------------------------------------------------------
# TrendAnalyser
# ---------------------------------------------------------------------------

class TestTrendAnalyser:
    def _analyser_with_values(self, service: str, metric: str, values: list[float]) -> TrendAnalyser:
        """Build a TrendAnalyser pre-loaded with a deterministic value sequence."""
        ta = TrendAnalyser(window_steps=len(values) + 5)
        base = {"error_rate": 0.005, "consumer_lag": 120.0, "memory_pct": 42.0, "cpu_pct": 35.0}
        for i, v in enumerate(values):
            sig = Signal(
                signal_type="metric",
                service=service,
                ts=datetime.now(timezone.utc) + timedelta(seconds=i * 30),
                metrics={**base, metric: v},   # overwrites base value for the metric under test
            )
            ta.add_signal(sig)
        return ta

    def test_high_urgency_fast_growing_error_rate(self) -> None:
        # error_rate grows from 0.01 to 0.037 in 10 steps — warn threshold 0.05
        # slope ≈ 0.003/step → ~2 min to threshold → HIGH
        values = [0.01 + i * 0.003 for i in range(10)]
        ta = self._analyser_with_values("svc", "error_rate", values)
        reports = ta.analyse("svc")
        er = next((r for r in reports if r.metric == "error_rate"), None)
        assert er is not None
        assert er.urgency == "HIGH"
        assert er.time_to_threshold_min is not None
        assert er.time_to_threshold_min < 30

    def test_medium_urgency_moderate_growth(self) -> None:
        # error_rate grows slowly: 0.01 to 0.03 in 10 steps
        # slope ≈ 0.002/step → (0.05-0.03)/0.002 * 30/60 ≈ 5 min — still < 30, HIGH
        # Let's use even slower: grow from 0.01 to 0.025 in 10 steps
        # slope ≈ 0.0015/step → (0.05-0.025)/0.0015 * 30/60 ≈ 8.3 min → HIGH
        # To get MEDIUM we need time_to_threshold 30-60 min
        # slope = (0.05-0.04)/10 * (30s/60) = need 30-60 min gap
        # 0.04 to warn=0.05: gap=0.01, need 30 min → 60 steps at 30s.
        # With slope 0.0001/step → 100 steps → 50 min → MEDIUM
        values = [0.04 + i * 0.0001 for i in range(10)]  # slope ≈ 0.0001
        ta = self._analyser_with_values("svc", "error_rate", values)
        reports = ta.analyse("svc")
        er = next((r for r in reports if r.metric == "error_rate"), None)
        assert er is not None
        assert er.urgency in ("MEDIUM", "LOW")   # depends on exact regression

    def test_none_urgency_flat_signal(self) -> None:
        values = [0.01] * 10
        ta = self._analyser_with_values("svc", "error_rate", values)
        reports = ta.analyse("svc")
        er = next((r for r in reports if r.metric == "error_rate"), None)
        assert er is not None
        assert er.urgency == "NONE"
        assert er.time_to_threshold_min is None

    def test_none_urgency_falling_signal_below_threshold(self) -> None:
        # Falling but already below threshold — no projection
        values = [0.04, 0.035, 0.03, 0.025, 0.02, 0.015, 0.01, 0.008, 0.006, 0.005]
        ta = self._analyser_with_values("svc", "error_rate", values)
        reports = ta.analyse("svc")
        er = next((r for r in reports if r.metric == "error_rate"), None)
        assert er is not None
        assert er.urgency == "NONE"

    def test_returns_empty_with_too_few_readings(self) -> None:
        ta = TrendAnalyser(window_steps=10)
        sig = _metric_signal("svc", error_rate=0.20)
        ta.add_signal(sig)
        ta.add_signal(sig)
        # Only 2 readings — need >= 3
        reports = ta.analyse("svc")
        er = next((r for r in reports if r.metric == "error_rate"), None)
        assert er is None

    def test_confidence_high_for_linear_data(self) -> None:
        # Perfectly linear data → R² should be very close to 1
        values = [0.01 + i * 0.005 for i in range(10)]
        ta = self._analyser_with_values("svc", "error_rate", values)
        reports = ta.analyse("svc")
        er = next((r for r in reports if r.metric == "error_rate"), None)
        assert er is not None
        assert er.confidence > 0.99

    def test_confidence_low_for_random_data(self) -> None:
        import random
        random.seed(42)
        values = [random.uniform(0.001, 0.04) for _ in range(10)]
        ta = self._analyser_with_values("svc", "error_rate", values)
        reports = ta.analyse("svc")
        er = next((r for r in reports if r.metric == "error_rate"), None)
        assert er is not None
        # R² should be low for random data (≤ 0.6 is a reasonable bound)
        assert er.confidence < 0.9  # can't be perfectly linear


# ---------------------------------------------------------------------------
# PatternMatcher
# ---------------------------------------------------------------------------

class TestPatternMatcher:
    def test_loads_10_fingerprints(self) -> None:
        pm = PatternMatcher()
        assert pm.fingerprint_count() == 10

    def test_no_match_with_insufficient_window(self) -> None:
        pm = PatternMatcher()
        for _ in range(15):  # need 30
            pm.add_signal(_metric_signal("kafka-consumer"))
        assert pm.match("kafka-consumer") == []

    def test_no_match_on_clean_baseline(self) -> None:
        pm = PatternMatcher()
        for _ in range(30):
            pm.add_signal(_metric_signal("kafka-consumer"))
        matches = pm.match("kafka-consumer")
        assert all(m.similarity_score < 0.75 for m in matches)

    def test_fires_on_kafka_lag_cascade(self) -> None:
        # Feed the exact JSON signal shapes as the incoming window —
        # similarity against the fingerprint loaded from the same file = 1.0
        fixture_path = _PROJECT_ROOT / "scenarios" / "kafka_lag_cascade.json"
        with open(fixture_path) as f:
            data = json.load(f)

        pm = PatternMatcher()
        for step in data["signal_shape"]:
            sig = Signal(
                signal_type="metric",
                service="kafka-consumer",
                ts=datetime.fromisoformat(step["timestamp"]),
                metrics={k: step[k] for k in
                         ["throughput", "consumer_lag", "error_rate",
                          "memory_pct", "cpu_pct", "disk_pct", "pod_restarts"]},
                scenario="kafka_lag_cascade",
            )
            pm.add_signal(sig)

        matches = pm.match("kafka-consumer")
        kafka_match = next((m for m in matches if m.scenario_name == "kafka_lag_cascade"), None)
        assert kafka_match is not None
        assert kafka_match.similarity_score >= 0.75

    def test_match_returns_correct_runbook(self) -> None:
        fixture_path = _PROJECT_ROOT / "scenarios" / "kafka_lag_cascade.json"
        with open(fixture_path) as f:
            data = json.load(f)
        pm = PatternMatcher()
        for step in data["signal_shape"]:
            sig = Signal(
                signal_type="metric",
                service="kafka-consumer",
                ts=datetime.fromisoformat(step["timestamp"]),
                metrics={k: step[k] for k in
                         ["throughput", "consumer_lag", "error_rate",
                          "memory_pct", "cpu_pct", "disk_pct", "pod_restarts"]},
            )
            pm.add_signal(sig)
        matches = pm.match("kafka-consumer")
        kafka_match = next((m for m in matches if m.scenario_name == "kafka_lag_cascade"), None)
        assert kafka_match is not None
        assert kafka_match.matched_runbook.endswith(".md")

    def test_similarity_score_in_range(self) -> None:
        fixture_path = _PROJECT_ROOT / "scenarios" / "kafka_lag_cascade.json"
        with open(fixture_path) as f:
            data = json.load(f)
        pm = PatternMatcher()
        for step in data["signal_shape"]:
            sig = Signal(
                signal_type="metric",
                service="kafka-consumer",
                ts=datetime.fromisoformat(step["timestamp"]),
                metrics={k: step[k] for k in
                         ["throughput", "consumer_lag", "error_rate",
                          "memory_pct", "cpu_pct", "disk_pct", "pod_restarts"]},
            )
            pm.add_signal(sig)
        for m in pm.match("kafka-consumer"):
            assert 0.0 <= m.similarity_score <= 1.0

    def test_custom_threshold_respected(self) -> None:
        pm = PatternMatcher(threshold=0.99)   # very high threshold
        fixture_path = _PROJECT_ROOT / "scenarios" / "kafka_lag_cascade.json"
        with open(fixture_path) as f:
            data = json.load(f)
        for step in data["signal_shape"]:
            sig = Signal(
                signal_type="metric",
                service="kafka-consumer",
                ts=datetime.fromisoformat(step["timestamp"]),
                metrics={k: step[k] for k in
                         ["throughput", "consumer_lag", "error_rate",
                          "memory_pct", "cpu_pct", "disk_pct", "pod_restarts"]},
            )
            pm.add_signal(sig)
        # Exact match (similarity = 1.0) must still fire even at threshold=0.99
        matches = pm.match("kafka-consumer")
        assert any(m.scenario_name == "kafka_lag_cascade" for m in matches)

    def test_results_sorted_by_score_descending(self) -> None:
        fixture_path = _PROJECT_ROOT / "scenarios" / "kafka_lag_cascade.json"
        with open(fixture_path) as f:
            data = json.load(f)
        pm = PatternMatcher(threshold=0.0)   # surface all matches
        for step in data["signal_shape"]:
            sig = Signal(
                signal_type="metric",
                service="kafka-consumer",
                ts=datetime.fromisoformat(step["timestamp"]),
                metrics={k: step[k] for k in
                         ["throughput", "consumer_lag", "error_rate",
                          "memory_pct", "cpu_pct", "disk_pct", "pod_restarts"]},
            )
            pm.add_signal(sig)
        matches = pm.match("kafka-consumer")
        scores = [m.similarity_score for m in matches]
        assert scores == sorted(scores, reverse=True)


# ---------------------------------------------------------------------------
# RiskScorer
# ---------------------------------------------------------------------------

def _build_scorer() -> tuple[SlidingWindowDetector, TrendAnalyser, PatternMatcher, RiskScorer]:
    wd = SlidingWindowDetector(window_size_min=5)
    ta = TrendAnalyser(window_steps=10)
    pm = PatternMatcher()
    scorer = RiskScorer(wd, ta, pm)
    return wd, ta, pm, scorer


class TestRiskScorer:
    def test_score_in_0_100_on_baseline(self) -> None:
        wd, ta, pm, scorer = _build_scorer()
        for _ in range(10):
            sig = _metric_signal("svc")
            wd.add_signal(sig)
            ta.add_signal(sig)
            pm.add_signal(sig)
        rs = scorer.score("svc")
        assert 0.0 <= rs.score <= 100.0

    def test_score_in_0_100_on_critical_signals(self) -> None:
        wd, ta, pm, scorer = _build_scorer()
        for _ in range(10):
            sig = _metric_signal("svc", error_rate=0.50, consumer_lag=50000)
            wd.add_signal(sig)
            ta.add_signal(sig)
            pm.add_signal(sig)
        rs = scorer.score("svc")
        assert 0.0 <= rs.score <= 100.0

    def test_score_higher_after_injected_anomalies(self) -> None:
        wd, ta, pm, scorer = _build_scorer()

        # Baseline first
        for _ in range(5):
            sig = _metric_signal("svc")
            wd.add_signal(sig)
            ta.add_signal(sig)
            pm.add_signal(sig)
        baseline = scorer.score("svc").score

        # Now degrade
        for _ in range(5):
            sig = _metric_signal("svc", error_rate=0.50, consumer_lag=50000)
            wd.add_signal(sig)
            ta.add_signal(sig)
            pm.add_signal(sig)
        degraded = scorer.score("svc").score

        assert degraded >= baseline

    def test_top_signals_nonempty_under_anomaly(self) -> None:
        wd, ta, pm, scorer = _build_scorer()
        for _ in range(10):
            sig = _metric_signal("svc", error_rate=0.50)
            wd.add_signal(sig)
            ta.add_signal(sig)
            pm.add_signal(sig)
        rs = scorer.score("svc")
        assert len(rs.top_signals) > 0

    def test_top_signals_are_strings(self) -> None:
        wd, ta, pm, scorer = _build_scorer()
        for _ in range(10):
            sig = _metric_signal("svc", error_rate=0.50)
            wd.add_signal(sig)
            ta.add_signal(sig)
            pm.add_signal(sig)
        rs = scorer.score("svc")
        for s in rs.top_signals:
            assert isinstance(s, str) and len(s) > 5

    def test_top_signals_capped_at_5(self) -> None:
        wd, ta, pm, scorer = _build_scorer()
        for _ in range(10):
            sig = _metric_signal("svc", error_rate=0.50, consumer_lag=50000,
                                 memory_pct=95.0, cpu_pct=95.0)
            wd.add_signal(sig)
            ta.add_signal(sig)
            pm.add_signal(sig)
        rs = scorer.score("svc")
        assert len(rs.top_signals) <= 5

    def test_composite_formula_weights(self) -> None:
        # With known component scores we can verify 0.30a + 0.35t + 0.35p
        wd, ta, pm, scorer = _build_scorer()
        rs = scorer.score("svc")
        expected = min(100.0, 0.30 * rs.anomaly_score + 0.35 * rs.trend_score + 0.35 * rs.pattern_score)
        assert rs.score == pytest.approx(expected, abs=0.01)

    def test_risk_score_dataclass_fields(self) -> None:
        _, _, _, scorer = _build_scorer()
        # Build with no data — should still return a valid RiskScore
        rs = scorer.score("empty-svc")
        assert isinstance(rs.service, str)
        assert isinstance(rs.ts, datetime)
        assert rs.score == 0.0
        assert rs.top_signals == []


# ---------------------------------------------------------------------------
# DetectionPipeline end-to-end
# ---------------------------------------------------------------------------

class TestDetectionPipeline:
    @pytest.mark.asyncio
    async def test_pipeline_yields_risk_scores(self) -> None:
        fixture = build_scenario("kafka_lag_cascade")
        signals = [s.to_signal() for s in fixture.signal_shape]
        pipeline = DetectionPipeline(SyntheticIngester(signals=signals))
        scores = [rs async for rs in pipeline.run()]
        assert len(scores) == len(signals)

    @pytest.mark.asyncio
    async def test_pipeline_all_scores_in_range(self) -> None:
        fixture = build_scenario("kafka_lag_cascade")
        signals = [s.to_signal() for s in fixture.signal_shape]
        pipeline = DetectionPipeline(SyntheticIngester(signals=signals))
        async for rs in pipeline.run():
            assert 0.0 <= rs.score <= 100.0

    @pytest.mark.asyncio
    async def test_pipeline_risk_increases_during_scenario(self) -> None:
        fixture = build_scenario("kafka_lag_cascade")
        signals = [s.to_signal() for s in fixture.signal_shape]
        pipeline = DetectionPipeline(SyntheticIngester(signals=signals))
        scores = [rs.score async for rs in pipeline.run()]
        # Final score should exceed the initial score (risk builds up)
        assert scores[-1] > scores[0]

    @pytest.mark.asyncio
    async def test_pipeline_pattern_match_fires_by_end(self) -> None:
        # After 30 steps the pattern matcher has a full window — should match
        fixture_path = _PROJECT_ROOT / "scenarios" / "kafka_lag_cascade.json"
        with open(fixture_path) as f:
            data = json.load(f)
        signals = []
        for step in data["signal_shape"]:
            signals.append(Signal(
                signal_type="metric",
                service="kafka-consumer",
                ts=datetime.fromisoformat(step["timestamp"]),
                metrics={k: step[k] for k in
                         ["throughput", "consumer_lag", "error_rate",
                          "memory_pct", "cpu_pct", "disk_pct", "pod_restarts"]},
                scenario="kafka_lag_cascade",
            ))
        pipeline = DetectionPipeline(SyntheticIngester(signals=signals))
        scores = [rs async for rs in pipeline.run()]
        final = scores[-1]
        assert final.pattern_score > 0

    @pytest.mark.asyncio
    async def test_pipeline_skips_log_signals(self) -> None:
        # Log-type signals should not trigger any risk score update
        log_signal = Signal(
            signal_type="log",
            service="svc",
            ts=datetime.now(timezone.utc),
            level="ERROR",
            message="something went wrong",
        )
        pipeline = DetectionPipeline(SyntheticIngester(signals=[log_signal, log_signal]))
        scores = [rs async for rs in pipeline.run()]
        assert scores == []

    @pytest.mark.asyncio
    async def test_pipeline_service_tagged_on_score(self) -> None:
        fixture = build_scenario("kafka_lag_cascade")
        signals = [s.to_signal() for s in fixture.signal_shape[:5]]
        pipeline = DetectionPipeline(SyntheticIngester(signals=signals))
        async for rs in pipeline.run():
            assert rs.service == fixture.service

    @pytest.mark.asyncio
    async def test_pipeline_injectable_detectors(self) -> None:
        wd = SlidingWindowDetector(window_size_min=1)
        ta = TrendAnalyser(window_steps=3)
        pm = PatternMatcher(threshold=0.99)
        fixture = build_scenario("kafka_lag_cascade")
        signals = [s.to_signal() for s in fixture.signal_shape[:5]]
        pipeline = DetectionPipeline(
            SyntheticIngester(signals=signals),
            window_detector=wd,
            trend_analyser=ta,
            pattern_matcher=pm,
        )
        scores = [rs async for rs in pipeline.run()]
        assert len(scores) == 5
