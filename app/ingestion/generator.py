"""
Synthetic data engine: log events, metric snapshots, and 10 failure scenarios.

Each scenario models gradual pre-incident degradation (not an instant spike)
followed by a failure threshold crossing. The ground_truth dict per scenario
is the eval target for the RCA agent.
"""

from __future__ import annotations

import asyncio
import random
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, AsyncIterator

import numpy as np

from app.ingestion.base import Signal, SignalIngester

# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

LOG_LEVELS = ["DEBUG", "INFO", "WARN", "ERROR", "FATAL"]
SERVICES = [
    "billing-api",
    "kafka-consumer",
    "goldengate-extract",
    "pricing-engine",
    "auth-service",
    "db-proxy",
    "replication-agent",
    "deploy-controller",
]


@dataclass
class LogEvent:
    timestamp: str
    service: str
    level: str
    message: str
    trace_id: str
    duration_ms: float
    error_code: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "service": self.service,
            "level": self.level,
            "message": self.message,
            "trace_id": self.trace_id,
            "duration_ms": self.duration_ms,
            "error_code": self.error_code,
        }

    def to_signal(self) -> Signal:
        return Signal(
            signal_type="log",
            service=self.service,
            ts=datetime.fromisoformat(self.timestamp),
            level=self.level,
            message=self.message,
            trace_id=self.trace_id,
            duration_ms=self.duration_ms,
            error_code=self.error_code,
        )


@dataclass
class MetricSnapshot:
    timestamp: str
    service: str
    throughput: float          # events/sec
    consumer_lag: float        # messages behind
    error_rate: float          # fraction 0–1
    memory_pct: float          # 0–100
    cpu_pct: float             # 0–100
    disk_pct: float            # 0–100
    pod_restarts: int
    scenario: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "service": self.service,
            "throughput": self.throughput,
            "consumer_lag": self.consumer_lag,
            "error_rate": self.error_rate,
            "memory_pct": self.memory_pct,
            "cpu_pct": self.cpu_pct,
            "disk_pct": self.disk_pct,
            "pod_restarts": self.pod_restarts,
            "scenario": self.scenario,
        }

    def to_signal(self) -> Signal:
        return Signal(
            signal_type="metric",
            service=self.service,
            ts=datetime.fromisoformat(self.timestamp),
            metrics={
                "throughput": self.throughput,
                "consumer_lag": self.consumer_lag,
                "error_rate": self.error_rate,
                "memory_pct": self.memory_pct,
                "cpu_pct": self.cpu_pct,
                "disk_pct": self.disk_pct,
                "pod_restarts": self.pod_restarts,
            },
            scenario=self.scenario,
        )


@dataclass
class GroundTruth:
    root_cause: str
    correct_runbook: str
    leading_signals: list[str]
    pre_emptive_action: str


@dataclass
class ScenarioFixture:
    name: str
    service: str
    ground_truth: GroundTruth
    # Metric snapshots ordered from t=0 (baseline) to t=N (failure)
    signal_shape: list[MetricSnapshot] = field(default_factory=list)
    # Log events that accompany the scenario
    log_events: list[LogEvent] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Baseline noise helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _jitter(value: float, pct: float = 0.05) -> float:
    """Add ±pct random noise to a value."""
    return value * (1.0 + random.uniform(-pct, pct))


def _baseline_snapshot(service: str, scenario: str | None = None) -> MetricSnapshot:
    return MetricSnapshot(
        timestamp=_now_iso(),
        service=service,
        throughput=_jitter(850.0),
        consumer_lag=_jitter(120.0),
        error_rate=_jitter(0.005),
        memory_pct=_jitter(42.0),
        cpu_pct=_jitter(35.0),
        disk_pct=_jitter(38.0),
        pod_restarts=0,
        scenario=scenario,
    )


