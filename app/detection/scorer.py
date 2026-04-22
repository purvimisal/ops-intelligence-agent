"""
Composite risk scorer.

Combines the three detection components into a single RiskScore per service.
Formula (from PRD): 0.35 * trend_score + 0.35 * pattern_score + 0.30 * anomaly_score

No LLM calls — fully deterministic.  Runs independently of the Claude API so
the detection layer continues working during LLM outages (graceful degradation).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

import structlog

from app.detection.patterns import PatternMatch, PatternMatcher
from app.detection.trend import TrendAnalyser, TrendReport
from app.detection.window import AnomalyEvent, SlidingWindowDetector
from app.core.config import settings

logger = structlog.get_logger(__name__)


@dataclass
class RiskScore:
    service: str
    score: float                         # composite 0–100
    anomaly_score: float                 # 0–100 component (30% weight)
    trend_score: float                   # 0–100 component (35% weight)
    pattern_score: float                 # 0–100 component (35% weight)
    time_to_incident_min: float | None   # soonest projected threshold breach
    top_signals: list[str]              = field(default_factory=list)  # plain-English rationale
    ts: datetime                         = field(default_factory=lambda: datetime.now(timezone.utc))
    scenario: str | None                 = None


# ---------------------------------------------------------------------------
# Component score helpers
# ---------------------------------------------------------------------------

def _anomaly_score(anomalies: list[AnomalyEvent]) -> float:
    """0–100: 100 for any CRITICAL breach; 0–70 scaled for WARNING."""
    score = 0.0
    for a in anomalies:
        if a.severity == "CRITICAL":
            return 100.0
        warn = getattr(settings, f"{a.metric}_warn")
        crit = getattr(settings, f"{a.metric}_critical")
        frac = min(1.0, (a.value - warn) / (crit - warn)) if crit > warn else 1.0
        score = max(score, frac * 70.0)
    return score


_URGENCY_SCORE = {"HIGH": 100.0, "MEDIUM": 60.0, "LOW": 30.0, "NONE": 0.0}


def _trend_score(trends: list[TrendReport]) -> tuple[float, float | None]:
    """Returns (trend_score 0–100, soonest time_to_threshold_min)."""
    best = 0.0
    soonest: float | None = None
    for t in trends:
        best = max(best, _URGENCY_SCORE.get(t.urgency, 0.0))
        if t.time_to_threshold_min is not None:
            if soonest is None or t.time_to_threshold_min < soonest:
                soonest = t.time_to_threshold_min
    return best, soonest


def _pattern_score(matches: list[PatternMatch]) -> float:
    """0–100: best similarity × 100."""
    return max((m.similarity_score for m in matches), default=0.0) * 100.0


def _build_top_signals(
    anomalies: list[AnomalyEvent],
    trends: list[TrendReport],
    matches: list[PatternMatch],
) -> list[str]:
    signals: list[str] = []

    # Anomalies — worst first
    for a in sorted(anomalies, key=lambda x: x.severity == "CRITICAL", reverse=True):
        signals.append(
            f"{a.metric} at {a.value:.3g} exceeded {a.severity} threshold {a.threshold:.3g}"
        )

    # Trends — HIGH/MEDIUM urgency only
    for t in sorted(trends, key=lambda x: x.urgency == "HIGH", reverse=True):
        if t.urgency in ("HIGH", "MEDIUM"):
            ttm = f"{t.time_to_threshold_min:.1f} min" if t.time_to_threshold_min is not None else "—"
            signals.append(
                f"{t.metric} trend: {ttm} to threshold"
                f" (slope {t.slope:+.3g}/step, R²={t.confidence:.2f}, {t.urgency})"
            )

    # Pattern matches
    for m in matches:
        signals.append(
            f"Pattern match: {m.scenario_name} ({m.similarity_score * 100:.1f}% similarity)"
            f" → {m.matched_runbook}"
        )

    return signals[:5]  # cap at 5 — LLM context budget


# ---------------------------------------------------------------------------
# Scorer
# ---------------------------------------------------------------------------

class RiskScorer:
    """
    Pure-computation risk scorer.  Inject the three detector instances;
    call score() after each new metric signal is processed.
    """

    def __init__(
        self,
        window_detector: SlidingWindowDetector,
        trend_analyser: TrendAnalyser,
        pattern_matcher: PatternMatcher,
    ) -> None:
        self._window = window_detector
        self._trend = trend_analyser
        self._pattern = pattern_matcher

    def score(self, service: str, scenario: str | None = None) -> RiskScore:
        anomalies = self._window.check(service)
        trends = self._trend.analyse(service)
        matches = self._pattern.match(service)

        a = _anomaly_score(anomalies)
        t, soonest = _trend_score(trends)
        p = _pattern_score(matches)

        composite = min(100.0, max(0.0, 0.30 * a + 0.35 * t + 0.35 * p))

        top = _build_top_signals(anomalies, trends, matches)

        rs = RiskScore(
            service=service,
            score=composite,
            anomaly_score=a,
            trend_score=t,
            pattern_score=p,
            time_to_incident_min=soonest,
            top_signals=top,
            scenario=scenario,
        )

        logger.info(
            "risk_score_updated",
            service=service,
            score=round(composite, 2),
            anomaly_score=round(a, 2),
            trend_score=round(t, 2),
            pattern_score=round(p, 2),
            time_to_incident_min=round(soonest, 1) if soonest is not None else None,
            top_signal=top[0] if top else None,
        )
        return rs
