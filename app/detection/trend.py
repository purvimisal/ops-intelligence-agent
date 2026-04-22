"""
Trend extrapolation engine.

TrendAnalyser fits a linear regression over each metric's rolling history and
projects the time remaining before the metric crosses its warning threshold.
Returns a TrendReport per metric with urgency level and R² confidence.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

import numpy as np

from app.core.config import settings
from app.detection.window import METRIC_THRESHOLDS
from app.ingestion.base import Signal


@dataclass
class TrendReport:
    service: str
    metric: str
    slope: float                         # change per step (one metrics_interval_sec period)
    current_value: float
    threshold: float                     # warning threshold being projected toward
    time_to_threshold_min: float | None  # None if not trending toward threshold
    confidence: float                    # R² in [0, 1]
    urgency: str                         # "HIGH" | "MEDIUM" | "LOW" | "NONE"


class TrendAnalyser:
    """
    Maintains a rolling history of metric values per service and fits linear
    regression over the last trend_window_steps readings.

    Requires >= 3 data points before reporting; returns empty list otherwise.
    """

    def __init__(self, window_steps: int | None = None) -> None:
        self._window_steps = window_steps if window_steps is not None else settings.trend_window_steps
        # service → metric → rolling float deque
        self._history: dict[str, dict[str, deque[float]]] = {}

    def _hist(self, service: str, metric: str) -> deque[float]:
        if service not in self._history:
            self._history[service] = {}
        if metric not in self._history[service]:
            self._history[service][metric] = deque(maxlen=self._window_steps)
        return self._history[service][metric]

    def add_signal(self, signal: Signal) -> None:
        if signal.signal_type != "metric":
            return
        for metric in METRIC_THRESHOLDS:
            if metric in signal.metrics:
                self._hist(signal.service, metric).append(float(signal.metrics[metric]))

    def analyse(self, service: str) -> list[TrendReport]:
        """
        Return one TrendReport per metric that has >= 3 readings.
        """
        reports: list[TrendReport] = []

        for metric, (warn_attr, _) in METRIC_THRESHOLDS.items():
            hist = list(self._hist(service, metric))
            if len(hist) < 3:
                continue

            threshold = getattr(settings, warn_attr)
            current = hist[-1]

            x = np.arange(len(hist), dtype=float)
            y = np.array(hist, dtype=float)
            coeffs = np.polyfit(x, y, 1)
            slope = float(coeffs[0])

            # R² confidence
            y_pred = np.polyval(coeffs, x)
            ss_res = float(np.sum((y - y_pred) ** 2))
            ss_tot = float(np.sum((y - float(np.mean(y))) ** 2))
            confidence = max(0.0, min(1.0, 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0))

            # Project time to threshold
            time_to_threshold_min: float | None = None
            if slope > 1e-9 and current < threshold:
                # Metric rising toward threshold from below
                steps = (threshold - current) / slope
                time_to_threshold_min = steps * settings.metrics_interval_sec / 60.0
            elif slope < -1e-9 and current > threshold:
                # Metric falling toward threshold from above (e.g. throughput collapse)
                steps = (current - threshold) / abs(slope)
                time_to_threshold_min = steps * settings.metrics_interval_sec / 60.0

            if time_to_threshold_min is not None:
                if time_to_threshold_min < 30:
                    urgency = "HIGH"
                elif time_to_threshold_min < 60:
                    urgency = "MEDIUM"
                else:
                    urgency = "LOW"
            else:
                urgency = "NONE"

            reports.append(TrendReport(
                service=service,
                metric=metric,
                slope=slope,
                current_value=current,
                threshold=threshold,
                time_to_threshold_min=time_to_threshold_min,
                confidence=confidence,
                urgency=urgency,
            ))

        return reports
