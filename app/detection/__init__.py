"""
Detection layer.

DetectionPipeline wires the three detectors behind the SignalIngester interface.
Each metric signal updates all detector buffers; a RiskScore is yielded after
every update so callers see risk evolve in real time.

No DB writes happen here — persistence is the caller's responsibility so the
pipeline stays independently testable without a running Postgres instance.
"""

from __future__ import annotations

from typing import AsyncIterator

from app.detection.patterns import PatternMatcher
from app.detection.scorer import RiskScore, RiskScorer
from app.detection.trend import TrendAnalyser
from app.detection.window import SlidingWindowDetector
from app.ingestion.base import SignalIngester


class DetectionPipeline:
    """
    Async pipeline: consumes Signals from a SignalIngester, routes each metric
    signal through the three detectors, and yields a RiskScore per update.

    All detector instances are injectable for testing; defaults construct the
    standard implementations with config-driven parameters.
    """

    def __init__(
        self,
        ingester: SignalIngester,
        window_detector: SlidingWindowDetector | None = None,
        trend_analyser: TrendAnalyser | None = None,
        pattern_matcher: PatternMatcher | None = None,
    ) -> None:
        self.ingester = ingester
        self.window_detector = window_detector or SlidingWindowDetector()
        self.trend_analyser = trend_analyser or TrendAnalyser()
        self.pattern_matcher = pattern_matcher or PatternMatcher()
        self.risk_scorer = RiskScorer(
            self.window_detector, self.trend_analyser, self.pattern_matcher
        )

    async def run(self) -> AsyncIterator[RiskScore]:  # type: ignore[override]
        async for signal in self.ingester.read_signals():
            if signal.signal_type != "metric":
                continue
            self.window_detector.add_signal(signal)
            self.trend_analyser.add_signal(signal)
            self.pattern_matcher.add_signal(signal)
            yield self.risk_scorer.score(signal.service, scenario=signal.scenario)
