"""Tests for benchmark cost attribution (issues #85, #86).

Verifies the deterministic cost/round-trip comparison between the agentic
baseline and resolve-before-context, and the reproducibility of the emitted
exhibit.
"""
from __future__ import annotations

import json

from plutus_agent.benchmark_cost import (
    AGENTIC,
    RESOLVE_BEFORE_CONTEXT,
    ApproachRun,
    CallRecord,
    attribute,
    emit_exhibit,
    to_csv,
    write_exhibit,
)
from plutus_agent.pricing import PRICE_TABLE_AS_OF


def _scenario():
    """A 5-directive task: agentic needs 6 round-trips, resolve needs 1.

    Agentic: one discovery/fetch round-trip per directive (5) + a final answer
    call (1) = 6 model round-trips, each re-sending the growing context.
    Resolve-before-context: directives resolved offline, ONE model call.
    """
    model = ("anthropic", "claude-opus-4-8")
    agentic = ApproachRun(AGENTIC)
    # 5 gathering round-trips, each re-sends a growing prompt (cumulative input).
    for i in range(5):
        agentic.record(CallRecord(
            provider=model[0], model=model[1],
            input_tokens=1000 * (i + 1), output_tokens=120,
            label=f"gather-{i}",
        ))
    # Final answer call sees the full assembled context.
    agentic.record(CallRecord(
        provider=model[0], model=model[1],
        input_tokens=6000, output_tokens=400, label="final-answer",
    ))

    resolve = ApproachRun(RESOLVE_BEFORE_CONTEXT)
    # One call: the resolved context is assembled offline, answered in one shot.
    resolve.record(CallRecord(
        provider=model[0], model=model[1],
        input_tokens=6000, output_tokens=400, label="single-shot",
    ))
    return agentic, resolve


def test_round_trip_counts():
    agentic, resolve = _scenario()
    assert agentic.round_trips == 6
    assert resolve.round_trips == 1


def test_attribution_costs_and_reduction():
    agentic, resolve = _scenario()
    attr = attribute("six-directive-context", agentic, resolve)

    a = attr["approaches"][AGENTIC]
    r = attr["approaches"][RESOLVE_BEFORE_CONTEXT]

    # Resolve uses strictly fewer round-trips and costs strictly less.
    assert a["round_trips"] == 6
    assert r["round_trips"] == 1
    assert a["cost_usd"] > r["cost_usd"] > 0

    # Reduction block is internally consistent.
    assert attr["reduction"]["round_trips_eliminated"] == 5
    assert attr["reduction"]["cost_usd_saved"] == round(a["cost_usd"] - r["cost_usd"], 6)
    assert 0 < attr["reduction"]["cost_pct"] <= 100
    assert attr["price_table_as_of"] == PRICE_TABLE_AS_OF


def test_attribution_is_deterministic():
    """Same inputs => byte-identical attribution JSON (no timestamp inside)."""
    a1, r1 = _scenario()
    a2, r2 = _scenario()
    j1 = json.dumps(attribute("t", a1, r1), sort_keys=True)
    j2 = json.dumps(attribute("t", a2, r2), sort_keys=True)
    assert j1 == j2


def test_exact_cost_math():
    """Cost matches the frozen price table exactly (opus: $15/M in, $75/M out)."""
    run = ApproachRun(RESOLVE_BEFORE_CONTEXT).record(
        CallRecord("anthropic", "claude-opus-4-8",
                   input_tokens=1_000_000, output_tokens=1_000_000)
    )
    # 1M input * $15/M + 1M output * $75/M = 90.0
    assert run.total_cost_usd() == 90.0


def test_exhibit_is_reproducible_when_timestamp_pinned():
    agentic, resolve = _scenario()
    attr = attribute("six-directive-context", agentic, resolve)
    e1 = emit_exhibit(attr, generated_at="2026-06-28T00:00:00Z")
    e2 = emit_exhibit(attr, generated_at="2026-06-28T00:00:00Z")
    assert json.dumps(e1, sort_keys=True) == json.dumps(e2, sort_keys=True)
    assert e1["deterministic"] is True
    assert e1["generated_at"] == "2026-06-28T00:00:00Z"


def test_csv_is_deterministic_and_two_rows():
    agentic, resolve = _scenario()
    attr = attribute("six-directive-context", agentic, resolve)
    csv1 = to_csv(attr)
    csv2 = to_csv(attr)
    assert csv1 == csv2
    lines = csv1.strip().split("\n")
    assert len(lines) == 3  # header + 2 approaches
    assert lines[0].startswith("task,approach,round_trips")
    assert AGENTIC in csv1 and RESOLVE_BEFORE_CONTEXT in csv1


def test_write_exhibit_pair(tmp_path):
    agentic, resolve = _scenario()
    attr = attribute("six-directive-context", agentic, resolve)
    jpath, cpath = write_exhibit(
        attr, str(tmp_path), generated_at="2026-06-28T00:00:00Z"
    )
    assert jpath.endswith("20260628T000000Z-cost-attribution.json")
    assert cpath.endswith("20260628T000000Z-cost-attribution.csv")
    # Round-trip the JSON envelope.
    envelope = json.loads(open(jpath, encoding="utf-8").read())
    assert envelope["exhibit"] == "cost-attribution"
    assert envelope["attribution"]["reduction"]["round_trips_eliminated"] == 5
    # CSV has the two approach rows.
    assert "agentic" in open(cpath, encoding="utf-8").read()
