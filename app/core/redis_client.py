"""
Async Redis client with graceful degradation.

All public methods return safe defaults when Redis is unavailable — they never
raise. The DLQ (dead-letter queue) uses a Redis list keyed ``dlq:incidents``.
"""

from __future__ import annotations

import json
from typing import Any

import structlog

logger = structlog.get_logger(__name__)


class RedisClient:
    DLQ_KEY = "dlq:incidents"

    def __init__(self, url: str) -> None:
        self._url = url
        self._client: Any = None
        self.available = False

    async def connect(self) -> None:
        try:
            import redis.asyncio as aioredis

            self._client = aioredis.from_url(self._url, decode_responses=True)
            await self._client.ping()
            self.available = True
            logger.info("redis_connected", url=self._url)
        except Exception as exc:
            logger.warning("redis_unavailable", error=str(exc))
            self.available = False

    async def close(self) -> None:
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception:
                pass
            self._client = None

    async def push_to_dlq(self, data: dict[str, Any]) -> None:
        if not self._check("push_to_dlq"):
            return
        try:
            await self._client.rpush(self.DLQ_KEY, json.dumps(data, default=str))
        except Exception as exc:
            logger.warning("redis_dlq_push_failed", error=str(exc))

    async def pop_from_dlq(self) -> dict[str, Any] | None:
        if not self._check("pop_from_dlq"):
            return None
        try:
            raw = await self._client.lpop(self.DLQ_KEY)
            return json.loads(raw) if raw else None
        except Exception as exc:
            logger.warning("redis_dlq_pop_failed", error=str(exc))
            return None

    async def dlq_length(self) -> int:
        if not self.available or self._client is None:
            return 0
        try:
            return int(await self._client.llen(self.DLQ_KEY))
        except Exception as exc:
            logger.warning("redis_dlq_length_failed", error=str(exc))
            return 0

    async def ping(self) -> bool:
        if not self.available or self._client is None:
            return False
        try:
            return bool(await self._client.ping())
        except Exception:
            return False

    def _check(self, operation: str) -> bool:
        if not self.available or self._client is None:
            logger.warning("redis_op_skipped", op=operation, reason="unavailable")
            return False
        return True
