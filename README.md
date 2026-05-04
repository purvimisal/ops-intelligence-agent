# Ops Intelligence Agent

AI-powered backend system that detects infrastructure anomalies before they become incidents, then drives a structured RCA using an LLM agent. Built on four years of on-call experience with Oracle OCI's distributed billing infrastructure — where ~70% of incident time is toil.

## Live demo

> Deploy in progress — link will appear here after `make deploy`.
>
> Loom walkthrough: _record after first successful deploy_

## Eval results — latest run

| Metric | Result | Target |
|---|---|---|
| Detection recall | **100%** (10/10 scenarios) | ≥ 90% |
| Precision | **100%** | ≥ 85% |
| False positive rate | **0%** | ≤ 10% |
| Detection pipeline latency | **86 ms** (all 10 scenarios) | ≤ 60 s |
| Pre-emptive runbook match | **100%** (10/10) | ≥ 60% |
| Toil eliminated | **98%** (79.5 min → 1.4 min) | ≥ 80% |
| Estimated cost per RCA | **$0.027** (4 rounds, 70% cache) | ≤ $0.05 |
| RCA accuracy (LLM-as-judge) | _run `make eval` with API key_ | ≥ 85% |

_Eval run: 2026-05-04 · Model: claude-sonnet-4-5 · Embeddings: BAAI/bge-base-en-v1.5_

## How it works

**Signal ingestion** generates realistic JSON log streams and pipeline metric snapshots (throughput, consumer lag, error rate, memory/CPU/disk) at configurable rates. Ten failure scenario fixtures — based on real Oracle OCI incident patterns — provide ground truth for evals. Each fixture contains a 30-step signal trajectory plus log events and a structured `ground_truth` block with root cause, correct runbook, and pre-emptive action.

**Detection layer** runs three independent detectors against a rolling signal window. The sliding window detector fires `AnomalyEvent` when any metric crosses a warn or critical threshold. The trend analyser fits linear regression over the last N readings and projects time-to-threshold, surfacing early warnings before the metric crosses warn. The pattern matcher computes cosine similarity between mean-centred feature vectors and pre-loaded failure fingerprints — it identifies *which* failure scenario is forming. A composite risk scorer combines the three as `0.35 × trend + 0.35 × pattern + 0.30 × anomaly`, updating every 30 seconds.

**Agent orchestrator** activates when risk score ≥ 70. It drives a Claude `claude-sonnet-4-5` tool-use loop (max 6 rounds) with five tools: `search_logs`, `get_pipeline_metrics`, `query_runbook_kb`, `get_service_health`, `create_incident_report`. The runbook tool does semantic RAG search over 10 runbooks chunked and embedded into pgvector. The agent is forced to commit to a structured report via `create_incident_report` before the loop terminates. A circuit breaker (5-failure threshold, 60 s recovery) ensures the LLM layer degrades independently — detection and Slack alerts continue running when the Claude API is unavailable.

**Observability** is layered: Prometheus scrapes `risk_score_gauge`, `detection_latency_seconds`, `incidents_total`, and `dlq_length` from `/metrics`; Grafana visualises them across four panels; Langfuse traces every Claude API call with token counts, cost, latency, and tool calls as child spans.

## Architecture

```
Synthetic signals ──► DetectionPipeline ──► RiskScorer ──► risk_scores (Postgres)
                            │                    │
                   SlidingWindow           risk > 70?
                   TrendAnalyser               │
                   PatternMatcher         OrchestratorAgent
                                               │
                                    ┌──────────┴──────────┐
                                    │   Claude tool-use   │
                                    │   search_logs        │
                                    │   get_metrics        │
                                    │   query_runbook_kb  │──► pgvector (chunks)
                                    │   get_service_health│
                                    │   create_incident   │──► incidents (Postgres)
                                    └─────────────────────┘
                                               │
                                        SlackAlerter ──► Slack webhook
                                        Langfuse     ──► LLM traces
                                        Prometheus   ──► Grafana dashboard
```

## Technical decisions

**1. Async Python throughout** — the detection loop, agent orchestrator, and API all run on the same event loop. No threading avoids GIL contention on I/O-heavy workloads and makes the system predictable under load. `asyncpg` and `redis.asyncio` are the database/cache clients.

**2. BAAI/bge-base-en-v1.5 over MiniLM** — BGE uses asymmetric retrieval (separate encoding paths for documents vs queries via a prefix). This matters for runbook search: a query like "consumer lag growing" should surface passages about Kafka consumer scaling even when the text doesn't share keywords. The model is baked into the Docker image to avoid cold-start download delays.

**3. Mean-centring over Z-score for pattern matching** — dividing by standard deviation would suppress the metrics with the most signal (consumer_lag changes by 47,000 during a cascade; memory changes by 2%). Mean-centring preserves relative magnitude so the pattern matcher is dominated by the metrics that actually changed, not noise.

**4. Deterministic detection + LLM reasoning, not LLM-only** — the detection layer has no API calls, no latency, no cost, and no hallucinations. The LLM adds narrative, runbook retrieval, and structured RCA. If Claude is unavailable, risk scores and Slack alerts still fire; the LLM output is marked `pending`.

**5. Circuit breaker as a first-class component** — five consecutive Claude API failures open the breaker for 60 seconds. After recovery timeout, one probe is allowed through (HALF_OPEN). This prevents a flapping API from triggering cascading failures and keeps the agent layer independently deployable.

## Running locally

```bash
git clone https://github.com/your-username/ops-intelligence-agent
cd ops-intelligence-agent

cp .env.example .env    # fill in ANTHROPIC_API_KEY at minimum
make dev                # start Postgres, Redis, Prometheus, Grafana
make ingest             # embed the 10 runbooks into pgvector (run once)

make detect SCENARIO=kafka_lag_cascade   # detection + agent RCA
make test                                # 205 unit tests
make eval                                # eval suite (set ANTHROPIC_API_KEY for LLM judge)
make serve                               # API on :8000
```

Grafana dashboard at http://localhost:3000 (admin / opsagent).

## What I'd do differently in v2

- **Section-aware chunking** — the current chunker splits on `##` headers then falls back to paragraph boundaries. A smarter chunker would keep procedure steps together (numbered lists under `## Remediation`) so the agent retrieves actionable steps rather than context paragraphs.
- **Hybrid BM25 + dense retrieval** — for runbook search, keyword hits on exact error codes (`KAFKA_LAG_HIGH`, `DB_WRITE_TIMEOUT`) complement semantic similarity. The current dense-only search can miss exact string matches.
- **Real Kafka integration** — replacing `SyntheticIngester` with a real `KafkaIngester` is a one-class swap (the `SignalIngester` interface is already designed for it). The pattern fingerprints and detection thresholds would need calibration on real traffic.
