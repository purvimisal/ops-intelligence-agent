# Runbook: Cascade 5xx Errors

## Overview
A cascade of HTTP 5xx errors indicates one or more backend services are returning server-side failures. A cascade occurs when a failing dependency causes upstream callers to also fail, spreading the error rate across multiple services.

## Symptoms
- `error_rate` rising rapidly (> 0.05 within minutes, trending toward 0.15+)
- Correlated 5xx responses across multiple service endpoints
- Latency spike or timeout increase alongside errors (slow failure, not fast failure)
- CPU or memory normal on affected pods — failure is logical, not resource-based
- Dependency health check failures in service mesh / load balancer logs
- Circuit breaker open events in application logs

## Root Cause Patterns
1. **Dependency failure propagation**: A downstream service (DB, cache, third-party API) begins failing, and callers don't have circuit breakers, causing the failure to propagate upstream.
2. **Configuration change**: Recent deployment introduced bad config (wrong endpoint URL, auth token, timeout value).
3. **Rate limiting**: Downstream service is rate-limiting the caller due to traffic spike or credential issue.
4. **Thread pool exhaustion**: Blocking calls to a slow dependency exhaust the thread pool, causing all requests to queue and time out.
5. **TLS/certificate expiry**: Certificate expired on a dependency causing all TLS handshakes to fail.

## Immediate Actions
1. Identify the failure origin — find the deepest service in the call chain that is failing:
   ```bash
   # Check service dependency map in your APM tool
   # Look for the service with highest error rate that has no upstream errors
   ```
2. Check recent deployments across all affected services:
   ```bash
   kubectl rollout history deployment/<service>
   ```
3. If a bad deployment is identified, immediately rollback:
   ```bash
   kubectl rollout undo deployment/<service>
   ```
4. Check downstream dependency availability (DB, Redis, external API) — verify health endpoints and response times.
5. Enable or lower circuit breaker thresholds to limit blast radius while root cause is investigated.
6. If rate limiting: check API key validity, quota usage, and rotate/upgrade keys if needed.
7. Check certificate expiry:
   ```bash
   openssl s_client -connect <host>:443 2>/dev/null | openssl x509 -noout -dates
   ```

## Escalation Thresholds
- error_rate > 0.15 sustained > 5 minutes → P1, incident declared
- Multiple independent services failing simultaneously → possible infrastructure issue, escalate to platform team
- Error rate spike coincides with deployment → rollback immediately without waiting for investigation

## Prevention
- Implement circuit breakers (half-open pattern) on all synchronous inter-service calls.
- Enforce timeouts on all outbound HTTP/gRPC calls (connect_timeout + read_timeout).
- Use canary deployments with automatic rollback on error rate increase.
- Monitor certificate expiry with > 30-day advance alerting.
- Chaos engineering: periodically inject dependency failures to test resilience.

## Related Runbooks
- runbook_deployment_regression.md (if errors correlate with recent deploy)
- runbook_db_connection_pool.md (if DB is the failing dependency)
