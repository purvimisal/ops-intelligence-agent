# Runbook: Deployment Regression

## Overview
A deployment regression occurs when a newly deployed version of a service introduces a defect that degrades performance or correctness compared to the previous version. Quick detection and rollback are the primary objectives — root cause analysis comes after service is restored.

## Symptoms
- Error rate spike beginning within minutes of a deployment completing
- Latency increase (P99 > 2× baseline) correlated with deployment timestamp
- CPU or memory anomaly on new pods but not on old pods (if using canary/blue-green)
- Application crash loops or restart spikes (`pod_restarts` metric elevated)
- Specific new error types in logs that didn't exist in previous version
- Smoke tests or synthetic monitors failing post-deployment

## Root Cause Patterns
1. **Code bug**: Logic error, null pointer, unhandled exception path introduced in the change.
2. **Dependency version conflict**: Updated library version introduces breaking API change or incompatibility.
3. **Configuration drift**: New code requires config that wasn't deployed, causing startup failures or incorrect behavior.
4. **Database schema mismatch**: Migration not run, or migration ran but code expects old schema.
5. **Resource configuration regression**: Container CPU/memory limits reduced, causing OOMKills or CPU throttling.
6. **Feature flag not set**: New code paths gated behind a flag that's disabled in production.

## Immediate Actions
1. Confirm deployment timing aligns with error spike:
   ```bash
   kubectl rollout history deployment/<service>
   # Compare timestamp to alert firing time
   ```
2. **If error rate > 5% and rising: rollback immediately.** Root cause investigation can happen on the rolled-back version.
   ```bash
   kubectl rollout undo deployment/<service>
   # Wait for rollout to complete:
   kubectl rollout status deployment/<service>
   ```
3. Verify rollback resolved the issue (check error rate dashboard, give 5 minutes).
4. If rollback doesn't help, check if a downstream dependency was also changed simultaneously.
5. Check for schema migration issues:
   ```sql
   SELECT * FROM schema_migrations ORDER BY applied_at DESC LIMIT 5;
   ```
6. Preserve logs and metrics from the failed version for post-incident analysis before clearing.

## When NOT to Rollback Immediately
- The deployment included a database migration that cannot be reversed (data has been transformed). In this case, forward-fix instead of rollback — communicate with the team before acting.
- Multiple services were deployed simultaneously — identify which one is the cause before rolling any back.

## Escalation Thresholds
- Error rate > 10% post-deploy → immediate rollback, P1 incident
- Rollback completed but errors persist > 10 minutes → escalate to platform/infra team (may be infrastructure issue, not code)

## Prevention
- Enforce canary deployments (5% → 25% → 100%) with automated rollback on error rate increase.
- Block deployments without passing smoke tests against a staging environment.
- Use feature flags to decouple code deployments from feature activations.
- Run integration tests against a production-like DB with pending migrations applied.
- Set deployment freeze windows around high-traffic periods.

## Related Runbooks
- runbook_pod_oomkill.md (if new pods are OOMKilling due to reduced memory limits)
- runbook_cascade_5xx.md (if deployment regression is propagating to upstream services)
