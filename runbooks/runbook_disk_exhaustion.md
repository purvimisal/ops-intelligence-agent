# Runbook: Disk Space Exhaustion

## Overview
Disk space exhaustion causes service failures when processes can no longer write to disk — log files stop rotating, databases cannot write WAL, and application state becomes corrupted. This failure mode has a hard deadline: once disk is 100% full, most writes fail immediately.

## Symptoms
- `disk_pct` metric trending steadily upward toward 100%
- Write errors in application logs: `No space left on device`
- Database WAL errors or Postgres `FATAL: could not write to file`
- Log rotation failures, log files growing unbounded
- Container pod restarts with `Error` state (application can't write state files)
- Monitoring tools reporting disk usage > 85% (warning) or > 95% (critical)

## Root Cause Patterns
1. **Log accumulation**: Application or system logs not rotating or rotating too slowly; debug logging left enabled in production.
2. **Core dumps**: Crashed processes leaving large core dump files.
3. **Database WAL bloat**: Unreplicated WAL accumulating due to stale replication slot or failed replica.
4. **Temp file accumulation**: Failed batch jobs leaving large temporary files behind.
5. **Container image layer growth**: Accumulated overlayfs layers from many deployments without periodic cleanup.
6. **Backup files not purged**: Old backups retained indefinitely due to misconfigured retention policy.

## Immediate Actions
1. Identify disk usage by directory:
   ```bash
   df -h /
   du -sh /* 2>/dev/null | sort -rh | head -20
   du -sh /var/log/* 2>/dev/null | sort -rh | head -10
   ```
2. Check for core dumps:
   ```bash
   find /var/crash /tmp /var/lib -name "core*" -size +100M 2>/dev/null
   ```
3. Clear rotated/old logs immediately:
   ```bash
   journalctl --vacuum-size=500M
   find /var/log -name "*.gz" -mtime +7 -delete
   ```
4. Check Postgres WAL for stale replication slots:
   ```sql
   SELECT slot_name, active, pg_size_pretty(pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn)) AS lag
   FROM pg_replication_slots;
   ```
   Drop stale slots if replica is confirmed gone:
   ```sql
   SELECT pg_drop_replication_slot('<slot_name>');
   ```
5. Truncate large temp files if safe:
   ```bash
   find /tmp -size +1G -mtime +1 -delete
   ```
6. If < 5% free and application is still running: take an emergency snapshot/backup, then extend the volume or add additional storage.

## Escalation Thresholds
- disk_pct > 85% → alert, schedule cleanup within 4 hours
- disk_pct > 95% → immediate action, service may fail imminently
- disk_pct reaches 100% → P1, service likely already impaired

## Prevention
- Alert at 75% and 90% usage with separate PagerDuty priorities.
- Enforce log rotation with `logrotate` or structured logging to stdout (let container runtime handle rotation).
- Set Postgres `log_rotation_age` and `log_rotation_size` appropriately.
- Implement automated cleanup jobs for temp files and old backups with enforced retention policies.
- Use persistent volumes with auto-expand policies where supported by your cloud provider.
- Monitor `pg_replication_slots` for inactive slots weekly.

## Related Runbooks
- runbook_db_connection_pool.md (if Postgres is failing due to WAL disk exhaustion)
- runbook_pod_oomkill.md (if pods are being killed and leaving core dumps)
