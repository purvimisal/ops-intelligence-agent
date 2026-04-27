# Runbook: Pricing Engine Timeout

## Overview
Pricing engine timeouts occur when the service responsible for computing prices fails to respond within the required SLA. This commonly cascades to checkout failures, cart abandonment, and revenue loss. Pricing engines are often computationally intensive and sensitive to data freshness requirements.

## Symptoms
- `error_rate` elevated on pricing service endpoints
- Response time P99 > timeout threshold (e.g., > 2s for synchronous pricing calls)
- `throughput` dropping as requests queue behind slow computations
- Downstream services (checkout, cart) showing 503/504 errors from pricing dependency
- `cpu_pct` may be high (compute-bound) or normal (IO-bound waiting for data)
- Cache hit ratio dropping (if pricing relies on cached rate data)

## Root Cause Patterns
1. **Pricing rule complexity explosion**: A recent rule configuration change introduced computationally expensive rule combinations (e.g., combinatorial rule evaluation without pruning).
2. **Cache invalidation storm**: A bulk pricing update invalidated the cache simultaneously for many products, causing a thundering herd of DB lookups.
3. **Underlying data source slow**: The database or external rate feed that pricing depends on has become slow, causing each computation to wait.
4. **Algorithm complexity regression**: Code change introduced O(n²) complexity for a data set that grew beyond the tested scale.
5. **Concurrent request accumulation**: Pricing requests queuing behind one slow request, exhausting worker threads; appears as simultaneous timeout spike.
6. **External feed unavailability**: Third-party rate feed (FX rates, tax tables) unavailable, causing lookups to time out waiting for fresh data.

## Immediate Actions
1. Identify whether timeouts are compute-bound or IO-bound:
   - CPU high → algorithm complexity or rule explosion issue
   - CPU normal but latency high → waiting on DB, cache, or external feed
2. Check pricing cache hit ratio and current cache size:
   ```bash
   redis-cli info stats | grep -E 'keyspace_hits|keyspace_misses'
   redis-cli info memory | grep used_memory_human
   ```
3. If cache miss storm: temporarily serve stale-but-valid cached prices with extended TTL:
   ```bash
   # In Redis, bump TTL on pricing keys
   redis-cli --scan --pattern 'pricing:*' | xargs -L 100 redis-cli expire 3600
   ```
4. Check if a recent pricing rule configuration change occurred; if so, revert to previous configuration.
5. Implement request coalescing or shedding: return last-known price from cache for non-critical paths while investigation continues.
6. Check external data feed health and set fallback to last-good cached rates if unavailable.
7. If worker thread exhaustion: scale pricing service replicas and increase thread pool size temporarily.

## Escalation Thresholds
- Pricing timeout rate > 5% → P2, checkout flow degraded
- Pricing timeout rate > 20% → P1, revenue impact, page on-call lead
- Pricing service completely unavailable → P0, emergency failover to static/cached pricing

## Prevention
- Implement a pricing cache with stale-while-revalidate strategy: always serve cached price immediately, refresh asynchronously.
- Set absolute request timeout at the client (e.g., 500ms) with fallback to cached price.
- Load-test pricing engine with production rule complexity and data volume quarterly.
- Implement rate limiting on cache invalidation: batch pricing updates, never invalidate all at once.
- Monitor cache hit ratio; alert if it drops below 80%.
- Decouple from external feeds: snapshot external rates on schedule, never call them synchronously in the pricing hot path.

## Related Runbooks
- runbook_db_connection_pool.md (if pricing DB connections are exhausted)
- runbook_cascade_5xx.md (if pricing timeouts are cascading to checkout/cart services)
