"""Benchmark cost attribution for the Perseus resolve-before-context patent.

Issues #85 (per-call cost/round-trip metering) and #86 (reproducible
cost-attribution exhibit).

This module turns a benchmark run — two approaches to assembling the same
context — into a deterministic, dollar-denominated comparison the Perseus
exhibit suite can embed:

  * **agentic baseline**: the model issues one round-trip per context-gathering
    operation (N tool calls => N+ model round-trips).
  * **resolve-before-context**: Perseus resolves all directives deterministically
    *before* the single model call (1 model round-trip regardless of N).

The §101 technical-effect argument is strongest in dollars + round-trips, not
just tokens. This module computes both, using Plutus's frozen ``PRICE_TABLE``
(``pricing.PRICE_TABLE_AS_OF``) so the same inputs always yield the same cost —
a reproducible reduction-to-practice artifact.

Determinism contract (issue #86):
  - Cost is computed solely from the frozen price table + the run's token counts;
    no wall-clock, no network, no live pricing.
  - The emitted exhibit is byte-identical for identical inputs EXCEPT for an
    explicit ``generated_at`` timestamp, which is confined to a single field and
    can be pinned via ``generated_at=`` for golden-file tests.
"""
from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Optional

from .pricing import PRICE_TABLE_AS_OF, estimate_cost

# Approach identifiers used throughout the exhibit.
AGENTIC = "agentic"
RESOLVE_BEFORE_CONTEXT = "resolve_before_context"


@dataclass(frozen=True)
class CallRecord:
    """One metered model call within a benchmark run.

    A single context-assembly task comprises one or more model round-trips. The
    agentic baseline records one CallRecord per tool-gathering round-trip plus a
    final answer call; resolve-before-context records exactly one.
    """
    provider: str
    model: Optional[str]
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int = 0
    reasoning_tokens: int = 0
    # Free-form label, e.g. "discover-tools", "fetch-file", "final-answer".
    label: str = ""

    def cost_usd(self, overrides: Optional[dict] = None) -> float:
        return estimate_cost(
            self.provider, self.model,
            self.input_tokens, self.output_tokens,
            self.cache_read_tokens, self.reasoning_tokens,
            overrides=overrides,
        )


@dataclass
class ApproachRun:
    """All metered calls for one approach on one task."""
    approach: str
    calls: list[CallRecord] = field(default_factory=list)

    def record(self, call: CallRecord) -> "ApproachRun":
        self.calls.append(call)
        return self

    @property
    def round_trips(self) -> int:
        """Number of model round-trips = number of metered model calls."""
        return len(self.calls)

    def total_input_tokens(self) -> int:
        return sum(c.input_tokens for c in self.calls)

    def total_output_tokens(self) -> int:
        return sum(c.output_tokens for c in self.calls)

    def total_cost_usd(self, overrides: Optional[dict] = None) -> float:
        return round(sum(c.cost_usd(overrides) for c in self.calls), 6)


def _pct_reduction(baseline: float, improved: float) -> float:
    """Percent reduction from baseline to improved (0 when baseline is 0)."""
    if baseline <= 0:
        return 0.0
    return round((baseline - improved) / baseline * 100.0, 2)


def attribute(
    task: str,
    agentic: ApproachRun,
    resolve: ApproachRun,
    *,
    overrides: Optional[dict] = None,
) -> dict:
    """Build the deterministic cost-attribution comparison for one task.

    Returns a plain dict (JSON-serializable) with per-approach totals and the
    reduction deltas. Pure function of its inputs + the frozen price table —
    no timestamp, no I/O — so it is byte-reproducible.
    """
    a_cost = agentic.total_cost_usd(overrides)
    r_cost = resolve.total_cost_usd(overrides)
    a_rt = agentic.round_trips
    r_rt = resolve.round_trips
    return {
        "task": task,
        "price_table_as_of": PRICE_TABLE_AS_OF,
        "approaches": {
            AGENTIC: {
                "round_trips": a_rt,
                "input_tokens": agentic.total_input_tokens(),
                "output_tokens": agentic.total_output_tokens(),
                "cost_usd": a_cost,
                "calls": [asdict(c) for c in agentic.calls],
            },
            RESOLVE_BEFORE_CONTEXT: {
                "round_trips": r_rt,
                "input_tokens": resolve.total_input_tokens(),
                "output_tokens": resolve.total_output_tokens(),
                "cost_usd": r_cost,
                "calls": [asdict(c) for c in resolve.calls],
            },
        },
        "reduction": {
            "round_trips_eliminated": a_rt - r_rt,
            "round_trips_pct": _pct_reduction(a_rt, r_rt),
            "cost_usd_saved": round(a_cost - r_cost, 6),
            "cost_pct": _pct_reduction(a_cost, r_cost),
        },
    }


def to_csv(attribution: dict) -> str:
    """Render a cost-attribution dict as deterministic CSV (one row/approach)."""
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow([
        "task", "approach", "round_trips",
        "input_tokens", "output_tokens", "cost_usd", "price_table_as_of",
    ])
    task = attribution["task"]
    as_of = attribution["price_table_as_of"]
    for name in (AGENTIC, RESOLVE_BEFORE_CONTEXT):
        a = attribution["approaches"][name]
        w.writerow([
            task, name, a["round_trips"],
            a["input_tokens"], a["output_tokens"],
            f"{a['cost_usd']:.6f}", as_of,
        ])
    return buf.getvalue()


def emit_exhibit(
    attribution: dict,
    *,
    generated_at: Optional[str] = None,
) -> dict:
    """Wrap an attribution dict as a self-describing exhibit envelope.

    ``generated_at`` is the ONLY non-deterministic field; pass an explicit value
    to make the whole envelope reproducible (golden-file tests pin it). When
    omitted it defaults to the current UTC time in ISO-8601.
    """
    ts = generated_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "exhibit": "cost-attribution",
        "supports": "Perseus resolve-before-context patent (§101 technical effect)",
        "generated_at": ts,
        "price_table_as_of": attribution["price_table_as_of"],
        "deterministic": True,
        "attribution": attribution,
    }


def write_exhibit(
    attribution: dict,
    out_dir: str,
    *,
    generated_at: Optional[str] = None,
) -> tuple[str, str]:
    """Write a timestamped JSON + CSV exhibit pair to ``out_dir``.

    Returns ``(json_path, csv_path)``. The timestamp prefix is derived from the
    exhibit's ``generated_at`` so the JSON envelope and the filename agree.
    """
    import os

    envelope = emit_exhibit(attribution, generated_at=generated_at)
    ts = envelope["generated_at"].replace(":", "").replace("-", "")
    os.makedirs(out_dir, exist_ok=True)
    base = f"{ts}-cost-attribution"
    json_path = os.path.join(out_dir, f"{base}.json")
    csv_path = os.path.join(out_dir, f"{base}.csv")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(envelope, f, indent=2, sort_keys=True)
        f.write("\n")
    with open(csv_path, "w", encoding="utf-8") as f:
        f.write(to_csv(attribution))
    return json_path, csv_path
