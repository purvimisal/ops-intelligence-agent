"""
Tests for the synthetic data engine.
Covers: valid log/metric output, all 10 scenarios, ground truth completeness,
and gradual (non-instant) pre-incident signal shapes.
"""

from __future__ import annotations

import pytest

from app.ingestion.generator import (
    ALL_SCENARIO_BUILDERS,
    STEPS,
    LogEvent,
    MetricSnapshot,
    ScenarioFixture,
    build_all_scenarios,
    build_scenario,
    generate_log_event,
    generate_metric_snapshot,
)

# ---------------------------------------------------------------------------
# Log event tests
# ---------------------------------------------------------------------------

def test_log_event_has_required_fields() -> None:
    event = generate_log_event()
    assert isinstance(event, LogEvent)
    d = event.to_dict()
    for key in ("timestamp", "service", "level", "message", "trace_id", "duration_ms"):
        assert key in d
        assert d[key] is not None


def test_log_event_valid_level() -> None:
    for _ in range(50):
        event = generate_log_event()
        assert event.level in ("DEBUG", "INFO", "WARN", "ERROR", "FATAL")


def test_log_event_positive_duration() -> None:
    for _ in range(20):
        event = generate_log_event()
        assert event.duration_ms > 0


def test_log_event_trace_id_is_uuid() -> None:
    import re
    pattern = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
    for _ in range(20):
        event = generate_log_event()
        assert pattern.match(event.trace_id), f"Bad trace_id: {event.trace_id}"


def test_log_event_service_pin() -> None:
    event = generate_log_event(service="billing-api")
    assert event.service == "billing-api"


# ---------------------------------------------------------------------------
# Metric snapshot tests
# ---------------------------------------------------------------------------

def test_metric_snapshot_has_required_fields() -> None:
    snap = generate_metric_snapshot()
    assert isinstance(snap, MetricSnapshot)
    d = snap.to_dict()
    for key in ("timestamp", "service", "throughput", "consumer_lag", "error_rate",
                "memory_pct", "cpu_pct", "disk_pct", "pod_restarts"):
        assert key in d


def test_metric_snapshot_ranges() -> None:
    for _ in range(30):
        snap = generate_metric_snapshot()
        assert snap.throughput > 0
        assert snap.consumer_lag >= 0
        assert 0.0 <= snap.error_rate <= 1.0
        assert 0.0 <= snap.memory_pct <= 100.0
        assert 0.0 <= snap.cpu_pct <= 100.0
        assert 0.0 <= snap.disk_pct <= 100.0
        assert snap.pod_restarts >= 0


def test_metric_snapshot_service_pin() -> None:
    snap = generate_metric_snapshot(service="db-proxy")
    assert snap.service == "db-proxy"


# ---------------------------------------------------------------------------
# Scenario count and naming
# ---------------------------------------------------------------------------

EXPECTED_SCENARIO_NAMES = {
    "kafka_lag_cascade",
    "goldengate_extract_failure",
    "cascade_5xx_errors",
    "pod_oomkill_loop",
    "db_connection_exhaustion",
    "pricing_engine_timeout",
    "multi_region_replication_lag",
    "secret_rotation_failure",
    "disk_space_exhaustion",
    "deployment_regression",
}


def test_all_10_scenarios_exist() -> None:
    assert set(ALL_SCENARIO_BUILDERS.keys()) == EXPECTED_SCENARIO_NAMES


def test_build_all_scenarios_returns_10() -> None:
    scenarios = build_all_scenarios()
    assert len(scenarios) == 10


def test_build_scenario_unknown_raises() -> None:
    with pytest.raises(ValueError, match="Unknown scenario"):
        build_scenario("not_a_real_scenario")


# ---------------------------------------------------------------------------
# Individual scenario shape and content
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("scenario_name", list(EXPECTED_SCENARIO_NAMES))
def test_scenario_has_correct_step_count(scenario_name: str) -> None:
    fixture = build_scenario(scenario_name)
    assert len(fixture.signal_shape) == STEPS, (
        f"{scenario_name}: expected {STEPS} steps, got {len(fixture.signal_shape)}"
    )


