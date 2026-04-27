# Runbook: Database Connection Pool Exhaustion

## Overview
Connection pool exhaustion occurs when all available database connections are in use and new requests cannot acquire a connection, resulting in timeouts and errors. This is a common failure mode during traffic spikes or when long-running queries hold connections open.

## Symptoms
- Application errors: `connection pool exhausted`, `too many connections`, `FATAL: remaining connection slots reserved`
- `error_rate` rising sharply on DB-dependent services
- `memory_pct` elevated on application pods (connections held in memory)
- Slow query log showing long-running transactions
- `pg_stat_activity` showing connections in `idle in transaction` state
- Database server: `max_connections` limit approached or reached

## Root Cause Patterns
1. **Connection leak**: Code paths that don't return connections to the pool (missing `conn.close()`, exception bypassing context manager).
2. **Long-running transactions**: Queries holding locks or transactions open for minutes due to missing COMMIT, slow batch jobs, or stuck background workers.
3. **Traffic spike without pool scaling**: Sudden request volume exceeds connection pool size.
4. **Slow queries causing thread stacking**: Unindexed query causes sequential scans; each request waits, holding connections open while waiting for results.
5. **Pool configuration mismatch**: Pool min/max sized for average traffic, not peak; no queuing timeout configured.

## Immediate Actions
1. Check current connection state:
   ```sql
   SELECT state, count(*), max(now() - state_change) AS oldest
   FROM pg_stat_activity
   WHERE datname = '<your_db>'
   GROUP BY state;
   ```
2. Identify and kill idle-in-transaction connections older than 5 minutes:
   ```sql
   SELECT pg_terminate_backend(pid)
   FROM pg_stat_activity
   WHERE state = 'idle in transaction'
     AND state_change < NOW() - INTERVAL '5 minutes';
   ```
3. Check pool utilization in application metrics (e.g., asyncpg `pool_size`, `pool_free`).
4. If traffic spike: scale application replicas down (paradoxically reduces total connections) or add a PgBouncer pooler in front of Postgres.
5. Kill long-running queries blocking others:
   ```sql
   SELECT pg_terminate_backend(pid)
   FROM pg_stat_activity
   WHERE query_start < NOW() - INTERVAL '10 minutes'
     AND state != 'idle';
   ```
6. Temporarily increase `max_connections` on Postgres if headroom exists:
   ```sql
   ALTER SYSTEM SET max_connections = 200;
   SELECT pg_reload_conf();
   ```
   Note: requires enough shared memory; monitor RAM usage.

## Escalation Thresholds
- Connection count at > 90% of `max_connections` → immediate action required
- Growing queue of idle-in-transaction connections → suspect connection leak, page dev lead
- Killing connections does not reduce count (new ones appear immediately) → suspect leak in hot path

## Prevention
- Set `pool_timeout` (fail fast if pool exhausted) and `statement_timeout` (kill long queries).
- Deploy PgBouncer or equivalent connection pooler in transaction mode for high-throughput services.
- Alert on `idle in transaction` connections older than 60 seconds.
- Code review checklist: all DB connections must be acquired via context manager (async with pool.acquire()).
- Regular `EXPLAIN ANALYZE` reviews on queries that run > 100ms.

## Related Runbooks
- runbook_kafka_consumer_lag.md (if consumers are blocked on DB writes)
- runbook_cascade_5xx.md (if DB exhaustion is causing upstream 5xx cascade)
