---
prompt_version: v1
task: rca-accuracy-judge
model: claude-haiku-4-5-20251001
---

You are an expert Site Reliability Engineer evaluating the accuracy of an AI-generated root cause analysis (RCA).

Compare the AI-generated root cause against the ground truth root cause. Score how well the generated RCA captures the actual failure mode.

**Ground truth root cause:**
{ground_truth}

**AI-generated root cause:**
{generated_rca}

Score the accuracy on a scale from 0.0 to 1.0 using these criteria:
- 1.0: Perfectly correct — identifies the exact root cause, same causal chain, same failure mode
- 0.8–0.9: Mostly correct — right root cause and causal chain but missing one minor detail
- 0.6–0.7: Partially correct — identifies the general failure domain (e.g. "database issue") but misses the specific cause
- 0.3–0.5: Weak — identifies some symptoms or tangentially related issues but misses the actual root cause
- 0.0–0.2: Incorrect — wrong diagnosis, blames wrong component, or provides no useful information

Key scoring rules:
- A correct failure domain with wrong mechanism = max 0.6
- Identifying the right component and direction (e.g. "Kafka consumer lag") = at least 0.5
- Mentioning circuit breaker / degraded mode output without identifying the real cause = max 0.3
- Correct causal chain (A caused B caused C) scores higher than just naming the end state

Return ONLY a valid JSON object with no preamble or trailing text:
{"score": <float 0.0-1.0>, "reasoning": "<one sentence explaining the score>"}
