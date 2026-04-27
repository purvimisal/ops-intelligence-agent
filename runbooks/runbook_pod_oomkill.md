# Runbook: Pod OOMKill Loop

## Overview
OOMKill (Out-of-Memory Kill) occurs when the Kubernetes OOM killer terminates a container because its memory usage exceeds the configured limit. A loop occurs when the pod restarts, immediately consumes the same amount of memory, and gets killed again — producing a `CrashLoopBackOff` pattern.

## Symptoms
- `memory_pct` metric approaching or exceeding 90% before each restart
- `pod_restarts` count incrementing continuously
- Kubernetes events: `OOMKilled` exit code (exit code 137)
- `kubectl describe pod` showing `Last State: OOMKilled`
- Application logs cut off mid-request (process killed without graceful shutdown)
- Memory usage growing monotonically between restarts (leak) or spiking immediately on startup (configuration issue)

## Root Cause Patterns
1. **Memory leak**: Application allocates memory that is not released; each request or background task gradually increases resident memory until the limit is hit.
2. **Undersized memory limit**: Container limit is set too low for the actual working set of the application under normal load.
3. **JVM/runtime heap misconfiguration**: JVM `-Xmx` or Node.js `--max-old-space-size` set higher than container limit, causing runtime to demand more memory than Kubernetes permits.
4. **Large dataset loaded into memory**: Batch job or cache warm-up loads an unexpectedly large dataset (e.g., loading full table instead of paginating).
5. **Dependency response unexpectedly large**: External API or DB returning much larger payloads than expected, causing request-scoped memory to spike.

## Immediate Actions
1. Confirm OOMKill cause:
   ```bash
   kubectl describe pod <pod-name> -n <namespace>
   # Look for: Last State: Terminated  Reason: OOMKilled
   kubectl get events --field-selector reason=OOMKilling -n <namespace>
   ```
2. Check current memory limits vs requests:
   ```bash
   kubectl get pod <pod-name> -o jsonpath='{.spec.containers[*].resources}'
   ```
3. **Immediate mitigation**: Increase memory limit temporarily to stop the loop:
   ```bash
   kubectl set resources deployment/<name> --limits=memory=<new-limit>
   ```
   This buys time for root cause investigation without production impact.
4. Capture a heap dump before restarting (if still running):
   ```bash
   kubectl exec <pod-name> -- jmap -dump:live,format=b,file=/tmp/heap.hprof <pid>
   kubectl cp <pod-name>:/tmp/heap.hprof ./heap.hprof
   ```
5. Check if memory grows monotonically (leak) vs spikes on startup (misconfiguration) by reviewing memory timeline in Grafana.
6. Review recent code changes for: large in-memory data structures, missing .close() on streams, caches without max-size configuration.

## Escalation Thresholds
- pod_restarts > 5 in 10 minutes → P1, service is degraded
- All replicas OOMKilling simultaneously → P0, service down, escalate immediately
- OOMKill during deployment → rollback; check if new version has increased memory footprint

## Prevention
- Set memory `requests` equal to or slightly less than `limits` (avoid burstable QoS for stateful services).
- Configure JVM/runtime max heap to 75% of container memory limit (leave headroom for non-heap).
- Implement memory usage alerting at 75% and 85% of limit.
- Use memory profiling in staging load tests with production-equivalent data volumes.
- Enforce cache max-size configurations; never allow unbounded caches.
- Add leak detection in development (e.g., Valgrind for C, jhat for JVM, node --inspect for Node).

## Related Runbooks
- runbook_deployment_regression.md (if OOMKill started after a new deployment)
- runbook_disk_exhaustion.md (if OOMKill is generating core dumps and filling disk)