def _make_log(
    service: str,
    level: str,
    message: str,
    duration_ms: float = 20.0,
    error_code: str | None = None,
) -> LogEvent:
    return LogEvent(
        timestamp=_now_iso(),
        service=service,
        level=level,
        message=message,
        trace_id=str(uuid.uuid4()),
        duration_ms=_jitter(duration_ms, 0.2),
        error_code=error_code,
    )


def _gradual(
    start: float,
    end: float,
    steps: int,
    noise: float = 0.03,
    max_val: float = float("inf"),
) -> list[float]:
    """Linear ramp from start to end over `steps` with small noise."""
    vals = np.linspace(start, end, steps)
    noise_arr = np.random.normal(0, abs(end - start) * noise, steps)
    return [float(min(max(0.0, v + n), max_val)) for v, n in zip(vals, noise_arr)]


def _sigmoid_ramp(start: float, end: float, steps: int) -> list[float]:
    """S-curve ramp — slow start, accelerates mid-way, plateaus near end."""
    xs = np.linspace(-6, 6, steps)
    sigmoid = 1 / (1 + np.exp(-xs))
    return [float(start + (end - start) * s) for s in sigmoid]


# ---------------------------------------------------------------------------
# Scenario builders
# Each returns a ScenarioFixture with STEPS snapshots (30s cadence each).
# STEPS=30 ≈ 15 minutes of pre-incident window.
# ---------------------------------------------------------------------------

STEPS = 30  # 30 snapshots × 30s = 15 minutes


def _scenario_kafka_lag() -> ScenarioFixture:
    """
    Scenario 1: Kafka consumer lag cascade.
    Consumer falls behind → DB write latency up → lag compounds.
    Signal shape: lag grows 2×-5× baseline; error rate ticks up late.
    """
    service = "kafka-consumer"
    lags = _sigmoid_ramp(120, 48_000, STEPS)
    errors = _gradual(0.005, 0.08, STEPS)
    throughputs = _gradual(850, 220, STEPS)
    db_latencies = _gradual(20, 380, STEPS)

    snapshots = [
        MetricSnapshot(
            timestamp=_now_iso(),
            service=service,
            throughput=throughputs[i],
            consumer_lag=lags[i],
            error_rate=errors[i],
            memory_pct=_jitter(44.0),
            cpu_pct=_jitter(38.0 + i * 0.5),
            disk_pct=_jitter(38.0),
            pod_restarts=0,
            scenario="kafka_lag_cascade",
        )
        for i in range(STEPS)
    ]

    logs = [
        _make_log(service, "WARN", "consumer lag growing, offset gap: 2400", 25.0),
        _make_log(service, "WARN", "DB write latency high: 180ms", 180.0),
        _make_log(service, "ERROR", "consumer lag exceeded threshold: 12000", 30.0, "KAFKA_LAG_HIGH"),
        _make_log(service, "ERROR", "DB write timeout after 5s", 5000.0, "DB_WRITE_TIMEOUT"),
        _make_log(service, "FATAL", "consumer group rebalancing — partition count mismatch", 10.0, "CONSUMER_REBALANCE"),
    ]

    return ScenarioFixture(
        name="kafka_lag_cascade",
        service=service,
        ground_truth=GroundTruth(
            root_cause="Kafka consumer throughput dropped due to downstream DB write latency spike, causing lag to compound in a positive feedback loop.",
            correct_runbook="runbook_kafka_consumer_lag.md",
            leading_signals=["consumer_lag rate-of-change > 2× baseline", "db_write_latency_ms > 150", "throughput < 500 events/sec"],
            pre_emptive_action="Scale consumer replicas from 2 to 4 to absorb backlog before lag exceeds 50k.",
        ),
        signal_shape=snapshots,
        log_events=logs,
    )


