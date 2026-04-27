# Runbook: Kafka Consumer Lag Cascade

## Overview
Consumer lag accumulates when the rate of message production exceeds the rate of consumption. Left unaddressed, lag compounds exponentially as backpressure forces producers to queue, which can cascade into memory exhaustion and service-wide outages.

## Symptoms
- `consumer_lag` metric rising sharply (> 2× baseline rate-of-change)
- `throughput` dropping below normal processing rate (e.g., < 500 events/sec)
- Downstream DB write latency elevated (> 150 ms P99)
- Consumer pod CPU flat while lag grows (indicates IO-bound stall, not CPU)
- Rebalance events in consumer logs (`Revoke`, `Assign` log lines)
- Alert: consumer group offset not advancing for > 5 minutes

## Root Cause Patterns
1. **Downstream saturation**: DB, cache, or API the consumer writes to has become slow, causing consumer threads to block on writes and fall behind.
2. **Consumer rebalance storm**: Frequent pod restarts trigger group rebalances, pausing consumption during leader election.
3. **Message size spike**: Unusually large messages cause GC pressure, reducing effective throughput.
4. **Partition hotspot**: One partition receives disproportionate traffic; under-provisioned consumer threads can't keep up.
5. **Deserialization failure loop**: Poison-pill messages cause retries that appear as lag growth with zero committed progress.

## Immediate Actions
1. Check current lag per partition:
   ```bash
   kafka-consumer-groups.sh --bootstrap-server <broker>:9092 \
     --describe --group <consumer-group>
   ```
2. Check downstream dependency latency (DB write P99, external API health).
3. If downstream is healthy, scale consumer replicas:
   ```bash
   kubectl scale deployment <consumer> --replicas=<current+2>
   ```
4. If rebalance storm: check pod restart count and OOMKill events; address root cause before scaling.
5. If poison-pill messages suspected: enable dead-letter queue (DLQ) routing and skip the offending offsets after inspecting:
   ```bash
   kafka-console-consumer.sh --topic <topic> --offset <offset> \
     --partition <p> --max-messages 1
   ```
6. Increase `max.poll.interval.ms` and `session.timeout.ms` if consumers are healthy but slow:
   ```
   max.poll.interval.ms=600000
   session.timeout.ms=45000
   ```

## Escalation Thresholds
- Lag > 100k and still growing → P1, page on-call lead
- Lag > 500k → consider pausing non-critical producers to allow draining
- Lag growth rate > 10k/min and downstream healthy → immediate replica scale

## Prevention
- Set consumer lag alerting at 2× expected baseline with 5-minute evaluation window.
- Implement auto-scaling on `consumer_lag` metric via KEDA or HPA custom metric.
- Use circuit breakers on downstream DB calls to fail fast instead of blocking consumer threads.
- Test rebalance behaviour during deployments with canary rollouts (max-surge=1, max-unavailable=0).
- Establish and periodically test DLQ processing pipeline.

## Related Runbooks
- runbook_db_connection_pool.md (if downstream DB is the bottleneck)
- runbook_pod_oomkill.md (if consumers are restarting due to memory pressure)
