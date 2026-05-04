# Architecture — Technical Deep Dive

Reference for interviews. Covers every major component with the design decision made, the alternative considered, and why this choice was made.

---

## SignalIngester interface (`app/ingestion/base.py`)

**What it does:** Abstract base class defining `read_signals() -> AsyncIterator[Signal]`. All signal sources implement this one interface. `Signal` is a dataclass that maps 1:1 to the `signals` Postgres table — no translation layer needed between ingestion and persistence.

**Decision:** Single abstract interface over concrete implementations.
**Alternative:** Direct Kafka/Postgres queries in the detection layer.
**Why:** Swapping from synthetic to real signals is a one-class change. `DetectionPipeline` never imports `SyntheticIngester` directly — it imports `SignalIngester`. This also makes the detection layer testable without a running Kafka cluster or DB.

---

## Sliding window detector (`app/detection/window.py`)

**What it does:** Maintains a `deque` per service of the last `window_size_min` minutes of metric readings. `check(service)` fires `AnomalyEvent` objects when the *current* (latest) reading crosses a threshold. Uses `(service, ts.isoformat())` deduplication — the same signal fed twice produces identical output.

**Decision:** Threshold on latest value, window average reported as context.
**Alternative:** Threshold on window average (smoother but slower to react).
**Why:** A single CRITICAL reading is a real event worth alerting on. Window average is included in `AnomalyEvent` for context but doesn't gate the alert. Averaging would delay detection by half the window length.

**Note on urgency drop:** The trend analyser fires urgency = NONE once a metric crosses its warn threshold (positive slope, already above warn). This is intentional — the anomaly detector owns the above-threshold regime. The two detectors cover non-overlapping regions of the metric's lifecycle.

---

## Trend analyser (`app/detection/trend.py`)

**What it does:** Fits NumPy linear regression over the last `trend_window_steps` readings per metric. Projects steps-to-threshold using `(threshold - current) / slope`, converts to minutes. Returns urgency HIGH/MEDIUM/LOW based on time-to-threshold buckets (<30 min / 30–60 min / >60 min).

**Decision:** Linear regression over exponential smoothing or moving average.
**Alternative:** EWMA (exponential weighted moving average) — simpler, more reactive.
**Why:** We need both slope (direction) and a threshold crossing projection. Linear regression gives both directly. R² is also computed and included in `top_signals` so the LLM agent gets a confidence signal alongside the projection.

**Edge case:** If slope is negative and metric is below threshold, urgency = NONE. This correctly ignores a metric that's *improving*. Metrics that are currently above threshold with positive slope also return NONE — they've already been caught by the window detector.

---

## Pattern matcher (`app/detection/patterns.py`)

**What it does:** Loads 10 failure fingerprints from `scenarios/*.json` at init. Each fingerprint is a mean-centred, L2-normalised 180-dimensional vector (6 features × 30 steps). Incoming signals fill a rolling 30-step deque per service. After the deque is full, cosine similarity is computed against all fingerprints.

**Decision:** Mean-centering without dividing by standard deviation.
**Alternative:** Z-score normalisation (divide by std).
**Why:** Z-score would suppress the most informative metrics. During a Kafka lag cascade, `consumer_lag` changes by ~47,000 units while `memory_pct` changes by ~3 units. Z-scoring would give both equal weight. Mean-centering preserves the large-change signal so the pattern vector is dominated by the metric that's actually failing.

**Decision:** 30-step window, not variable-length.
**Alternative:** Dynamic Time Warping over variable-length windows.
**Why:** Fixed-length windows make dot products trivial and keep inference O(n_fingerprints × feature_dim). DTW would be more robust to timing differences but ~10× slower and complex to maintain. The synthetic scenarios are generated with consistent timing so fixed-length is sufficient.

**Cosine similarity = 1.0 for matching scenario:** The fingerprints are built from the same data used in evals. When a scenario is replayed step-by-step, the rolling window after 30 steps IS the fingerprint data — similarity approaches 1.0. In production, similarity would be 0.75–0.90 due to noise.

---

## Risk scorer formula (`app/detection/scorer.py`)

```
composite = 0.30 × anomaly_score + 0.35 × trend_score + 0.35 × pattern_score
```

**Decision:** Weighted sum with these specific weights.
**Alternative:** Max of three scores, or a learned classifier.
**Why:** Trend and pattern get equal weight (0.35 each) because they are both *predictive* — they fire before the metric crosses critical. Anomaly gets slightly less weight (0.30) because it's *reactive*. A learned classifier would require labelled production data we don't have. Weighted sum is interpretable: you can tell an on-call engineer "your risk score is 65 because pattern match is 94% similar to kafka_lag_cascade."

**Ceiling effect:** The maximum composite for most scenarios is ~65 (anomaly=100 + pattern=100 × their weights, trend=0 because metric already crossed warn). The orchestrator threshold is 70. This means the agent activates reliably when both the anomaly AND pattern components fire together, not on weak signals from one source alone.

---

## Agent orchestrator (`app/agent/orchestrator.py`)

**What it does:** Runs a Claude tool-use loop. Builds a `user` message from the `RiskScore`, calls `messages.create()` with the 5 tool schemas, dispatches tool calls to async functions, feeds results back as `tool_result` messages. Hard cap at 6 rounds with a "you MUST call create_incident_report now" nudge on round 6.