def _scenario_goldengate_extract() -> ScenarioFixture:
    """
    Scenario 2: GoldenGate extract failure.
    Transaction log growth + extract lag + disk I/O spike precede extract stop.
    """
    service = "goldengate-extract"
    disk = _sigmoid_ramp(38, 91, STEPS)
    lag = _gradual(0, 7200, STEPS)       # extract lag in seconds
    cpu = _gradual(35, 88, STEPS)

    snapshots = [
        MetricSnapshot(
            timestamp=_now_iso(),
            service=service,
            throughput=_gradual(400, 10, STEPS)[i],
            consumer_lag=lag[i],
            error_rate=_gradual(0.002, 0.12, STEPS)[i],
            memory_pct=_jitter(55.0),
            cpu_pct=cpu[i],
            disk_pct=disk[i],
            pod_restarts=0,
            scenario="goldengate_extract_failure",
        )
        for i in range(STEPS)
    ]

    logs = [
        _make_log(service, "WARN", "transaction log file size: 4.2 GB, threshold: 5 GB", 10.0),
        _make_log(service, "WARN", "extract lag accumulating: 900s behind", 15.0),
        _make_log(service, "ERROR", "disk I/O wait: 78%", 8.0, "DISK_IO_HIGH"),
        _make_log(service, "ERROR", "extract process checkpoint failed", 12.0, "CHECKPOINT_FAIL"),
        _make_log(service, "FATAL", "GoldenGate extract ABENDED — trail file write failure", 5.0, "GG_EXTRACT_ABEND"),
    ]

    return ScenarioFixture(
        name="goldengate_extract_failure",
        service=service,
        ground_truth=GroundTruth(
            root_cause="GoldenGate extract process abended due to trail file write failure caused by disk exhaustion from transaction log growth.",
            correct_runbook="runbook_goldengate_extract.md",
            leading_signals=["disk_pct linear growth > 85%", "extract_lag_sec > 1800", "disk_io_wait_pct > 60%"],
            pre_emptive_action="Restart extract process after clearing old trail files; verify disk has > 20% free.",
        ),
        signal_shape=snapshots,
        log_events=logs,
    )


def _scenario_cascade_5xx() -> ScenarioFixture:
    """
    Scenario 3: Cascade 5xx errors.
    Upstream service error rate climbs → p99 latency spikes → downstream cascade.
    """
    service = "billing-api"
    error_rate = _sigmoid_ramp(0.005, 0.42, STEPS)
    latency = _sigmoid_ramp(45, 8500, STEPS)

    snapshots = [
        MetricSnapshot(
            timestamp=_now_iso(),
            service=service,
            throughput=_gradual(900, 150, STEPS)[i],
            consumer_lag=_jitter(120.0),
            error_rate=error_rate[i],
            memory_pct=_jitter(48.0),
            cpu_pct=_gradual(35, 75, STEPS)[i],
            disk_pct=_jitter(38.0),
            pod_restarts=0,
            scenario="cascade_5xx_errors",
        )
        for i in range(STEPS)
    ]

    logs = [
        _make_log(service, "WARN", "upstream pricing-engine p99 latency: 850ms", 850.0),
        _make_log(service, "WARN", "5xx rate: 2.1% — approaching circuit breaker threshold", 30.0),
        _make_log(service, "ERROR", "HTTP 503 from pricing-engine, retry 1/3", 1200.0, "HTTP_503"),
        _make_log(service, "ERROR", "circuit breaker half-open — upstream still unhealthy", 20.0, "CIRCUIT_HALF_OPEN"),
        _make_log(service, "FATAL", "circuit breaker OPEN — all requests to pricing-engine rejected", 5.0, "CIRCUIT_OPEN"),
    ]

    return ScenarioFixture(
        name="cascade_5xx_errors",
        service=service,
        ground_truth=GroundTruth(
            root_cause="Upstream pricing-engine saturation caused p99 latency to exceed timeout budget, triggering 5xx cascade into billing-api.",
            correct_runbook="runbook_cascade_5xx.md",
            leading_signals=["upstream_p99_latency_ms > 500", "error_rate > 0.05 and climbing", "circuit_breaker_state != closed"],
            pre_emptive_action="Open circuit breaker on upstream pricing-engine to shed load and prevent cascade propagation.",
        ),
        signal_shape=snapshots,
        log_events=logs,
    )


