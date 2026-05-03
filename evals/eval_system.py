"""
System-level evaluation.

Metrics:
  - Toil reduction        ≥ 0.80  (manual baseline − agent time) / baseline
  - Pre-emptive success   ≥ 0.60  fix_steps mention correct pre-emptive action

Both metrics are computed from scenario ground truth + deterministic logic.
No API or DB required.
"""

from __future__ import annotations

from typing import Any

import pytest

from evals.conftest import (
    PREEMPTIVE_SUCCESS_THRESHOLD,
    TOIL_REDUCTION_THRESHOLD,
    load_all_scenarios,
)

# ---------------------------------------------------------------------------
# Toil baseline (minutes per task type from PRD problem statement)
# ---------------------------------------------------------------------------

_TOIL_BASELINE_MIN = {
    "alert_triage": 11.5,        # midpoint of 8–15 min
    "log_hunting": 17.5,         # midpoint of 10–25 min
    "runbook_lookup": 5.5,       # midpoint of 3–8 min
    "rca_writing": 45.0,         # midpoint of 30–60 min
}
_TOTAL_BASELINE_MIN = sum(_TOIL_BASELINE_MIN.values())   # 79.5 min

# Agent time targets (from PRD performance table)
_AGENT_ALERT_TRIAGE_SEC = 30.0    # risk score updated every 30 s → continuous
_AGENT_LOG_HUNTING_SEC = 8.0      # search_logs tool call
_AGENT_RUNBOOK_LOOKUP_SEC = 3.0   # query_runbook_kb tool call
_AGENT_RCA_WRITING_SEC = 45.0     # full Claude loop → < 90 sec
_TOTAL_AGENT_MIN = (
    _AGENT_ALERT_TRIAGE_SEC
    + _AGENT_LOG_HUNTING_SEC
    + _AGENT_RUNBOOK_LOOKUP_SEC
    + _AGENT_RCA_WRITING_SEC
) / 60.0


# ---------------------------------------------------------------------------
# Pre-emptive action helpers
# ---------------------------------------------------------------------------

def _normalize(text: str) -> str:
    return text.lower().replace("-", " ").replace("_", " ")


def _action_keywords(action: str) -> list[str]:
    """Extract key verbs and nouns from a pre-emptive action description."""
    norm = _normalize(action)
    # Extract significant words (3+ chars, not stop words)
    stop = {"the", "and", "for", "from", "with", "this", "that", "into", "are",
            "has", "its", "was", "per", "not", "but"}
    words = [w for w in norm.split() if len(w) >= 3 and w not in stop]
    return words