@pytest.mark.parametrize("scenario_name", list(EXPECTED_SCENARIO_NAMES))
def test_scenario_has_log_events(scenario_name: str) -> None:
    fixture = build_scenario(scenario_name)
    assert len(fixture.log_events) >= 3, f"{scenario_name}: too few log events"


@pytest.mark.parametrize("scenario_name", list(EXPECTED_SCENARIO_NAMES))
def test_scenario_signal_shape_all_snapshots_valid(scenario_name: str) -> None:
    fixture = build_scenario(scenario_name)
    for i, snap in enumerate(fixture.signal_shape):
        assert snap.service == fixture.service, f"step {i}: service mismatch"
        assert snap.scenario == scenario_name, f"step {i}: scenario tag mismatch"
        assert 0.0 <= snap.error_rate <= 1.0, f"step {i}: error_rate out of range"
        assert snap.throughput >= 0.0, f"step {i}: negative throughput"
        assert 0.0 <= snap.memory_pct <= 100.0, f"step {i}: memory_pct out of range"
        assert 0.0 <= snap.disk_pct <= 100.0, f"step {i}: disk_pct out of range"


@pytest.mark.parametrize("scenario_name", list(EXPECTED_SCENARIO_NAMES))
def test_scenario_ground_truth_complete(scenario_name: str) -> None:
    fixture = build_scenario(scenario_name)
    gt = fixture.ground_truth
    assert gt.root_cause, f"{scenario_name}: missing root_cause"
    assert gt.correct_runbook, f"{scenario_name}: missing correct_runbook"
    assert isinstance(gt.leading_signals, list) and len(gt.leading_signals) >= 2, (
        f"{scenario_name}: need at least 2 leading_signals"
    )
    assert gt.pre_emptive_action, f"{scenario_name}: missing pre_emptive_action"


@pytest.mark.parametrize("scenario_name", list(EXPECTED_SCENARIO_NAMES))
def test_scenario_ground_truth_runbook_format(scenario_name: str) -> None:
    fixture = build_scenario(scenario_name)
    assert fixture.ground_truth.correct_runbook.endswith(".md"), (
        f"{scenario_name}: correct_runbook should reference a .md file"
    )


# ---------------------------------------------------------------------------
# Gradual degradation tests (key invariant for pre-incident signal shapes)
# ---------------------------------------------------------------------------

def test_kafka_lag_shows_gradual_growth() -> None:
    fixture = build_scenario("kafka_lag_cascade")
    lags = [s.consumer_lag for s in fixture.signal_shape]
    early_avg = sum(lags[:5]) / 5
    late_avg = sum(lags[-5:]) / 5
    assert late_avg > early_avg * 5, (
        f"Kafka lag should grow 5× from early to late: early={early_avg:.1f} late={late_avg:.1f}"
    )


