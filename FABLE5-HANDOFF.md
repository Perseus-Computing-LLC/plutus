# Plutus — Fable 5 Development Handoff

> Written 2026-07-02. Grounded in live repo state (verified via GitHub API, not memory recall).
> Current version: **v1.0.0** tag exists and is pushed. Per `ROADMAP-1.0.md`, 1.0.0 is
> "code-frozen; tag/publish pending" as of 2026-06-27 — verify whether PyPI/GHCR publish
> and the external security-review pass have actually completed since then.
> Open issues: **0**. Open PRs: **0**.

## Purpose of this doc

Development on Perseus, Perseus Vault, and Plutus is moving to **Fable 5** for
intensive work. This file is the entry point for that handoff.

## What Plutus is (unchanged)

Greek god of wealth — LLM provider credit & spend monitor for Hermes Agent.
Tracks money draining from providers (deepseek, anthropic, google) and
auto-rebalances model routing toward whichever provider has the most
projected runway (days-left).

## Verified current state (2026-07-02)

Recent merges on `main`:
- Docs rebrand sweep: Mneme → Perseus Vault naming (#89, #90) — appears
  complete for this repo; double-check no stray "Mimir"/"Mneme" prose
  remains before considering the sweep fully closed.
- Internal HANDOFF docs removed from public repo (#88) — good hygiene,
  keep this pattern: this new FABLE5-HANDOFF.md is *meant* to be public
  and durable, not an internal scratch file — don't delete it the same way.
- Deterministic cost/round-trip benchmark + reproducible exhibit (#85-87).

**Two roadmap docs exist and serve different purposes — don't confuse them:**
- `ROADMAP.md` — 12-month FinOps/monitor vision (Q2 2031 "billing standard"
  aspirational endpoint). Long-horizon, directional.
- `ROADMAP-1.0.md` — the actual near-term path to a stable 1.0 billing
  engine release. This is the operative doc for near-term work.

## Phase roadmap for Fable 5

### Phase 1 — Verify and close out the 1.0 release (immediate)
Per `ROADMAP-1.0.md` as of 2026-06-27, the 1.0 blocker queue (#60-66), low-sev
follow-ups (#56-59), and pre-1.0 security fixes (#80) were all merged, the
`/v1` OpenAPI spec + DB forward-compat policy published, and version bumped
to 1.0.0. Two "remaining gates (human/outward)" were listed:
1. Push the `v1.0.0` tag to publish to PyPI + GHCR.
2. External security-review pass before public announcement.

**Action for Fable 5: verify both actually happened.** The tag `v1.0.0` does
exist in the repo (confirmed via API), so gate 1 may be done — but confirm
the PyPI/GHCR publish actually succeeded (check the package registries
directly, don't assume tag-push == publish-succeeded). Confirm gate 2
(external security review) status before treating 1.0 as fully shipped.

### Phase 2 — Provider coverage expansion (per existing ROADMAP.md)
Currently 3 providers tracked (deepseek, anthropic, google). Success metric
in ROADMAP.md targets 3+ live-balance providers within 12 months — already
at the floor of that target, so the next real milestone is adding a 4th
(OpenAI, or whichever is next in spend priority) rather than declaring this
phase done.

### Phase 3 — Alerting (per existing ROADMAP.md)
Success metrics list "none → email + 1 push" as the 12-month alert-channel
target. Not yet started per current state — this is a concrete, scoped
near-term feature: low-balance email alert, at minimum.

### Phase 4 — Billing integration readiness (cross-repo gate, now potentially unblocked)
Perseus Vault's own roadmap explicitly gates any "Billing for hosted tiers
via Plutus" work on **Plutus reaching a stable 1.0 (frozen API + DB
schema)**. Plutus is now tagged 1.0.0. Once Phase 1 verification above
confirms 1.0 is genuinely stable and published (not just tagged), this
cross-repo gate can be treated as open — coordinate with the Perseus Vault
FABLE5-HANDOFF.md Phase 5 note before starting integration work, since that
doc currently still says "pending" as a caution against jumping the gun.

### Phase 5 — Long-horizon FinOps vision (per ROADMAP.md, unchanged)
Per-task-type cost benchmarking, optimization recommendations, model
retirement planning, and the "PayPal for AI agents" positioning remain
directional/exploratory — no committed dates, correctly so. Don't pull
these forward into near-term phases without a real trigger (e.g. actual
demand from Perseus/Vault integration work in Phase 4).

## What NOT to do
- Don't start Phase 4 billing integration work until Phase 1 verification
  is actually done — tag existing ≠ release verified.
- Don't conflate `ROADMAP.md` (aspirational, 2031 horizon) with
  `ROADMAP-1.0.md` (operative, near-term) when deciding what to work on next.

## Where to look first (for Fable 5 onboarding)
1. `ROADMAP-1.0.md` — the operative near-term plan; check off remaining
   gates first.
2. `ROADMAP.md` — long-horizon vision, for context only.
3. This file — handoff snapshot as of 2026-07-02.
4. GitHub API/`gh` directly — verify PyPI/GHCR publish state and any
   security-review status live, don't trust doc claims without checking.