def _scenario_pod_oomkill() -> ScenarioFixture:
    """
    Scenario 4: Pod OOMKill loop.
    Memory grows linearly → OOM kill → pod restart counter increments.
    """
    service = "billing-api"
    memory = _gradual(42, 98, STEPS)
    restarts = [int(i / 6) for i in range(STEPS)]  # restart every ~3 min

    snapshots = [
        MetricSnapshot(
            timestamp=_now_iso(),
            service=service,
            throughput=_gradual(850, 600, STEPS)[i],
            consumer_lag=_jitter(120.0),
            error_rate=_gradual(0.005, 0.035, STEPS)[i],
            memory_pct=memory[i],
            cpu_pct=_jitter(40.0),
            disk_pct=_jitter(38.0),
            pod_restarts=restarts[i],
            scenario="pod_oomkill_loop",
        )
        for i in range(STEPS)
    ]

    logs = [
        _make_log(service, "WARN", "heap memory utilisation: 72% — GC pressure increasing", 15.0),
        _make_log(service, "WARN", "memory growth rate: +1.8% per minute", 10.0),
        _make_log(service, "ERROR", "OOMKilled: container exceeded memory limit 2Gi", 5.0, "OOM_KILLED"),
        _make_log(service, "INFO", "pod restarted (restart #2), CrashLoopBackOff threshold: 5", 8.0),
        _make_log(service, "FATAL", "pod in CrashLoopBackOff — restart count: 5", 5.0, "CRASH_LOOP"),
    ]

    return ScenarioFixture(
        name="pod_oomkill_loop",
        service=service,
        ground_truth=GroundTruth(
            root_cause="Memory leak in billing-api caused linear heap growth leading to repeated OOMKill events and CrashLoopBackOff.",
            correct_runbook="runbook_pod_oomkill.md",
            leading_signals=["memory_pct linear growth > 80%", "pod_restarts incrementing", "heap_gc_pause_ms increasing"],
            pre_emptive_action="Increase pod memory limit from 2Gi to 4Gi and alert on-call for heap dump analysis.",
        ),
        signal_shape=snapshots,
        log_events=logs,
    )


def _scenario_db_connection_exhaustion() -> ScenarioFixture:
    """
    Scenario 5: DB connection pool exhaustion.
    Pool utilisation rises > 80% → query queue backs up → timeouts.
    """
    service = "db-proxy"
    pool_util = _sigmoid_ramp(0.35, 1.0, STEPS)  # fraction of pool used
    queue_depth = _sigmoid_ramp(0, 420, STEPS)

    snapshots = [
        MetricSnapshot(
            timestamp=_now_iso(),
            service=service,
            throughput=_gradual(650, 80, STEPS)[i],
            consumer_lag=queue_depth[i],
            error_rate=_gradual(0.003, 0.25, STEPS)[i],
            memory_pct=_jitter(55.0 + pool_util[i] * 20),
            cpu_pct=_jitter(60.0),
            disk_pct=_jitter(38.0),
            pod_restarts=0,
            scenario="db_connection_exhaustion",
        )
        for i in range(STEPS)
    ]

    logs = [
        _make_log(service, "WARN", "connection pool utilisation: 82% (41/50 connections active)", 12.0),
        _make_log(service, "WARN", "query queue depth: 85 — above normal (< 20)", 10.0),
        _make_log(service, "ERROR", "connection checkout timeout after 5s — pool exhausted", 5000.0, "POOL_EXHAUSTED"),
        _make_log(service, "ERROR", "query queue backed up: 310 pending", 8.0, "QUEUE_OVERFLOW"),
        _make_log(service, "FATAL", "all connections in pool exhausted — new requests rejected", 5.0, "POOL_FULL"),
    ]

    return ScenarioFixture(
        name="db_connection_exhaustion",
        service=service,
        ground_truth=GroundTruth(
            root_cause="DB connection pool exhausted due to long-running queries holding connections, preventing new queries from executing.",
            correct_runbook="runbook_db_connection_pool.md",
            leading_signals=["pool_utilisation_pct > 80%", "query_queue_depth > 50", "connection_checkout_latency_ms > 1000"],
            pre_emptive_action="Reduce max_connections per client by 20% and scale db-proxy replica to distribute load.",
        ),
        signal_shape=snapshots,
        log_events=logs,
    )


