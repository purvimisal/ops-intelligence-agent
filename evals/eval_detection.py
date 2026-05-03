"""
Detection layer evaluation.

Metrics measured (all deterministic — no API, no DB required):
  - Recall      ≥ 0.90   detected_scenarios / 10
  - Precision   ≥ 0.85   TP / (TP + FP)
  - FPR         ≤ 0.10   false_alerts / clean_window_count
  - MTTD        ≤ 60 sec  wall-clock processing time for all 10 scenarios

"Detected" = at least one AnomalyEvent fires during scenario signal replay.
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from evals.conftest import (
    FPR_THRESHOLD,
    MTTD_SEC_THRESHOLD,
    PRECISION_THRESHOLD,
    RECALL_THRESHOLD,
    load_all_scenarios,
    make_clean_signals,
    replay_for_anomalies,
    scenario_to_signals,
)

# 10 fake service names used for clean window testing (one per scenario slot)
_CLEAN_SERVICES = [f"clean-svc-{i:02d}" for i in range(10)]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run_all_scenarios() -> list[tuple[str, bool]]:
    """Return (scenario_name, detected) for all 10 scenarios."""
    results = []
    for sc in load_all_scenarios():
        signals = scenario_to_signals(sc)
        detected, _ = replay_for_anomalies(signals)
        results.append((sc["name"], detected))
    return results


def _run_clean_windows() -> list[tuple[str, bool]]:
    """Return (service_name, false_positive) for 10 clean windows."""
    results = []
    for svc in _CLEAN_SERVICES:
        signals = make_clean_signals(svc, n=30)
        detected, _ = replay_for_anomalies(signals)
        results.append((svc, detected))
    return results


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_recall_all_scenarios_detected() -> None:
    """Recall ≥ 0.90 — detection must fire on ≥ 9 of 10 failure scenarios."""
    results = _run_all_scenarios()
    true_positives = sum(1 for _, detected in results if detected)
    total = len(results)
    recall = true_positives / total

    failed = [name for name, detected in results if not detected]
    assert recall >= RECALL_THRESHOLD, (
        f"Recall {recall:.2f} < threshold {RECALL_THRESHOLD}. "
        f"Missed scenarios: {failed}"
    )
    print(f"\n  Recall: {recall:.2f}  ({true_positives}/{total} scenarios detected)")


def test_false_positive_rate_clean_windows() -> None:
    """FPR ≤ 0.10 — clean (normal-metric) windows must not trigger anomaly events."""
    results = _run_clean_windows()
    false_positives = sum(1 for _, fp in results if fp)
    total = len(results)
    fpr = false_positives / total

    fp_services = [svc for svc, fp in results if fp]
    assert fpr <= FPR_THRESHOLD, (
        f"FPR {fpr:.2f} > threshold {FPR_THRESHOLD}. "
        f"False positive services: {fp_services}"
    )
    print(f"\n  FPR: {fpr:.2f}  ({false_positives}/{total} clean windows triggered)")


def test_precision_no_false_detections() -> None:
    """Precision ≥ 0.85 — TP / (TP + FP) across both scenario and clean sets."""
    scenario_results = _run_all_scenarios()
    clean_results = _run_clean_windows()

    true_positives = sum(1 for _, d in scenario_results if d)
    false_positives = sum(1 for _, d in clean_results if d)

    if (true_positives + false_positives) == 0:
        pytest.skip("No detections — cannot compute precision")

    precision = true_positives / (true_positives + false_positives)
    assert precision >= PRECISION_THRESHOLD, (
        f"Precision {precision:.2f} < threshold {PRECISION_THRESHOLD}. "
        f"TP={true_positives}, FP={false_positives}"
    )
    print(f"\n  Precision: {precision:.2f}  (TP={true_positives}, FP={false_positives})")


def test_mttd_processing_latency() -> None:
    """MTTD ≤ 60 sec — detection pipeline processes all 10 scenarios in under 60 s."""
    start = time.monotonic()
    for sc in load_all_scenarios():
        signals = scenario_to_signals(sc)
        replay_for_anomalies(signals)
    elapsed = time.monotonic() - start

    assert elapsed < MTTD_SEC_THRESHOLD, (
        f"Detection pipeline took {elapsed:.2f}s > {MTTD_SEC_THRESHOLD}s threshold"
    )
    print(f"\n  Total detection time: {elapsed * 1000:.1f} ms for 10 scenarios")


def test_risk_score_increases_across_scenario() -> None:
    """Risk score must trend upward as failure signals accumulate."""
    scenarios = load_all_scenarios()
    failures = 0

    for sc in scenarios:
        signals = scenario_to_signals(sc)
        _, scores = replay_for_anomalies(signals)
        if not scores:
            continue
        initial = scores[0].score
        final = scores[-1].score
        if final <= initial:
            failures += 1

    assert failures == 0, (
        f"{failures}/{len(scenarios)} scenarios did not show increasing risk over time"
    )


def test_all_scenarios_produce_top_signals() -> None:
    """Every scenario with anomalies must populate top_signals on the RiskScore."""
    scenarios = load_all_scenarios()
    missing = []

    for sc in scenarios:
        signals = scenario_to_signals(sc)
        _, scores = replay_for_anomalies(signals)
        final = scores[-1] if scores else None
        if final is None or (final.score > 0 and not final.top_signals):
            missing.append(sc["name"])

    assert not missing, (
        f"Scenarios with non-zero score but no top_signals: {missing}"
    )


def test_pattern_matcher_fires_on_all_scenarios() -> None:
    """Pattern matcher must fire (similarity ≥ 0.75) for each of the 10 scenarios."""
    scenarios = load_all_scenarios()
    no_match = []

    for sc in scenarios:
        signals = scenario_to_signals(sc)
        _, scores = replay_for_anomalies(signals)
        final = scores[-1] if scores else None
        if final is None or final.pattern_score < 75.0:
            no_match.append(f"{sc['name']} (pattern_score={final.pattern_score:.1f if final else 0})")

    assert not no_match, (
        f"Scenarios where pattern matcher did not fire: {no_match}"
    )


def test_scenario_report() -> None:
    """Print per-scenario detection summary (always passes — informational only)."""
    print("\n\n  === Detection eval report ===")
    print(f"  {'Scenario':<40} {'Detected':<10} {'Max score':<12} {'Pattern':<10}")
    print(f"  {'-'*40} {'-'*10} {'-'*12} {'-'*10}")

    scenarios = load_all_scenarios()
    for sc in scenarios:
        signals = scenario_to_signals(sc)
        detected, scores = replay_for_anomalies(signals)
        max_score = max((rs.score for rs in scores), default=0.0)
        pattern = max((rs.pattern_score for rs in scores), default=0.0)
        status = "YES" if detected else "NO "
        print(
            f"  {sc['name']:<40} {status:<10} {max_score:<12.1f} {pattern:<10.1f}"
        )