**Decision:** Tool-use loop with hard round cap.
**Alternative:** ReAct-style agent with text parsing, or a single structured-output call.
**Why:** Tool-use with Anthropic's native format is more reliable than text parsing for structured extraction. A single-call approach can't do multi-step investigation (search logs → get metrics → query runbook → create report). The 6-round cap prevents runaway loops — production incidents have been caused by LLM agents looping 40+ times.

**`create_incident_report` as termination signal:** The agent cannot complete an investigation without calling this tool. The tool schema is in the system prompt with "MUST". This pattern (force a structured terminal tool) is more reliable than checking for `end_turn` stop reason because the LLM sometimes produces a text summary instead of a tool call.

---

## Circuit breaker (`app/agent/circuit_breaker.py`)

**What it does:** Three-state FSM (CLOSED → OPEN → HALF_OPEN). HALF_OPEN is derived in the `state` property from `time.monotonic()` — it's not stored. This means no background timer is needed: the state machine self-heals purely through property access.

**Decision:** Computed state via property, not stored + background timer.
**Alternative:** `asyncio.Task` that transitions state after timeout.
**Why:** A background task can be cancelled, can lose its reference, or can fail silently. Property-based computation is deterministic and testable — you can force HALF_OPEN in tests by setting `_opened_at = time.monotonic() - 61.0`.

**Failure threshold = 5:** Five consecutive failures open the breaker. A single failure (e.g., API rate limit on a single request) does not open it. Recovery timeout = 60 s. This is conservative — better to block a few extra LLM calls than to hammer a degraded API.

---

## RAG chunking strategy (`app/agent/runbook_ingester.py`)

**What it does:** Splits runbooks by `##` markdown headers first (preserves section context). If a section exceeds 2048 characters, it falls back to paragraph splits with 256-character overlap. SHA-256 of chunk content is the `content_hash` — duplicate chunks across re-ingestion are silently ignored.

**Decision:** Header-first, paragraph-fallback chunking.
**Alternative:** Fixed-size character windows (e.g., every 512 chars).
**Why:** Fixed windows split sentences mid-thought and lose section identity. Header-based chunking means each retrieved chunk carries its section title as context (e.g., "## Remediation Steps"), which the agent needs to distinguish diagnosis context from action steps. The 256-char overlap on long sections prevents procedure steps from being split between chunks.

**BGE asymmetric retrieval:** `embed_query()` prepends `"Represent this sentence for searching relevant passages: "` before encoding. This prefix shifts the embedding toward retrieval-style matching rather than semantic similarity. Without it, a query "consumer lag growing" might match a runbook section titled "What is consumer lag?" instead of "How to fix consumer lag."

---

## SSE streaming (`app/api/streaming.py`)

**What it does:** The `/api/v1/incidents/analyse` endpoint returns `text/event-stream`. The generator yields `data: {...}\n\n` events with types: `status` (progress), `token` (incremental summary text), `done` (final result), `error`. The summary is streamed word-by-word in 3-word chunks to give the feel of live generation even though the full RCA is computed before streaming starts.

**Decision:** Chunked streaming of a pre-computed result.
**Alternative:** True token-by-token streaming via Anthropic's streaming API.
**Why:** True streaming would require restructuring the tool-use loop (tool results can't be streamed) and adds significant complexity. For a demo, streaming the summary in 3-word chunks is visually equivalent and much simpler. The `status` events before the `done` event provide real-time progress.

---

## APScheduler jobs (`app/alerts/scheduler.py`)

**`detection_loop` (every 30 s):** Reads the last 30 minutes of signals from `signals` table, re-runs all three detectors from scratch, writes a `risk_scores` row, fires Slack if score > 70. Re-running from DB (not from in-memory state) means the scheduler is stateless — it can restart without losing detection state.

**`dlq_retry` (every 60 s):** Pops one item from `dlq:incidents` Redis list, retries `agent.investigate()`, max 3 attempts. Using Redis RPUSH/LPOP for the DLQ gives FIFO ordering and persistence across restarts. The DLQ item includes `retry_count` and `last_error` — if all 3 retries fail, the item is logged with full context and discarded (no infinite retry loop).

---

## DB schema design

Four tables:
- **`signals`** — append-only event log. Never updated. `(service, ts)` indexed for fast window queries. `scenario` column distinguishes synthetic from baseline signals.
- **`incidents`** — one row per RCA. `hypotheses` and `fix_steps` stored as JSONB arrays. `langfuse_url` links directly to the trace for that investigation.
- **`chunks`** — runbook embeddings. `content_hash TEXT UNIQUE` enforces idempotency at the DB level. `embedding vector(768)` — the dimension is hard-coded and validated in `init_db()` with a conditional DROP if the wrong dimension is detected.
- **`risk_scores`** — append-only time series. `DISTINCT ON (service) ORDER BY ts DESC` gives the latest score per service. The scheduler writes here; the `/api/v1/risk` endpoint reads here.

**`init_db()` is idempotent:** All `CREATE TABLE IF NOT EXISTS`, `CREATE INDEX IF NOT EXISTS`, and `ALTER TABLE ADD COLUMN IF NOT EXISTS`. Running it multiple times or on an existing DB is safe.