def test_kafka_lag_not_instant_spike() -> None:
    """Lag must grow gradually, not jump immediately from baseline to max."""
    fixture = build_scenario("kafka_lag_cascade")
    lags = [s.consumer_lag for s in fixture.signal_shape]
    # First step should not already be at max
    assert lags[0] < lags[-1] * 0.2, (
        f"Lag at t=0 ({lags[0]:.0f}) is already > 20% of final value ({lags[-1]:.0f}) — not gradual"
    )
    # There should be at least 5 steps of monotonic growth in the first half
    first_half = lags[:STEPS // 2]
    monotone_count = sum(1 for a, b in zip(first_half, first_half[1:]) if b >= a)
    assert monotone_count >= len(first_half) // 2, "First half should be mostly increasing"


def test_pod_oomkill_memory_grows_linearly() -> None:
    fixture = build_scenario("pod_oomkill_loop")
    memories = [s.memory_pct for s in fixture.signal_shape]
    assert memories[-1] > memories[0] + 30, (
        f"Memory should grow > 30 pct points: start={memories[0]:.1f}, end={memories[-1]:.1f}"
    )
    assert memories[0] < 70, "Memory should start below 70%"
    assert memories[-1] > 85, "Memory should end above 85% (near OOM threshold)"


def test_pod_oomkill_restarts_increment() -> None:
    fixture = build_scenario("pod_oomkill_loop")
    restarts = [s.pod_restarts for s in fixture.signal_shape]
    assert restarts[-1] > restarts[0], "Pod restarts should increment over time"
    assert restarts[0] == 0, "Restarts should start at 0"


def test_disk_exhaustion_gradual() -> None:
    fixture = build_scenario("disk_space_exhaustion")
    disks = [s.disk_pct for s in fixture.signal_shape]
    assert disks[0] < 75, f"Disk should start below 75%: {disks[0]:.1f}"
    assert disks[-1] > 95, f"Disk should end above 95%: {disks[-1]:.1f}"
    # Write errors should be zero initially, then appear late
    errors = [s.error_rate for s in fixture.signal_shape]
    assert errors[0] == 0.0, "Write errors should start at 0"
    assert errors[-1] > 0.0, "Write errors should appear as disk fills"


def test_secret_rotation_errors_spike_late() -> None:
    """Auth errors should be near-zero for first 20 steps, then spike."""
    fixture = build_scenario("secret_rotation_failure")
    errors = [s.error_rate for s in fixture.signal_shape]
    early_errors = errors[:20]
    late_errors = errors[20:]
    assert max(early_errors) < 0.02, (
        f"Auth errors should be low before rotation: max={max(early_errors):.4f}"
    )
    assert max(late_errors) > 0.2, (
        f"Auth errors should spike after rotation: max={max(late_errors):.4f}"
    )


def test_deployment_regression_stable_then_degrades() -> None:
    """Error rate should be stable for first 6 steps, then climb."""
    fixture = build_scenario("deployment_regression")
    errors = [s.error_rate for s in fixture.signal_shape]
    pre_deploy = errors[:6]
    post_deploy = errors[7:]
    assert max(pre_deploy) < 0.02, f"Pre-deploy errors too high: {max(pre_deploy):.4f}"
    assert max(post_deploy) > 0.05, f"Post-deploy regression not visible: {max(post_deploy):.4f}"


def test_cascade_5xx_error_rate_increases() -> None:
    fixture = build_scenario("cascade_5xx_errors")
    errors = [s.error_rate for s in fixture.signal_shape]
    assert errors[-1] > errors[0] * 10, (
        f"Error rate should grow 10× from start: {errors[0]:.4f} → {errors[-1]:.4f}"
    )


def test_replication_lag_grows() -> None:
    fixture = build_scenario("multi_region_replication_lag")
    lags = [s.consumer_lag for s in fixture.signal_shape]
    assert lags[-1] > lags[0] * 10, "Replication lag should grow significantly"


def test_db_pool_error_rate_grows() -> None:
    fixture = build_scenario("db_connection_exhaustion")
    errors = [s.error_rate for s in fixture.signal_shape]
    assert errors[-1] > errors[0] * 5, "DB pool error rate should grow 5×"


def test_pricing_engine_throughput_degrades() -> None:
    fixture = build_scenario("pricing_engine_timeout")
    throughputs = [s.throughput for s in fixture.signal_shape]
    assert throughputs[-1] < throughputs[0] * 0.5, "Throughput should halve as engine slows"


def test_goldengate_disk_and_lag_grow() -> None:
    fixture = build_scenario("goldengate_extract_failure")
    disks = [s.disk_pct for s in fixture.signal_shape]
    lags = [s.consumer_lag for s in fixture.signal_shape]
    assert disks[-1] > disks[0], "Disk should grow during GoldenGate failure"
    assert lags[-1] > lags[0], "Extract lag should grow during GoldenGate failure"


# ---------------------------------------------------------------------------
# Fixture file round-trip test
# ---------------------------------------------------------------------------

def test_scenario_to_dict_roundtrip() -> None:
    fixture = build_scenario("kafka_lag_cascade")
    import json
    for snap in fixture.signal_shape:
        d = snap.to_dict()
        serialised = json.dumps(d)   # must be JSON-serialisable
        parsed = json.loads(serialised)
        assert parsed["service"] == snap.service
    for log in fixture.log_events:
        d = log.to_dict()
        serialised = json.dumps(d)
        parsed = json.loads(serialised)
        assert parsed["service"] == log.service
