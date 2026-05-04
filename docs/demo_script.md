# Demo Script — Loom Recording

Target: 3 minutes. Interviewers watch the first 90 seconds — front-load the most interesting parts.

## Setup (do before recording)

```bash
make dev          # stack running
make ingest       # runbooks embedded (only needed once)
make serve        # API on :8000 in a separate terminal
```

Have open in separate terminal tabs:
1. `make serve` output
2. CLI commands ready to paste
3. Grafana at http://localhost:3000
4. Langfuse at https://cloud.langfuse.com (if tracing)

## Script

**00:00 – 00:20 — What the system does (speak to the problem)**

> "This is an ops intelligence agent that detects infrastructure anomalies before they become incidents. On the left is the architecture — four layers: signal ingestion, deterministic detection, LLM-driven RCA, and observability. The detection layer runs independently of the LLM — if Claude is unavailable, risk scores and Slack alerts still fire."

_(Show the architecture diagram in README or docs/architecture.md)_

---

**00:20 – 00:40 — Live risk scores**

```bash
python -m app.cli status
```

> "This shows the current risk score for every monitored service. Green is OK, yellow is medium risk, red needs investigation. The score is a composite of three detectors — sliding window anomaly detection, trend extrapolation, and cosine-similarity pattern matching against 10 known failure fingerprints."

---

**00:40 – 01:30 — Inject a failure scenario and watch it detect**

```bash
make detect SCENARIO=kafka_lag_cascade
```

> "I'm injecting the Kafka consumer lag cascade scenario — based on a real incident pattern from Oracle OCI's billing pipeline. Watch the risk score column. The trend detector fires first — it sees consumer lag increasing and projects when it'll breach the threshold. By step 14 the anomaly detector fires a CRITICAL event. By step 29 the pattern matcher recognises the shape — 96% similarity to the Kafka lag fingerprint."

_(While it runs, narrate what's happening in the step table)_

> "Score hits 65. The agent threshold is 70 — in the demo it activates. Watch the agent loop..."

---

**01:30 – 02:00 — Read the RCA output**

> "The agent called three tools: get_pipeline_metrics to confirm the lag trajectory, query_runbook_kb which retrieved the Kafka consumer runbook, then create_incident_report. Here's the structured output..."

_(Read the root cause and fix steps aloud. Highlight that it matches the ground truth in the scenario fixture.)_

> "Ground truth: DB write latency spike caused consumer throughput to drop, lag compounded in a positive feedback loop. Agent got it right."

---

**02:00 – 02:20 — Show the stored incident**

```bash
curl http://localhost:8000/api/v1/incidents | python3 -m json.tool | head -30
```

> "The RCA is persisted. The API exposes it. In production, the Slack webhook fires at risk > 70, and at > 85 it surfaces the relevant runbooks proactively."

---

**02:20 – 02:40 — Grafana dashboard**

_(Switch to browser, http://localhost:3000)_

> "Four panels: risk scores over time per service with red/yellow/green thresholds, detection latency p50 and p95, incident count by severity, and the eval scorecard — recall 100%, precision 100%, false positive rate 0%, 98% toil eliminated."

---

**02:40 – 03:00 — Eval suite**

```bash
make eval 2>&1 | tail -15
```

> "The eval suite is 25 tests. Detection tests are fully deterministic — no API calls. The LLM judge tests use Claude Haiku to score agent RCA quality against ground truth. This gates CI — a regression in detection precision or recall blocks the deploy."

---

## What to emphasise (technical interviewers)

- **Graceful degradation**: detection layer has zero LLM calls. Circuit breaker is a proper 3-state FSM, not just a try/except.
- **Idempotency**: same signal batch processed twice = same risk score. Content-hash deduplication in the runbook KB.
- **Pattern matching design**: mean-centring instead of Z-score so large-change metrics dominate the fingerprint vector, not noise.
- **Eval as first-class code**: LLM judge prompt is versioned in `evals/prompts/`, treated like source code, changes require passing eval run.

## What NOT to say

- Don't call it an "AI chatbot"
- Don't say "I used Claude to write the code" (they know; focus on what YOU designed)
- Don't apologise for synthetic data — frame it as deliberate: "Real incident data is confidential; this is how you build a reliable eval suite without it."
