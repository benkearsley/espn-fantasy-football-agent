# Decision Manager Implementation Wave, 2026-08-17

## Summary

- Completed the read-only decision-manager application layer under the `4oa`
  epic. Beads `4oa.1`, `4oa.2`, `4oa.3`, `4oa.4`, `4oa.7`, `4oa.8`, `4oa.9`,
  and `4oa.10` are closed after review and quality gates.
- `4oa.5` is the next ready item and remains operator-assisted: it needs the
  real ESPN session, Telegram credentials/chat, model configuration, and
  network access. `4oa.6` remains gated on that live evidence and Ben's
  explicit authorization.

## Durable Findings

- Decision history is append-only, season-partitioned, owner-only JSONL. It
  stores bounded/redacted ESPN facts, specialist dissent, Lead confidence and
  uncertainty, pending-action approval/veto/expiry transitions, and safe
  execution references without changing the executor ledger.
- Lead orchestration is provider-neutral and fail-closed. It enforces fresh,
  complete snapshots, bounded structured calls, retry ceilings, specialist
  dissent, risk review, and persisted no-action records for invalid or
  unavailable analysis.
- Approval-required lineup and trade-acceptance recommendations become stable,
  durable pending actions. Missing, ambiguous, duplicate, or stale kickoff and
  trade deadline facts fail closed with the reason retained in `why`.
- ESPN kickoff facts are normalized from already-loaded player schedules into
  UTC `PlayerKickoff` values; no extra read or write request is introduced.
- The supervised service persists only owner-only operational state (Telegram
  offset, pause, health interval, digest marker, monitor timestamp, and a
  one-way snapshot fingerprint), serializes cycles, deduplicates health/digest
  notifications, and never constructs an ESPN write executor.
- Runtime model analysis is optional and provider-boundary isolated. When
  configured, the OpenAI Responses adapter uses strict role-specific JSON
  schemas, bounded output, `store=false`, runtime-only credentials, and
  redacted fail-closed errors. Without configuration, no model client or
  network call is made.
- Reports are curated, deterministic HTML artifacts that exclude pending
  actions, execution references, raw ledger evidence, and secrets. Publication
  remains an explicit human-reviewed workflow.

## Verification

- Full test suite: 77 passed.
- Ruff check and format check passed.
- Strict mypy passed.
- `git diff --check` and `bd lint` passed.
- No live ESPN, Telegram, or model API call was made during implementation.

## Canon Boundary

`CANONICAL_SPEC.md` was reviewed and intentionally unchanged. Implementation
details and the remaining live-gate procedure belong in Beads and this note.
