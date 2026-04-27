from __future__ import annotations

import asyncpg
from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)

_pool: asyncpg.Pool | None = None


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(dsn=settings.database_url, min_size=2, max_size=10)
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


async def init_db() -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS signals (
                id          BIGSERIAL PRIMARY KEY,
                ts          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                service     TEXT NOT NULL,
                signal_type TEXT NOT NULL,  -- 'log' | 'metric'
                level       TEXT,           -- log level or NULL for metrics
                message     TEXT,
                trace_id    TEXT,
                duration_ms FLOAT,
                error_code  TEXT,
                metrics     JSONB,          -- metric snapshot fields
                scenario    TEXT,           -- injected scenario name or NULL
                created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS signals_ts_idx ON signals (ts DESC)
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS signals_service_idx ON signals (service, ts DESC)
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS incidents (
                id           BIGSERIAL PRIMARY KEY,
                service      TEXT NOT NULL,
                severity     TEXT NOT NULL,
                summary      TEXT NOT NULL,
                root_cause   TEXT,
                hypotheses   JSONB,
                fix_steps    JSONB,
                risk_score   FLOAT,
                scenario     TEXT,
                langfuse_url TEXT,
                status       TEXT NOT NULL DEFAULT 'open',
                created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                resolved_at  TIMESTAMPTZ
            )
        """)
        # Drop chunks table if it was created with a different embedding dimension
        await conn.execute("""
            DO $$
            BEGIN
                IF EXISTS (
                    SELECT 1 FROM pg_attribute a
                    JOIN pg_class c ON c.oid = a.attrelid
                    WHERE c.relname = 'chunks'
                      AND a.attname = 'embedding'
                      AND a.atttypmod <> 768
                      AND a.attnum > 0
                ) THEN
                    DROP TABLE chunks CASCADE;
                END IF;
            END $$
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS chunks (
                id           BIGSERIAL PRIMARY KEY,
                runbook      TEXT NOT NULL,
                section      TEXT NOT NULL,
                content      TEXT NOT NULL,
                embedding    vector(768),
                content_hash TEXT UNIQUE,
                created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS chunks_embedding_idx
            ON chunks USING ivfflat (embedding vector_cosine_ops)
            WITH (lists = 50)
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS risk_scores (
                id           BIGSERIAL PRIMARY KEY,
                service      TEXT NOT NULL,
                score        FLOAT NOT NULL,
                trend_score  FLOAT,
                pattern_score FLOAT,
                llm_score    FLOAT,
                scenario     TEXT,
                ts           TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS risk_scores_service_ts_idx
            ON risk_scores (service, ts DESC)
        """)
    logger.info("db_init_complete")