def _scenario_pricing_engine_timeout() -> ScenarioFixture:
    """
    Scenario 6: Pricing engine timeout.
    Slow query rate rises → p99 response > SLO → timeouts cascade.
    """
    service = "pricing-engine"
    p99 = _sigmoid_ramp(95, 12_000, STEPS)   # ms
    slow_query_rate = _gradual(0.01, 0.38, STEPS)

    snapshots = [
        MetricSnapshot(
            timestamp=_now_iso(),
            service=service,
            throughput=_gradual(700, 120, STEPS)[i],
            consumer_lag=_jitter(50.0),
            error_rate=slow_query_rate[i],
            memory_pct=_jitter(60.0),
            cpu_pct=_gradual(45, 92, STEPS)[i],
            disk_pct=_jitter(40.0),
            pod_restarts=0,
            scenario="pricing_engine_timeout",
        )
        for i in range(STEPS)
    ]

    logs = [
        _make_log(service, "WARN", f"slow query detected: SELECT price_calc took {int(p99[5])}ms", p99[5]),
        _make_log(service, "WARN", "p99 response time: 1.8s — SLO threshold: 2.0s", p99[10]),
        _make_log(service, "ERROR", "p99 breached SLO: 4200ms > 2000ms SLO", p99[18], "SLO_BREACH"),
        _make_log(service, "ERROR", "query plan regression detected — missing index scan", 10.0, "QUERY_PLAN_REGRESSION"),
        _make_log(service, "FATAL", "request timeout: client gave up after 10s", 10000.0, "REQUEST_TIMEOUT"),
    ]

    return ScenarioFixture(
        name="pricing_engine_timeout",
        service=service,
        ground_truth=GroundTruth(
            root_cause="Pricing engine query plan regression caused slow query rate to spike, pushing p99 response time above SLO and triggering request timeouts.",
            correct_runbook="runbook_pricing_engine_timeout.md",
            leading_signals=["slow_query_rate > 0.10", "p99_response_ms > 2000", "cpu_pct > 85%"],
            pre_emptive_action="Kill slow queries exceeding 5s and warm pricing cache to reduce cold-query rate.",
        ),
        signal_shape=snapshots,
        log_events=logs,
    )


