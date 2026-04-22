"""
Ingestion layer interface.

All signal sources implement SignalIngester and emit Signal objects.
Current implementation: SyntheticIngester (app/ingestion/generator.py).

Future adapters that will implement this same interface:
  - KafkaIngester      — consume from a real Kafka topic via aiokafka
  - ElasticsearchIngester — tail logs from an ES index via scroll/search_after
  - K8sEventIngester   — stream pod/node events from the Kubernetes watch API
  - PrometheusIngester — pull metric snapshots from a Prometheus HTTP endpoint

Keeping all adapters behind this interface means the detection layer never
imports a concrete source; swapping data sources requires no detection changes.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, AsyncIterator


@dataclass
class Signal:
    """
    Unified signal envelope for both log events and metric snapshots.

    Matches the `signals` table schema in app/core/db.py so that any
    ingester can write directly to the DB without a translation step.
    """

    signal_type: str                        # "log" | "metric"
    service: str
    ts: datetime
    level: str | None = None               # log level; None for metrics
    message: str | None = None             # log message; None for metrics
    trace_id: str | None = None            # request trace ID; None for metrics
    duration_ms: float | None = None       # request duration; None for metrics
    error_code: str | None = None          # structured error code; None for metrics
    metrics: dict[str, Any] = field(default_factory=dict)   # metric fields; empty for logs
    scenario: str | None = None            # injected scenario name; None for baseline signals

    def to_dict(self) -> dict[str, Any]:
        return {
            "signal_type": self.signal_type,
            "service": self.service,
            "ts": self.ts.isoformat(),
            "level": self.level,
            "message": self.message,
            "trace_id": self.trace_id,
            "duration_ms": self.duration_ms,
            "error_code": self.error_code,
            "metrics": self.metrics,
            "scenario": self.scenario,
        }


class SignalIngester(ABC):
    """
    Abstract base class for all signal sources.

    Subclasses implement read_signals() as an async generator and close()
    for resource cleanup. The default __aenter__/__aexit__ delegate to close()
    so all ingesters work as async context managers without extra boilerplate.

    Usage::

        async with SomeIngester(...) as ingester:
            async for signal in ingester.read_signals():
                await process(signal)
    """

    @abstractmethod
    def read_signals(self) -> AsyncIterator[Signal]:
        """Yield Signal objects from the source. Implementations use async def + yield."""
        ...  # pragma: no cover

    @abstractmethod
    async def close(self) -> None:
        """Release any held resources (connections, file handles, etc.)."""
        ...  # pragma: no cover

    async def __aenter__(self) -> "SignalIngester":
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()
