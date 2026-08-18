# Fantasy Football Agent Manager

Python service foundation for the Fantasy Football Agent Manager.

## Development

Install [uv](https://docs.astral.sh/uv/) and run all commands from the repository
root. `uv` owns the interpreter, environment, dependencies, lockfile, tests, and
quality checks.

```sh
uv sync
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run fantasy-football
```

The package source lives in `src/fantasy_football`; tests live in `tests`.
Runtime secrets, session state, and action logs must remain outside Git.

## Read-only ESPN access

The `EspnApiReader` is the only provider integration currently implemented. It
loads `espn_s2` and `SWID` through a `SecretProvider`, creates the third-party
client with debug logging disabled, and returns app-owned `LeagueSnapshot`
contracts. It exposes no ESPN mutation methods.

Required runtime configuration is supplied through `FFM_LEAGUE_ID`,
`FFM_SEASON`, and an absolute owner-only `FFM_DATA_DIR`. Use
`JsonFileSecretProvider` only with an owner-only secret file, or provide an
encrypted `SecretProvider` for production deployments. `FFM_WRITE_ENABLED` and
raw payload logging are rejected by design.

After Ben completes the interactive ESPN login in a temporary browser context,
capture the browser-produced cookies with `ESPNSessionStore.capture(...)`. The
stored file can be checked or explicitly cleared for reauthentication without
printing secrets:

```sh
uv run fantasy-football-session status --path /absolute/path/session.json
uv run fantasy-football-session clear --path /absolute/path/session.json
```

This workflow never accepts or automates an ESPN password.

Run the post-login read-only onboarding check with the required environment
values and session path:

```sh
FFM_LEAGUE_ID=123 FFM_SEASON=2026 FFM_DATA_DIR=/absolute/path/runtime \
  uv run fantasy-football-onboard --session-path /absolute/path/session.json
```

The command prints and stores only normalized counts, league identity, Ben's
team roster sizes, deadlines, and freshness metadata.

Polling integrations should use `ReadHealthMonitor` around the reader. It
retries transient failures with bounded backoff, fails closed on stale or
incomplete snapshots, and emits deduplicated `lost_access`, `service_failure`,
and `recovered` events for the future Telegram health channel.

## Supervised service

`fantasy-football` is the restartable, read-only service entrypoint for the Pi.
It uses Telegram long polling (no webhook), runs bounded ESPN monitor/decision
cycles, schedules one daily UTC digest, and persists an owner-only
`service-state.json` beneath `FFM_DATA_DIR`. That state contains only the
Telegram offset, UTC digest marker, monitor timestamp, one-way snapshot
fingerprint, health interval, and global pause—not credentials, snapshots,
messages, or action evidence. A restart consequently keeps a user pause and
does not replay completed Telegram updates, unchanged decisions, or daily
digests.

The service remains default-off for ESPN writes and does not construct an ESPN
browser/action executor. Model analysis is also default-off: with no model
configuration, its built-in bounded analysis port records safe no-action
decisions and constructs no model client or network call. Use a live command
only with the runtime-only Telegram and ESPN session configuration described
above:

```sh
FFM_LEAGUE_ID=123 FFM_SEASON=2026 FFM_DATA_DIR=/absolute/path/runtime \
  FFM_SESSION_PATH=/absolute/path/session.json \
  FFM_TELEGRAM_BOT_TOKEN='...' FFM_TELEGRAM_ALLOWED_USER_ID=123 \
  uv run fantasy-football --readiness
```

`--once` executes a single supervised pass and prints redacted readiness JSON;
without it the process runs until stopped by its supervisor. Optional
`FFM_MONITOR_INTERVAL_SECONDS`, `FFM_DIGEST_HOUR_UTC`, and
`FFM_DIGEST_MINUTE_UTC` configure the bounded monitor and UTC digest schedule.

## Model analysis configuration

Set all three values below to enable the current OpenAI Responses adapter; the
model name is an operator choice, so changing it does not require application
code changes. The API key is read only at process start, excluded from object
representations and service state, and must never be committed or logged.

```sh
export FFM_MODEL_PROVIDER='openai'
export FFM_MODEL_NAME='your-supported-model'
export FFM_MODEL_API_KEY='...'
export FFM_MODEL_MAX_OUTPUT_TOKENS='1000'  # optional; 1 through 4096
export FFM_MODEL_TIMEOUT_SECONDS='30'      # optional; greater than 0 through 60
```

Partial model configuration stops startup rather than silently falling back.
Configured requests are bounded by the existing `AnalysisLimits`, use
[Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs)
with a role-specific closed JSON schema, disable provider response storage, and
fail closed on provider errors, incomplete output, invalid JSON, or exceeded
bounds. The adapter receives only the normalized analysis request; it has no
ESPN client, browser, executor, or write capability.

## ESPN action foundation

`fantasy_football.execution` contains the provider-isolated, fake-tested write
foundation. `ActionAuthorizer` is default-off and checks pause state, authorized
approval and expiry, and a fresh precondition fingerprint before the injected
browser factory can run. `PlaywrightESPNActionExecutor` reserves idempotency
keys, records immutable season-partitioned JSONL events, refreshes through an
injected verification port, and fails closed to human review on timeout,
navigation failure, or an unverifiable postcondition.

This issue adds no authenticated ESPN mutation or selectors. Browser ports and
verification predicates remain fakes until the action-specific lineup, waiver,
and trade work is implemented. Evidence is bounded and redacted before it is
stored; raw network payloads are outside the contract.

## Telegram control plane

Telegram credentials and the allowlisted account ID are runtime-only values:

```sh
export FFM_TELEGRAM_BOT_TOKEN='...'
export FFM_TELEGRAM_ALLOWED_USER_ID='123456789'
export FFM_TELEGRAM_CHAT_ID='123456789'  # optional default notification chat
```

`fantasy_football.telegram.TelegramService` uses Bot API long polling and
`CommandRouter` accepts only the configured user. It supports `status`, `run`,
`approve`, `veto`, `pause`, `resume`, `why`, and the safely reserved `draft`
command. Proposed actions carry an opaque identifier and UTC expiry. The
current service is intentionally read-only: approvals are recorded as safe
stubs until a separately gated ESPN write adapter exists. Health events from
`ReadHealthMonitor` can be sent with `send_health_event`; repeated failures are
already deduplicated by the monitor.

## Tuesday post-mortems and publication

After Monday Night Football, build a `WeeklyPostmortem` from curated
`DecisionRecord` values with `fantasy_football.reports.WeeklyPostmortem.from_records`.
The renderer writes only the public decision, outcome, lesson, error/missed
opportunity, and strategy-change summaries; pending actions, execution
references, raw action-ledger evidence, and secrets are excluded.

```python
from pathlib import Path

from fantasy_football.reports import WeeklyPostmortem, write_weekly_report

report = WeeklyPostmortem.from_records(
    2026,
    1,
    decision_history.records,
    lessons=("Prefer the higher-floor flex when the matchup is neutral.",),
    errors=("Missed the Sunday morning status change.",),
    strategy_changes=("Add a final injury-status check before lineup approval.",),
)
path = write_weekly_report(report, Path("/absolute/path/reports"))
print(path)
```

`write_weekly_report` creates `reports/<season>/week-<week>.html` exclusively;
it refuses to replace an existing season/week artifact, preserving report
history. Review the generated file locally before the explicit publication
workflow:

1. On `main`, review the HTML and commit only the curated report artifact.
2. On the `pages` branch, copy the reviewed artifact to the matching path and
   review the diff again.
3. Commit the reviewed file on the `pages` branch and publish only after
   explicit human approval.

Report generation does not switch branches, commit, push, or publish. Never
copy raw decision-history or action-ledger files to either branch.