def _scenario_replication_lag() -> ScenarioFixture:
    """
    Scenario 7: Multi-region replication lag.
    Write volume primary exceeds replication throughput → lag accumulates.
    """
    service = "replication-agent"
    lag_bytes = _sigmoid_ramp(0, 850_000_000, STEPS)   # bytes behind
    write_volume = _gradual(1000, 4200, STEPS)           # writes/sec primary

    snapshots = [
        MetricSnapshot(
            timestamp=_now_iso(),
            service=service,
            throughput=write_volume[i],
            consumer_lag=lag_bytes[i] / 1000,       # normalise to "units"
            error_rate=_gradual(0.001, 0.04, STEPS)[i],
            memory_pct=_jitter(50.0),
            cpu_pct=_gradual(40, 82, STEPS)[i],
            disk_pct=_jitter(45.0),
            pod_restarts=0,
            scenario="multi_region_replication_lag",
        )
        for i in range(STEPS)
    ]

    logs = [
        _make_log(service, "WARN", "replication lag: 45 MB behind primary", 8.0),
        _make_log(service, "WARN", "primary write volume 2.1× replica apply throughput", 10.0),
        _make_log(service, "ERROR", "replication lag: 320 MB — RPO breach threshold: 500 MB", 5.0, "REPLICATION_LAG_HIGH"),
        _make_log(service, "ERROR", "replica apply worker stalled — WAL backlog growing", 8.0, "WAL_BACKLOG"),
        _make_log(service, "FATAL", "replication lag exceeded RPO limit: 850 MB", 5.0, "RPO_BREACH"),
    ]

    return ScenarioFixture(
        name="multi_region_replication_lag",
        service=service,
        ground_truth=GroundTruth(
            root_cause="Primary write throughput burst exceeded replica apply capacity, causing WAL backlog to grow and replication lag to breach RPO threshold.",
            correct_runbook="runbook_replication_lag.md",
            leading_signals=["replication_lag_bytes growing", "primary_writes > 2× replica_apply_rate", "wal_backlog_mb > 100"],
            pre_emptive_action="Throttle primary write rate by 30% until replica apply catches up to < 50 MB lag.",
        ),
        signal_shape=snapshots,
        log_events=logs,
    )


def _scenario_secret_rotation_failure() -> ScenarioFixture:
    """
    Scenario 8: Secret rotation failure.
    Auth errors spike ~10 min after rotation event — credentials not propagated.
    """
    service = "auth-service"
    # Auth errors are near-zero until step ~20 (≈10 min), then spike sharply
    error_rates = []
    for i in range(STEPS):
        if i < 20:
            error_rates.append(_jitter(0.003))
        else:
            # Sharp spike after credential rotation
            spike = 0.003 + (i - 20) * 0.045
            error_rates.append(min(spike, 0.95))

    snapshots = [
        MetricSnapshot(
            timestamp=_now_iso(),
            service=service,
            throughput=_gradual(500, 80, STEPS)[i] if i >= 20 else _jitter(500.0),
            consumer_lag=_jitter(30.0),
            error_rate=error_rates[i],
            memory_pct=_jitter(38.0),
            cpu_pct=_jitter(25.0),
            disk_pct=_jitter(35.0),
            pod_restarts=0,
            scenario="secret_rotation_failure",
        )
        for i in range(STEPS)
    ]

    logs = [
        _make_log(service, "INFO", "secret rotation initiated for DB_PASSWORD (scheduled)", 5.0),
        _make_log(service, "INFO", "new secret version propagated to vault", 8.0),
        _make_log(service, "WARN", "auth error rate elevated: 3.2% — baseline: 0.3%", 10.0),
        _make_log(service, "ERROR", "authentication failure: invalid credentials for db-proxy", 15.0, "AUTH_FAILED"),
        _make_log(service, "FATAL", "auth failure storm: 89% of login attempts failing — secret not propagated", 5.0, "SECRET_STALE"),
    ]

    return ScenarioFixture(
        name="secret_rotation_failure",
        service=service,
        ground_truth=GroundTruth(
            root_cause="Secret rotation completed in vault but downstream services were not restarted to pick up the new credential, causing authentication failures 10 minutes after rotation.",
            correct_runbook="runbook_secret_rotation.md",
            leading_signals=["auth_error_rate spike 10 min post-rotation", "authentication_failure error_code spike", "throughput drop correlated with rotation event"],
            pre_emptive_action="Re-propagate credentials by rolling restart of auth-service and db-proxy pods after secret rotation.",
        ),
        signal_shape=snapshots,
        log_events=logs,
    )


