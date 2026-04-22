"""
Sliding window anomaly detector.

SlidingWindowDetector maintains a rolling buffer of metric signals per service
and fires AnomalyEvent when the most-recent reading crosses a threshold.

Idempotency guarantee: a (service, timestamp) pair is never entered into the
buffer twice.  Re-processing the same window produces identical AnomalyEvents.
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from app.core.config import settings
from app.ingestion.base import Signal

# Maps metric name → (warn_attr, critical_attr) on Settings.
# Used by window, trend, and scorer so all threshold references stay in sync.
METRIC_THRESHOLDS: dict[str, tuple[str, str]] = {
    "error_rate":   ("error_rate_warn",    "error_rate_critical"),
    "consumer_lag": ("consumer_lag_warn",  "consumer_lag_critical"),
    "memory_pct":   ("memory_pct_warn",    "memory_pct_critical"),
    "cpu_pct":      ("cpu_pct_warn",       "cpu_pct_critical"),
}


@dataclass
class AnomalyEvent:
    service: str
    metric: str
    value: float          # current (latest) reading
    threshold: float      # the threshold that was crossed
    window_avg: float     # mean across the window
    window_start: datetime
    window_end: datetime
    severity: str         # "WARNING" | "CRITICAL"
    ts: datetime          # when the anomaly was detected


class SlidingWindowDetector:
    """
    Idempotent sliding window detector.

    add_signal() accepts any Signal but only stores metric signals.  The same
    (service, ts) pair is silently ignored if seen before, so processing a
    batch of signals twice produces the same result as processing it once.
    """

    def __init__(self, window_size_min: int | None = None) -> None:
        wsize = window_size_min if window_size_min is not None else settings.window_size_min
        self._max_entries = max(1, (wsize * 60) // settings.metrics_interval_sec)
        # service → rolling deque of (ts, metrics_dict)
        self._windows: dict[str, deque[tuple[datetime, dict[str, Any]]]] = {}
        # service → set of ts strings already inserted (idempotency guard)
        self._seen: dict[str, set[str]] = defaultdict(set)

    def _window(self, service: str) -> deque[tuple[datetime, dict[str, Any]]]:
        if service not in self._windows:
            self._windows[service] = deque(maxlen=self._max_entries)
        return self._windows[service]

    def add_signal(self, signal: Signal) -> None:
        if signal.signal_type != "metric" or not signal.metrics:
            return
        key = signal.ts.isoformat()
        if key in self._seen[signal.service]:
            return
        self._seen[signal.service].add(key)
        self._window(signal.service).append((signal.ts, dict(signal.metrics)))

    def check(self, service: str) -> list[AnomalyEvent]:
        """
        Return AnomalyEvents for the current window.  Pure function — calling
        this twice on the same window returns the same list.
        """
        buf = list(self._window(service))
        if not buf:
            return []

        window_start, window_end = buf[0][0], buf[-1][0]
        now = datetime.now(timezone.utc)
        anomalies: list[AnomalyEvent] = []

        for metric, (warn_attr, crit_attr) in METRIC_THRESHOLDS.items():
            values = [m.get(metric, 0.0) for _, m in buf]
            if not values:
                continue
            current = values[-1]
            avg = sum(values) / len(values)
            warn = getattr(settings, warn_attr)
            crit = getattr(settings, crit_attr)

            if current >= crit:
                severity, threshold = "CRITICAL", crit
            elif current >= warn:
                severity, threshold = "WARNING", warn
            else:
                continue

            anomalies.append(AnomalyEvent(
                service=service,
                metric=metric,
                value=current,
                threshold=threshold,
                window_avg=avg,
                window_start=window_start,
                window_end=window_end,
                severity=severity,
                ts=now,
            ))

        return anomalies

    def services(self) -> list[str]:
        return list(self._windows.keys())
