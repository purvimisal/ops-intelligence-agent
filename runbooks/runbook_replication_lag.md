# Runbook: Multi-Region Replication Lag

## Overview
Replication lag between primary and replica regions creates a window where reads from replicas return stale data. High lag increases data loss risk (RPO) in a failover scenario and can cause user-visible consistency issues.

## Symptoms
- Replication lag metric > alerting threshold (e.g., > 30 seconds between regions)
- `consumer_lag` growing on cross-region Kafka replication topics
- Read queries returning data that's older than expected on replica endpoints
- Replica streaming replication showing large `write_lag`, `flush_lag`, or `replay_lag`
- Network metrics: elevated packet loss or high RTT between regions
- Replica Postgres: `pg_stat_replication` showing lag in bytes accumulating

## Root Cause Patterns
1. **Network degradation**: Increased latency or packet loss on the cross-region link reduces throughput of WAL streaming.
2. **Heavy write workload on primary**: Burst of large transactions generates WAL faster than it can be shipped and replayed.
3. **Replica I/O bottleneck**: Replica's disk cannot write WAL replay fast enough (undersized IOPS for the write rate).
4. **Vacuum/bloat on primary**: Aggressive autovacuum or VACUUM FULL generating heavy WAL that overwhelms replication bandwidth.
5. **Long-running transaction on primary**: Prevents WAL segments from being recycled and creates a replication slot lag.
6. **Replica CPU saturation**: Replay process starved of CPU during high-load periods.

## Immediate Actions
1. Check current replication lag on primary:
   ```sql
   SELECT client_addr, state, write_lag, flush_lag, replay_lag,
          pg_size_pretty(sent_lsn - replay_lsn) AS lag_bytes
   FROM pg_stat_replication;
   ```
2. Check network health between primary and replica regions (ping, MTR, cloud provider console).
3. If network is healthy, check replica's I/O and CPU utilization.
4. Check for long-running transactions blocking WAL recycling:
   ```sql
   SELECT pid, now() - xact_start AS duration, query
   FROM pg_stat_activity
   WHERE xact_start IS NOT NULL
   ORDER BY duration DESC LIMIT 5;
   ```
5. If Kafka MirrorMaker replication: check MirrorMaker consumer group lag per partition and check connector status.
6. If lag is growing rapidly and RPO is at risk, consider:
   - Redirecting all reads to primary temporarily (accept increased primary load)
   - Pausing non-critical batch writes to primary to reduce WAL generation rate
7. Once lag is recovering, monitor lag-per-minute trend to confirm it's decreasing.

## Escalation Thresholds
- Replication lag > 30 seconds → warning, investigate
- Replication lag > 5 minutes → P2, risk of stale reads affecting users
- Replication lag > 30 minutes → P1, failover RPO at risk; evaluate whether to pause writes

## Prevention
- Set two-tier alerting: 30s warning, 5 min critical.
- Right-size replica I/O to handle peak WAL generation rate (use `pg_stat_wal` to measure).
- Set `max_replication_slots` and `wal_keep_size` to prevent slot-based lag accumulation.
- Schedule heavy maintenance (VACUUM FULL, REINDEX) during low-traffic windows with explicit replication lag monitoring.
- Test failover quarterly to validate actual RPO under realistic lag conditions.
- Use `synchronous_commit = remote_write` for critical data paths if RPO requirements are strict.

## Related Runbooks
- runbook_db_connection_pool.md (if replica connections are exhausted due to lag-related read traffic)
- runbook_kafka_consumer_lag.md (if Kafka MirrorMaker is the replication mechanism)