def _scenario_disk_exhaustion() -> ScenarioFixture:
    """
    Scenario 9: Disk space exhaustion.
    Disk utilisation grows linearly → write errors begin → eventual disk full.
    """
    service = "goldengate-extract"
    disk = _gradual(65, 99.5, STEPS, max_val=100.0)
    write_errors = [0.0 if disk[i] < 88 else _jitter((disk[i] - 88) * 0.04) for i in range(STEPS)]

    snapshots = [
        MetricSnapshot(
            timestamp=_now_iso(),
            service=service,
            throughput=_gradual(400, 320, STEPS)[i],
            consumer_lag=_jitter(200.0),
            error_rate=write_errors[i],
            memory_pct=_jitter(55.0),
            cpu_pct=_jitter(45.0),
            disk_pct=disk[i],
            pod_restarts=0,
            scenario="disk_space_exhaustion",
        )
        for i in range(STEPS)
    ]

    logs = [
        _make_log(service, "WARN", "disk utilisation: 72% — growth rate 0.4%/hour", 5.0),
        _make_log(service, "WARN", "disk utilisation: 85% — purge job overdue", 5.0),
        _make_log(service, "ERROR", "disk write error: no space left on device (trail file)", 8.0, "ENOSPC"),
        _make_log(service, "ERROR", "disk utilisation critical: 94%", 5.0, "DISK_CRITICAL"),
        _make_log(service, "FATAL", "disk full: trail file write failed — extract suspended", 5.0, "DISK_FULL"),
    ]

    return ScenarioFixture(
        name="disk_space_exhaustion",
        service=service,
        ground_truth=GroundTruth(
            root_cause="Disk utilisation grew linearly due to trail file accumulation without scheduled purge, causing write failures when disk reached 99%.",
            correct_runbook="runbook_disk_exhaustion.md",
            leading_signals=["disk_pct linear growth > 85%", "write_error_rate beginning at disk > 88%", "purge_job_last_run overdue"],
            pre_emptive_action="Purge trail files older than 24h and alert on-call; schedule recurring disk purge job.",
        ),
        signal_shape=snapshots,
        log_events=logs,
    )


