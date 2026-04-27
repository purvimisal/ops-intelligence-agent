"""
Circuit breaker protecting the Claude API integration.

States:
  CLOSED    — normal operation; all LLM calls proceed
  OPEN      — too many consecutive failures; LLM calls are skipped entirely
  HALF_OPEN — recovery probe; one call is allowed through to test recovery

Transitions:
  CLOSED → OPEN      after `failure_threshold` consecutive failures
  OPEN   → HALF_OPEN after `recovery_timeout_sec` seconds
  HALF_OPEN → CLOSED on first successful call
  HALF_OPEN → OPEN   on failure
"""

from __future__ import annotations

import time
from enum import Enum

import structlog

logger = structlog.get_logger(__name__)


class CBState(str, Enum):
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"


class CircuitBreaker:
    def __init__(
        self,
        failure_threshold: int = 5,
        recovery_timeout_sec: float = 60.0,
    ) -> None:
        self._failure_threshold = failure_threshold
        self._recovery_timeout = recovery_timeout_sec
        self._consecutive_failures = 0
        self._opened_at: float | None = None
        self._state = CBState.CLOSED

    @property
    def state(self) -> str:
        if self._state == CBState.OPEN and self._opened_at is not None:
            if time.monotonic() - self._opened_at >= self._recovery_timeout:
                return CBState.HALF_OPEN.value
        return self._state.value

    def is_open(self) -> bool:
        return self.state == CBState.OPEN.value

    def is_closed(self) -> bool:
        return self.state == CBState.CLOSED.value

    def is_half_open(self) -> bool:
        return self.state == CBState.HALF_OPEN.value

    def record_success(self) -> None:
        prev = self.state
        self._consecutive_failures = 0
        self._state = CBState.CLOSED
        self._opened_at = None
        if prev != CBState.CLOSED.value:
            logger.info("circuit_breaker_closed", previous_state=prev)

    def record_failure(self) -> None:
        self._consecutive_failures += 1
        # Use the computed state so HALF_OPEN (derived from timeout) is detected
        current = self.state
        if current == CBState.HALF_OPEN.value or (
            self._consecutive_failures >= self._failure_threshold
            and current == CBState.CLOSED.value
        ):
            self._state = CBState.OPEN
            self._opened_at = time.monotonic()
            logger.warning(
                "circuit_breaker_opened",
                consecutive_failures=self._consecutive_failures,
            )
        else:
            logger.warning(
                "circuit_breaker_failure_recorded",
                consecutive_failures=self._consecutive_failures,
                threshold=self._failure_threshold,
            )

    @property
    def consecutive_failures(self) -> int:
        return self._consecutive_failures
