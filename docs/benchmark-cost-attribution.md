# Benchmark Cost Attribution (Perseus patent support)

`plutus_agent/benchmark_cost.py` turns a Perseus benchmark run into a
deterministic, dollar-denominated comparison of two ways to assemble the same
context, for the resolve-before-context patent's §101 technical-effect evidence.

Supports issues #85 (per-call cost/round-trip metering) and #86 (reproducible
cost-attribution exhibit).

## What it measures

| Approach | Round-trips | Idea |
|---|---|---|
| `agentic` | N+1 | The model issues one round-trip per context-gathering operation, re-sending a growing prompt each time, then a final answer call. |
| `resolve_before_context` | 1 | Perseus resolves all directives deterministically *offline*; the model is called once. |

The comparison reports, per approach: round-trips, input/output tokens, and USD
cost; plus the reduction deltas (round-trips eliminated, % round-trips, $ saved,
% cost).

## Determinism (issue #86)

- Cost is computed solely from Plutus's **frozen price table**
  (`pricing.PRICE_TABLE`, dated `pricing.PRICE_TABLE_AS_OF`) and the run's token
  counts. No wall-clock, no network, no live pricing.
- `attribute(...)` is a pure function — its JSON is byte-identical for identical
  inputs.
- `emit_exhibit(...)` adds a single `generated_at` field; pin it
  (`generated_at="..."`) to make the whole envelope reproducible for golden
  tests. `write_exhibit(...)` derives the filename timestamp from that field, so
  envelope and filename always agree.

## Usage

```python
from plutus_agent.benchmark_cost import (
    AGENTIC, RESOLVE_BEFORE_CONTEXT, ApproachRun, CallRecord,
    attribute, write_exhibit,
)

agentic = ApproachRun(AGENTIC)
for i in range(5):  # one gathering round-trip per directive
    agentic.record(CallRecord("anthropic", "claude-opus-4-8",
                              input_tokens=1000 * (i + 1), output_tokens=120,
                              label=f"gather-{i}"))
agentic.record(CallRecord("anthropic", "claude-opus-4-8",
                          input_tokens=6000, output_tokens=400, label="final-answer"))

resolve = ApproachRun(RESOLVE_BEFORE_CONTEXT).record(
    CallRecord("anthropic", "claude-opus-4-8",
               input_tokens=6000, output_tokens=400, label="single-shot"))

attribution = attribute("six-directive-context", agentic, resolve)
json_path, csv_path = write_exhibit(attribution, "docs/exhibits")
```

The Perseus exhibit suite calls `attribute(...)` with the token counts its own
`--explain` manifest already measures (agentic round-trip count is a structural
property of the directive manifest; tokens are directly measured), then embeds
the resulting JSON/CSV.

## Reference exhibit

`docs/exhibits/SAMPLE-cost-attribution.{json,csv}` is a committed, reproducible
reference for the canonical six-directive task:

| Approach | Round-trips | Cost (USD) |
|---|---|---|
| agentic | 6 | 0.390000 |
| resolve_before_context | 1 | 0.120000 |
| **reduction** | **5 (83.33%)** | **$0.27 saved (69.23%)** |

Prices per the frozen table as of `2026-06-26` (`claude-opus-4-8`: $15/M input,
$75/M output). Regenerate with pinned timestamp:

```python
from plutus_agent.benchmark_cost import emit_exhibit, to_csv
env = emit_exhibit(attribution, generated_at="2026-06-28T00:00:00Z")
```

Tests: `tests/test_benchmark_cost.py`.
