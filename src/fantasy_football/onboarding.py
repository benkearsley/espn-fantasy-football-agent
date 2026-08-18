"""Redacted, read-only onboarding verification command."""

from __future__ import annotations

import json
import os
import tempfile
from argparse import ArgumentParser
from pathlib import Path
from typing import Any

from .config import ConfigurationError, ServiceConfig
from .contracts import ESPNLeagueReader, LeagueSnapshot
from .espn_adapter import ESPNAdapterError, EspnApiReader
from .secrets import SecretProviderError
from .session import ESPNSessionStore


def build_summary(
    snapshot: LeagueSnapshot, *, owner_name: str = "Ben"
) -> dict[str, Any]:
    """Select safe normalized metadata for operator output and local storage."""

    owner_teams = [team for team in snapshot.teams if team.owner_name == owner_name]
    return {
        "success": True,
        "league": {
            "id": snapshot.settings.league_id,
            "name": snapshot.settings.name,
            "season": snapshot.settings.season,
        },
        "owner": {
            "name": owner_name,
            "teams": [
                {
                    "id": team.team_id,
                    "name": team.name,
                    "roster_size": len(team.roster),
                }
                for team in owner_teams
            ],
        },
        "counts": {
            "teams": len(snapshot.teams),
            "matchups": len(snapshot.matchups),
            "draft_picks": len(snapshot.draft_picks),
            "transactions": len(snapshot.transactions),
            "free_agents": len(snapshot.free_agents),
        },
        "player_kickoffs": [
            {
                "player_id": kickoff.player_id,
                "kickoff_at": kickoff.kickoff_at.isoformat(),
            }
            for kickoff in snapshot.player_kickoffs
        ],
        "current_week": snapshot.status.current_week,
        "season_state": snapshot.status.season_state,
        "deadlines": [deadline.name for deadline in snapshot.status.deadlines],
        "source_timestamp": snapshot.source_timestamp.isoformat(),
    }


def run_onboarding(
    config: ServiceConfig,
    reader: ESPNLeagueReader,
    *,
    owner_name: str = "Ben",
    output_path: Path | None = None,
) -> dict[str, Any]:
    """Read, summarize, and optionally persist one normalized snapshot check."""

    summary = build_summary(reader.read_snapshot(), owner_name=owner_name)
    if output_path is not None:
        _write_metadata(output_path, summary)
    return summary


def main() -> None:
    """Run the first live read-only integration check."""

    parser = ArgumentParser(prog="fantasy-football-onboard")
    parser.add_argument("--session-path", type=Path)
    parser.add_argument("--owner-name", default=os.environ.get("FFM_OWNER_NAME", "Ben"))
    args = parser.parse_args()
    try:
        config = ServiceConfig.from_env()
        session_path = args.session_path or _session_path_from_env()
        config.ensure_secure_data_dir()
        reader = EspnApiReader(config, ESPNSessionStore(session_path))
        summary = run_onboarding(
            config,
            reader,
            owner_name=args.owner_name,
            output_path=config.data_dir / "onboarding-summary.json",
        )
        print(json.dumps(summary, indent=2, sort_keys=True))
    except (ConfigurationError, SecretProviderError, ESPNAdapterError) as exc:
        raise SystemExit(f"onboarding failed: {exc}") from None


def _session_path_from_env() -> Path:
    value = os.environ.get("FFM_SESSION_PATH")
    if not value:
        raise ConfigurationError("missing FFM_SESSION_PATH or --session-path")
    path = Path(value)
    if not path.is_absolute():
        raise ConfigurationError("session path must be absolute")
    return path


def _write_metadata(path: Path, summary: dict[str, Any]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    temporary_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            delete=False,
        ) as handle:
            temporary_path = handle.name
            os.chmod(handle.name, 0o600)
            json.dump(summary, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        os.chmod(path, 0o600)
    except (OSError, TypeError, ValueError) as exc:
        raise ConfigurationError("unable to persist onboarding metadata") from exc
    finally:
        if temporary_path is not None:
            try:
                Path(temporary_path).unlink(missing_ok=True)
            except OSError:
                pass
