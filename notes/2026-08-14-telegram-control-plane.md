# Telegram Control Plane, 2026-08-14

## Summary

Implemented the initial Telegram control plane for current-season onboarding.
The feature uses Bot API long polling, enforces a configured user allowlist,
supports the baseline commands, formats daily digests, and forwards the
deduplicated urgent/recovery health events produced by `ReadHealthMonitor`.

## Durable Findings

- Telegram credentials remain runtime-only in the gitignored `.env`; the
  production configuration uses `FFM_TELEGRAM_*` names, while the local
  deployment environment currently supplies equivalent `TELEGRAM_*` names.
- Proposed actions use opaque `act-*` identifiers and timezone-aware expiry
  timestamps. Approvals are safely stubbed until the separately gated ESPN
  write adapter exists.
- A real Bot API delivery of a mock daily digest was accepted by Telegram and
  received by Ben during this session.
- The feature Beads issue `fantasy-football-5m0` is closed. Future ESPN-write
  and draft work remains intentionally open.

## Verification

- `uv run pytest`: 23 passed
- `uv run ruff check .`: passed
- `uv run ruff format --check .`: passed
- `uv run mypy`: passed
