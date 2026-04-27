# Runbook: Data Pipeline Extract Failure

## Overview
Extract process failures in CDC (Change Data Capture) or ETL pipelines halt the flow of data from source systems into downstream consumers. Unlike application failures, pipeline extract failures often go undetected for minutes to hours because the system appears healthy while data simply stops moving.

## Symptoms
- `consumer_lag` metric growing on downstream pipeline topics
- `throughput` dropping to near-zero on pipeline ingestion paths
- Extract process logs showing connection failures, redo log errors, or supplemental log gaps
- Downstream tables showing stale `MAX(updated_at)` values compared to source
- Monitoring: replication latency > alert threshold (e.g., > 10 minutes behind source)
- Extract process health check returning unhealthy or process not running

## Root Cause Patterns
1. **Source DB connectivity loss**: Network partition, credential rotation, or firewall change blocking the extract process from connecting to source.
2. **Redo/transaction log gap**: Log shipping gap due to source DB switchover, log archival issue, or supplemental logging disabled.
3. **Schema change without coordination**: DDL change on source table (column added/dropped/renamed) that the extract process wasn't prepared for.
4. **Extract process OOM or crash**: Memory exhaustion in the extract process, often triggered by large transactions or LOB columns.
5. **Checkpoint file corruption**: Extract process checkpoint (tracks current log position) became corrupted; process cannot resume from last known position.
6. **Disk full on extract host**: Trail files (intermediate CDC data) accumulating faster than downstream processes can consume them.

## Immediate Actions
1. Check if extract process is running:
   ```bash
   systemctl status <extract-service>
   # or for containerized:
   kubectl get pods -l app=<extract-pod>
   kubectl logs <extract-pod> --tail=100
   ```
2. Test connectivity from extract host to source database:
   ```bash
   nc -zv <source-db-host> <port>
   # Verify credentials are current
   ```
3. Check for recent schema changes on source tables:
   ```sql
   SELECT object_name, ddl_time, ddl_text
   FROM dba_ddl_log  -- or your CDC metadata table
   WHERE ddl_time > SYSDATE - 2/24
   ORDER BY ddl_time DESC;
   ```
4. Check extract trail disk usage and clear old consumed trails if full.
5. If process crashed: review core dumps or OOM logs; restart with increased heap if OOM:
   ```bash
   export JAVA_HEAP_MAX=4g  # or equivalent for your runtime
   systemctl restart <extract-service>
   ```
6. If checkpoint corruption suspected: restore from last known-good checkpoint (data may need to be re-applied from that point):
   ```bash
   # Identify last valid checkpoint timestamp from monitoring
   # Restore checkpoint file from backup
   cp <checkpoint-backup> <checkpoint-path>
   systemctl restart <extract-service>
   ```
7. Monitor that extract begins consuming log position and lag starts decreasing.

## Escalation Thresholds
- Pipeline lag > 30 minutes → P2, notify data team
- Pipeline lag > 2 hours → P1, data consumers may be serving stale data
- Extract stopped with unknown checkpoint position → P1, data integrity risk

## Prevention
- Implement end-to-end pipeline lag monitoring with alerting (source commit time vs. downstream visibility time).
- Create coordination process: DDL changes on source must be pre-registered with pipeline team.
- Run extract process in high-availability pair with automatic failover.
- Set disk usage alert on extract trail storage at 70%.
- Periodic checkpoint backup (e.g., hourly snapshots of checkpoint files).
- Test credential rotation procedure quarterly in non-production.

## Related Runbooks
- runbook_kafka_consumer_lag.md (if pipeline uses Kafka as intermediate transport)
- runbook_disk_exhaustion.md (if trail file disk is full)