def _scenario_deployment_regression() -> ScenarioFixture:
    """
    Scenario 10: Deployment regression.
    Latency spike and error rate tick up within 10 min of deploy event.
    """
    service = "billing-api"
    # Metrics are stable until step ~6 (≈3 min), then degrade post-deploy
    latency_factor = []
    error_rates = []
    for i in range(STEPS):
        if i < 6:
            latency_factor.append(1.0)
            error_rates.append(_jitter(0.004))
        else:
            # Post-deploy regression ramp
            factor = 1.0 + (i - 6) * 0.18
            latency_factor.append(factor)
            error_rates.append(min(0.004 + (i - 6) * 0.012, 0.28))

    snapshots = [
        MetricSnapshot(
            timestamp=_now_iso(),
            service=service,
            throughput=_gradual(850, 310, STEPS)[i] if i >= 6 else _jitter(850.0),
            consumer_lag=_jitter(120.0),
            error_rate=error_rates[i],
            memory_pct=_jitter(45.0 + latency_factor[i] * 5),
            cpu_pct=_jitter(38.0 + latency_factor[i] * 8),
            disk_pct=_jitter(38.0),
            pod_restarts=0,
            scenario="deployment_regression",
        )
        for i in range(STEPS)
    ]

    logs = [
        _make_log(service, "INFO", "deployment initiated: billing-api v2.14.1 → v2.15.0", 5.0),
        _make_log(service, "INFO", "rollout complete: 3/3 pods running v2.15.0", 8.0),
        _make_log(service, "WARN", "p99 latency elevated: 340ms (baseline: 95ms) — post-deploy", 340.0),
        _make_log(service, "ERROR", "error rate: 4.8% — exceeds 2% deploy watch threshold", 20.0, "DEPLOY_WATCH_BREACH"),
        _make_log(service, "ERROR", "regression confirmed: latency 8.4× baseline 8 min after deploy v2.15.0", 800.0, "REGRESSION_CONFIRMED"),
    ]

    return ScenarioFixture(
        name="deployment_regression",
        service=service,
        ground_truth=GroundTruth(
            root_cause="Deployment v2.15.0 introduced a regression in billing calculation path causing 8× latency increase and elevated error rate within 8 minutes of rollout.",
            correct_runbook="runbook_deployment_regression.md",
            leading_signals=["p99_latency_ms > 3× baseline within 10 min of deploy", "error_rate > 0.02 post-deploy", "deploy_event correlated with metric degradation"],
            pre_emptive_action="Flag v2.15.0 for rollback decision; initiate rollback to v2.14.1 if error rate exceeds 5%.",
        ),
        signal_shape=snapshots,
        log_events=logs,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

ALL_SCENARIO_BUILDERS = {
    "kafka_lag_cascade": _scenario_kafka_lag,
    "goldengate_extract_failure": _scenario_goldengate_extract,
    "cascade_5xx_errors": _scenario_cascade_5xx,
    "pod_oomkill_loop": _scenario_pod_oomkill,
    "db_connection_exhaustion": _scenario_db_connection_exhaustion,
    "pricing_engine_timeout": _scenario_pricing_engine_timeout,
    "multi_region_replication_lag": _scenario_replication_lag,
    "secret_rotation_failure": _scenario_secret_rotation_failure,
    "disk_space_exhaustion": _scenario_disk_exhaustion,
    "deployment_regression": _scenario_deployment_regression,
}


def build_scenario(name: str) -> ScenarioFixture:
    if name not in ALL_SCENARIO_BUILDERS:
        raise ValueError(f"Unknown scenario '{name}'. Available: {list(ALL_SCENARIO_BUILDERS)}")
    return ALL_SCENARIO_BUILDERS[name]()


def build_all_scenarios() -> dict[str, ScenarioFixture]:
    return {name: builder() for name, builder in ALL_SCENARIO_BUILDERS.items()}


def generate_log_event(service: str | None = None) -> LogEvent:
    """Generate a single realistic baseline log event."""
    svc = service or random.choice(SERVICES)
    level = random.choices(LOG_LEVELS, weights=[20, 60, 12, 7, 1])[0]
    messages = {
        "DEBUG": ["cache miss for key billing:user:1234", "trace span started", "connection checked out from pool"],
        "INFO": ["request processed successfully", "health check OK", "metrics flushed to prometheus"],
        "WARN": ["slow response: 450ms", "retry attempt 1/3", "cache hit rate below 70%"],
        "ERROR": ["upstream timeout", "database query failed", "unexpected null in pricing response"],
        "FATAL": ["service shutting down", "unrecoverable error — panic", "out of memory"],
    }
    return _make_log(svc, level, random.choice(messages[level]))


def generate_metric_snapshot(service: str | None = None) -> MetricSnapshot:
    """Generate a single realistic baseline metric snapshot."""
    svc = service or random.choice(SERVICES)
    return _baseline_snapshot(svc)


class SyntheticIngester(SignalIngester):
    """
    SignalIngester implementation backed by the synthetic data engine.

    Two modes:
      - Replay mode: pass signals=[...] to yield a fixed list and stop.
        Use this for unit tests and scenario injection.
      - Continuous mode: omit signals to generate a baseline stream forever
        (every 4th signal is a MetricSnapshot; the rest are LogEvents).
        Call close() or use as an async context manager to stop the stream.
    """

    def __init__(self, signals: list[Signal] | None = None) -> None:
        self._signals = signals
        self._stop = asyncio.Event()

    async def read_signals(self) -> AsyncIterator[Signal]:  # type: ignore[override]
        if self._signals is not None:
            for signal in self._signals:
                yield signal
            return

        count = 0
        while not self._stop.is_set():
            if count % 4 == 0:
                yield generate_metric_snapshot().to_signal()
            else:
                yield generate_log_event().to_signal()
            count += 1
            await asyncio.sleep(0)  # yield to the event loop without artificial delay

    async def close(self) -> None:
        self._stop.set()