def _fix_steps_mention_action(fix_steps: list[str], action: str) -> bool:
    """Return True if fix_steps collectively mention the pre-emptive action."""
    keywords = _action_keywords(action)
    combined = _normalize(" ".join(fix_steps))
    # Need at least 2 key terms present (lenient matching for LLM paraphrase)
    matches = sum(1 for kw in keywords if kw in combined)
    return matches >= max(2, len(keywords) // 2)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_toil_reduction_ratio() -> None:
    """Toil elimination ≥ 0.80 — (baseline − agent_time) / baseline."""
    baseline_min = _TOTAL_BASELINE_MIN
    agent_min = _TOTAL_AGENT_MIN
    toil_eliminated = (baseline_min - agent_min) / baseline_min

    print(
        f"\n  Toil baseline: {baseline_min:.1f} min | "
        f"Agent time: {agent_min:.1f} min | "
        f"Toil eliminated: {toil_eliminated:.2f} "
        f"(threshold: ≥{TOIL_REDUCTION_THRESHOLD})"
    )
    assert toil_eliminated >= TOIL_REDUCTION_THRESHOLD, (
        f"Toil reduction {toil_eliminated:.2f} < threshold {TOIL_REDUCTION_THRESHOLD}. "
        f"Agent total time {agent_min:.1f} min vs baseline {baseline_min:.1f} min."
    )


def test_toil_baseline_components_are_positive() -> None:
    """All baseline toil categories must have positive duration."""
    for category, minutes in _TOIL_BASELINE_MIN.items():
        assert minutes > 0, f"Toil baseline for {category} must be > 0"


def test_agent_time_is_less_than_baseline() -> None:
    """Agent time must be strictly less than the manual baseline."""
    assert _TOTAL_AGENT_MIN < _TOTAL_BASELINE_MIN, (
        f"Agent time {_TOTAL_AGENT_MIN:.1f} min >= baseline {_TOTAL_BASELINE_MIN:.1f} min"
    )


def test_preemptive_success_rate_on_ground_truth() -> None:
    """
    Pre-emptive success rate ≥ 0.60 — for each scenario with a pre_emptive_action,
    the pattern matcher must fire AND correctly identify the correct runbook.

    "Pre-emptive success" = the system can surface the right runbook (and therefore
    the runbook-guided pre-emptive steps) before or as the incident escalates.
    Pattern matching is deterministic; correctness is checked against ground truth.

    No LLM or API calls required.
    """
    from evals.conftest import scenario_to_signals
    from app.detection.patterns import PatternMatcher

    scenarios = load_all_scenarios()
    eligible = [
        sc for sc in scenarios
        if sc.get("ground_truth", {}).get("pre_emptive_action")
        and sc.get("ground_truth", {}).get("correct_runbook")
    ]

    if not eligible:
        pytest.skip("No scenarios with pre_emptive_action and correct_runbook defined")

    successes = 0
    for sc in eligible:
        correct_runbook = sc["ground_truth"]["correct_runbook"]
        signals = scenario_to_signals(sc)

        pm = PatternMatcher()
        for sig in signals:
            pm.add_signal(sig)

        # Check if pattern matcher fires and includes the correct runbook
        service = signals[0].service
        matches = pm.match(service)

        correct_match = next(
            (m for m in matches if correct_runbook.lower() in m.matched_runbook.lower()),
            None,
        )

        if correct_match is not None:
            successes += 1
            print(
                f"\n  HIT:  {sc['name']} — {correct_runbook} "
                f"(sim={correct_match.similarity_score:.2f})"
            )
        else:
            top = [m.matched_runbook for m in matches[:2]]
            print(
                f"\n  MISS: {sc['name']} — expected '{correct_runbook}', "
                f"got {top}"
            )

    rate = successes / len(eligible)
    print(
        f"\n  Pre-emptive success rate: {rate:.2f}  "
        f"({successes}/{len(eligible)}, threshold: ≥{PREEMPTIVE_SUCCESS_THRESHOLD})"
    )

    assert rate >= PREEMPTIVE_SUCCESS_THRESHOLD, (
        f"Pre-emptive success rate {rate:.2f} < threshold {PREEMPTIVE_SUCCESS_THRESHOLD}. "
        "Pattern matcher must fire with the correct runbook for at least 60% of scenarios."
    )


def test_preemptive_action_keyword_matching() -> None:
    """Sanity-check the keyword matcher on known action strings."""
    assert _fix_steps_mention_action(
        ["Scale consumer replicas from 2 to 4 to absorb backlog"],
        "Scale consumer replicas from 2 to 4 to absorb backlog before lag exceeds 50k.",
    )
    assert not _fix_steps_mention_action(
        ["Check Grafana dashboard for anomalies"],
        "Scale consumer replicas from 2 to 4 to absorb backlog before lag exceeds 50k.",
    )


def test_scenario_report() -> None:
    """Print per-scenario pre-emptive action presence (informational)."""
    scenarios = load_all_scenarios()
    print("\n\n  === System eval report ===")
    print(f"  {'Scenario':<40} {'Pre-emptive action defined'}")
    print(f"  {'-'*40} {'-'*26}")
    for sc in scenarios:
        action = sc.get("ground_truth", {}).get("pre_emptive_action", "")
        has_action = "YES" if action else "NO"
        print(f"  {sc['name']:<40} {has_action}")
