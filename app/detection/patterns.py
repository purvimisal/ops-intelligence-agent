"""
Pre-incident pattern matcher.

Loads the 10 pre-incident fingerprints from scenarios/ at init time.
Maintains a rolling window of metric snapshots per service and compares it
against each fingerprint using cosine similarity on a mean-centered feature vector.

Feature vector: 6 metrics × 30 steps = 180 dimensions.
Each feature is scaled to a common magnitude then mean-centred within the window
(subtract per-feature mean, do NOT divide by std).  This means:
  - Features with large variation dominate the vector (correct: consumer_lag
    change of 48000 should matter more than memory jitter of ±2).
  - Constant features collapse to zero and contribute nothing (flat baselines
    produce an all-zero vector → None → no match).
  - Two instances of the same scenario with different noise realizations score
    high cosine similarity because the dominant deterministic features match.
"""

from __future__ import annotations

import json
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from app.core.config import settings
from app.ingestion.base import Signal

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

# The 6 features extracted from each metric snapshot, in order.
FEATURE_KEYS = ["error_rate", "consumer_lag", "memory_pct", "cpu_pct", "disk_pct", "throughput"]

# Fixed scaling brings each metric to roughly the same order of magnitude
# before mean-centering so no single high-range metric drowns out the others.
FEATURE_SCALES: dict[str, float] = {
    "error_rate":   100.0,   # fraction [0, 1]   → [0, 100]
    "consumer_lag":   0.002, # [0, ~50 000]      → [0, ~100]
    "memory_pct":     1.0,   # already [0, 100]
    "cpu_pct":        1.0,   # already [0, 100]
    "disk_pct":       1.0,   # already [0, 100]
    "throughput":     0.1,   # [0, ~1 000]       → [0, ~100]
}

_WINDOW_STEPS = 30


def _vectorise(steps: list[dict[str, Any]]) -> np.ndarray | None:
    """
    Build a mean-centred, L2-normalised feature vector.

    1. Apply fixed scaling (brings all metrics to ~[0, 100]).
    2. Subtract per-feature mean within the window (centre, but do NOT
       divide by std so large-change features keep their relative dominance).
    3. L2-normalise the flattened vector.

    A window where all features are constant produces an all-zero centred
    vector → returns None → no match (flat baseline never triggers alerts).
    """
    rows = np.array(
        [[float(m.get(k, 0.0)) * FEATURE_SCALES[k] for k in FEATURE_KEYS] for m in steps],
        dtype=float,
    )  # shape (steps, features)

    centred = rows - rows.mean(axis=0)   # subtract per-feature mean
    vec = centred.flatten()
    norm = np.linalg.norm(vec)
    if norm < 1e-9:
        return None  # all features constant (flat baseline) → no detectable pattern
    return vec / norm


def _cosine(a: np.ndarray, b: np.ndarray) -> float:
    """Cosine similarity between two unit vectors, clipped to [-1, 1]."""
    return float(np.clip(np.dot(a, b), -1.0, 1.0))


@dataclass
class PatternMatch:
    scenario_name: str
    similarity_score: float   # cosine similarity in [0, 1]
    matched_runbook: str
    confidence: str           # "HIGH" (>= 0.90) | "MEDIUM" (>= 0.75)


class PatternMatcher:
    """
    Loads fingerprints from scenarios/ at init and compares incoming rolling
    windows against them.  Only returns matches where similarity >= threshold.
    """

    def __init__(
        self,
        scenarios_dir: Path | None = None,
        threshold: float | None = None,
    ) -> None:
        self._threshold = threshold if threshold is not None else settings.pattern_similarity_threshold
        self._windows: dict[str, deque[dict[str, Any]]] = defaultdict(
            lambda: deque(maxlen=_WINDOW_STEPS)
        )
        # list of (scenario_name, runbook, unit_vector)
        self._fingerprints: list[tuple[str, str, np.ndarray]] = []
        self._load_fingerprints(scenarios_dir or (_PROJECT_ROOT / "scenarios"))

    def _load_fingerprints(self, path: Path) -> None:
        if not path.exists():
            return
        for fp in sorted(path.glob("*.json")):
            with open(fp) as f:
                data = json.load(f)
            steps = [
                s for s in data.get("signal_shape", [])
                if isinstance(s.get("throughput"), (int, float))
            ]
            if len(steps) < _WINDOW_STEPS:
                continue
            vec = _vectorise(steps[-_WINDOW_STEPS:])
            if vec is None:
                continue
            runbook = data.get("ground_truth", {}).get("correct_runbook", "")
            self._fingerprints.append((data["name"], runbook, vec))

    def add_signal(self, signal: Signal) -> None:
        if signal.signal_type != "metric" or not signal.metrics:
            return
        self._windows[signal.service].append(dict(signal.metrics))

    def match(self, service: str) -> list[PatternMatch]:
        """
        Compare the current rolling window for service against all fingerprints.
        Returns an empty list until the window has >= _WINDOW_STEPS entries.
        """
        window = list(self._windows.get(service, deque()))
        if len(window) < _WINDOW_STEPS:
            return []

        incoming = _vectorise(window[-_WINDOW_STEPS:])
        if incoming is None:
            return []

        matches: list[PatternMatch] = []
        for name, runbook, fingerprint in self._fingerprints:
            sim = _cosine(incoming, fingerprint)
            if sim >= self._threshold:
                matches.append(PatternMatch(
                    scenario_name=name,
                    similarity_score=sim,
                    matched_runbook=runbook,
                    confidence="HIGH" if sim >= 0.90 else "MEDIUM",
                ))

        return sorted(matches, key=lambda m: m.similarity_score, reverse=True)

    def fingerprint_count(self) -> int:
        return len(self._fingerprints)
