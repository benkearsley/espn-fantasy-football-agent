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
