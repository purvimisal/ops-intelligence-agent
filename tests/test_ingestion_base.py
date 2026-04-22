"""
Tests for app/ingestion/base.py (Signal, SignalIngester) and the
SyntheticIngester implementation in app/ingestion/generator.py.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from app.ingestion.base import Signal, SignalIngester
from app.ingestion.generator import (
    LogEvent,
    MetricSnapshot,
    SyntheticIngester,
    build_scenario,
    generate_log_event,
    generate_metric_snapshot,
)

# ---------------------------------------------------------------------------
# Signal dataclass
# ---------------------------------------------------------------------------

def _make_signal(**kwargs) -> Signal:
    defaults = dict(
        signal_type="log",
        service="billing-api",
        ts=datetime.now(timezone.utc),
    )
    return Signal(**{**defaults, **kwargs})


def test_signal_required_fields() -> None:
    s = _make_signal()
    assert s.signal_type == "log"
    assert s.service == "billing-api"
    assert isinstance(s.ts, datetime)


def test_signal_optional_fields_default_none() -> None:
    s = _make_signal()
    assert s.level is None
    assert s.message is None
    assert s.trace_id is None
    assert s.duration_ms is None
    assert s.error_code is None
    assert s.scenario is None


def test_signal_metrics_defaults_to_empty_dict() -> None:
    s = _make_signal()
    assert s.metrics == {}


def test_signal_log_type_fields() -> None:
    s = _make_signal(
        signal_type="log",
        level="ERROR",
        message="upstream timeout",
        trace_id="abc-123",
        duration_ms=450.0,
        error_code="UPSTREAM_TIMEOUT",
    )
    assert s.level == "ERROR"
    assert s.message == "upstream timeout"
    assert s.trace_id == "abc-123"
    assert s.duration_ms == 450.0
    assert s.error_code == "UPSTREAM_TIMEOUT"


def test_signal_metric_type_fields() -> None:
    s = _make_signal(
        signal_type="metric",
        metrics={"throughput": 850.0, "error_rate": 0.01},
    )
    assert s.signal_type == "metric"
    assert s.metrics["throughput"] == 850.0
    assert s.metrics["error_rate"] == 0.01


def test_signal_to_dict_contains_all_keys() -> None:
    s = _make_signal(level="INFO", message="ok")
    d = s.to_dict()
    for key in ("signal_type", "service", "ts", "level", "message",
                "trace_id", "duration_ms", "error_code", "metrics", "scenario"):
        assert key in d


def test_signal_to_dict_ts_is_iso_string() -> None:
    s = _make_signal()
    d = s.to_dict()
    # Must be parseable back to datetime
    parsed = datetime.fromisoformat(d["ts"])
    assert isinstance(parsed, datetime)


def test_signal_to_dict_is_json_serialisable() -> None:
    import json
    s = _make_signal(
        signal_type="metric",
        metrics={"throughput": 850.0},
        scenario="kafka_lag_cascade",
    )
    json.dumps(s.to_dict())  # must not raise


# ---------------------------------------------------------------------------
# SignalIngester is abstract — cannot be instantiated directly
# ---------------------------------------------------------------------------

def test_signal_ingester_is_abstract() -> None:
    with pytest.raises(TypeError):
        SignalIngester()  # type: ignore[abstract]


def test_signal_ingester_has_abstract_read_signals() -> None:
    assert "read_signals" in SignalIngester.__abstractmethods__


def test_signal_ingester_has_abstract_close() -> None:
    assert "close" in SignalIngester.__abstractmethods__


# ---------------------------------------------------------------------------
# LogEvent.to_signal()
# ---------------------------------------------------------------------------

def test_log_event_to_signal_type() -> None:
    event = generate_log_event()
    signal = event.to_signal()
    assert signal.signal_type == "log"


def test_log_event_to_signal_preserves_service() -> None:
    event = generate_log_event(service="billing-api")
    assert event.to_signal().service == "billing-api"


def test_log_event_to_signal_preserves_fields() -> None:
    event = generate_log_event()
    signal = event.to_signal()
    assert signal.level == event.level
    assert signal.message == event.message
    assert signal.trace_id == event.trace_id
    assert signal.duration_ms == event.duration_ms
    assert signal.error_code == event.error_code


def test_log_event_to_signal_ts_is_datetime() -> None:
    event = generate_log_event()
    assert isinstance(event.to_signal().ts, datetime)


def test_log_event_to_signal_metrics_empty() -> None:
    event = generate_log_event()
    assert event.to_signal().metrics == {}


def test_log_event_to_signal_scenario_none() -> None:
    # Baseline log events carry no scenario tag
    event = generate_log_event()
    assert event.to_signal().scenario is None


# ---------------------------------------------------------------------------
# MetricSnapshot.to_signal()
# ---------------------------------------------------------------------------

def test_metric_snapshot_to_signal_type() -> None:
    snap = generate_metric_snapshot()
    assert snap.to_signal().signal_type == "metric"


def test_metric_snapshot_to_signal_preserves_service() -> None:
    snap = generate_metric_snapshot(service="db-proxy")
    assert snap.to_signal().service == "db-proxy"


def test_metric_snapshot_to_signal_metrics_dict() -> None:
    snap = generate_metric_snapshot()
    signal = snap.to_signal()
    for key in ("throughput", "consumer_lag", "error_rate",
                "memory_pct", "cpu_pct", "disk_pct", "pod_restarts"):
        assert key in signal.metrics


def test_metric_snapshot_to_signal_metrics_values_match() -> None:
    snap = generate_metric_snapshot()
    m = snap.to_signal().metrics
    assert m["throughput"] == snap.throughput
    assert m["error_rate"] == snap.error_rate
    assert m["memory_pct"] == snap.memory_pct


def test_metric_snapshot_to_signal_no_log_fields() -> None:
    snap = generate_metric_snapshot()
    signal = snap.to_signal()
    assert signal.level is None
    assert signal.message is None
    assert signal.trace_id is None


def test_metric_snapshot_to_signal_scenario_propagated() -> None:
    fixture = build_scenario("kafka_lag_cascade")
    snap = fixture.signal_shape[0]
    assert snap.to_signal().scenario == "kafka_lag_cascade"


# ---------------------------------------------------------------------------
# SyntheticIngester — interface compliance
# ---------------------------------------------------------------------------

def test_synthetic_ingester_is_signal_ingester_subclass() -> None:
    assert issubclass(SyntheticIngester, SignalIngester)


def test_synthetic_ingester_implements_all_abstract_methods() -> None:
    # Instantiation would raise TypeError if any abstract method is missing
    ingester = SyntheticIngester(signals=[])
    assert ingester is not None


# ---------------------------------------------------------------------------
# SyntheticIngester — replay mode (signals=[...])
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_replay_mode_yields_provided_signals() -> None:
    signals = [
        _make_signal(signal_type="log", service="billing-api"),
        _make_signal(signal_type="metric", service="db-proxy"),
    ]
    ingester = SyntheticIngester(signals=signals)
    collected = [s async for s in ingester.read_signals()]
    assert len(collected) == 2
    assert collected[0].service == "billing-api"
    assert collected[1].service == "db-proxy"


@pytest.mark.asyncio
async def test_replay_mode_stops_after_list_exhausted() -> None:
    signals = [_make_signal() for _ in range(5)]
    ingester = SyntheticIngester(signals=signals)
    collected = [s async for s in ingester.read_signals()]
    assert len(collected) == 5


@pytest.mark.asyncio
async def test_replay_mode_empty_list_yields_nothing() -> None:
    ingester = SyntheticIngester(signals=[])
    collected = [s async for s in ingester.read_signals()]
    assert collected == []


@pytest.mark.asyncio
async def test_replay_mode_signals_are_signal_instances() -> None:
    event = generate_log_event()
    snap = generate_metric_snapshot()
    ingester = SyntheticIngester(signals=[event.to_signal(), snap.to_signal()])
    async for signal in ingester.read_signals():
        assert isinstance(signal, Signal)


# ---------------------------------------------------------------------------
# SyntheticIngester — continuous mode
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_continuous_mode_yields_signals() -> None:
    ingester = SyntheticIngester()
    collected: list[Signal] = []
    async for signal in ingester.read_signals():
        collected.append(signal)
        if len(collected) >= 10:
            await ingester.close()
    assert len(collected) == 10


@pytest.mark.asyncio
async def test_continuous_mode_mix_of_types() -> None:
    ingester = SyntheticIngester()
    collected: list[Signal] = []
    async for signal in ingester.read_signals():
        collected.append(signal)
        if len(collected) >= 20:
            await ingester.close()
    types = {s.signal_type for s in collected}
    assert "log" in types
    assert "metric" in types


@pytest.mark.asyncio
async def test_continuous_mode_every_4th_is_metric() -> None:
    ingester = SyntheticIngester()
    collected: list[Signal] = []
    async for signal in ingester.read_signals():
        collected.append(signal)
        if len(collected) >= 12:
            await ingester.close()
    # indices 0, 4, 8 should be metrics
    assert collected[0].signal_type == "metric"
    assert collected[4].signal_type == "metric"
    assert collected[8].signal_type == "metric"


# ---------------------------------------------------------------------------
# SyntheticIngester — context manager
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_context_manager_enters_and_exits() -> None:
    signals = [_make_signal()]
    async with SyntheticIngester(signals=signals) as ingester:
        collected = [s async for s in ingester.read_signals()]
    assert len(collected) == 1


@pytest.mark.asyncio
async def test_context_manager_calls_close_on_exit() -> None:
    ingester = SyntheticIngester(signals=[])
    async with ingester:
        pass
    # After exit, stop event should be set
    assert ingester._stop.is_set()


@pytest.mark.asyncio
async def test_close_stops_continuous_stream() -> None:
    ingester = SyntheticIngester()
    collected: list[Signal] = []
    async for signal in ingester.read_signals():
        collected.append(signal)
        if len(collected) == 5:
            await ingester.close()
    # Stream should have stopped at or very near 5
    assert len(collected) >= 5


# ---------------------------------------------------------------------------
# SyntheticIngester — scenario signal round-trip
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_scenario_signals_round_trip_via_ingester() -> None:
    fixture = build_scenario("kafka_lag_cascade")
    signals = [snap.to_signal() for snap in fixture.signal_shape]
    signals += [log.to_signal() for log in fixture.log_events]

    ingester = SyntheticIngester(signals=signals)
    collected = [s async for s in ingester.read_signals()]

    assert len(collected) == len(signals)
    metric_signals = [s for s in collected if s.signal_type == "metric"]
    log_signals = [s for s in collected if s.signal_type == "log"]
    assert len(metric_signals) == len(fixture.signal_shape)
    assert len(log_signals) == len(fixture.log_events)
    # All metric signals should carry the scenario tag
    for s in metric_signals:
        assert s.scenario == "kafka_lag_cascade"
