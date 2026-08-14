"""Tests for interactive session capture and safe status handling."""

from pathlib import Path

import pytest

from fantasy_football.session import ESPNSessionStore, SessionError


def test_capture_round_trip_is_restricted_and_status_is_redacted(
    tmp_path: Path,
) -> None:
    path = tmp_path / "session.json"
    store = ESPNSessionStore(path)
    store.capture(
        [
            {"name": "espn_s2", "value": "private-s2"},
            {"name": "SWID", "value": "private-swid"},
        ],
        browser_state={"cookies": [{"name": "ignored-by-reader", "value": "x"}]},
    )

    assert path.stat().st_mode & 0o777 == 0o600
    assert store.status().usable is True
    credentials = store.get_credentials()
    assert credentials.espn_s2 == "private-s2"
    assert credentials.swid == "private-swid"
    assert "private-s2" not in repr(store.status())
    assert "private-swid" not in repr(store.status())


def test_capture_requires_both_espn_cookies(tmp_path: Path) -> None:
    with pytest.raises(SessionError, match="required cookies"):
        ESPNSessionStore(tmp_path.joinpath("session.json").absolute()).capture(
            [{"name": "espn_s2", "value": "only-one"}]
        )


def test_missing_and_clear_session_are_safe(tmp_path: Path) -> None:
    path = tmp_path / "session.json"
    store = ESPNSessionStore(path)
    assert store.status().exists is False
    with pytest.raises(Exception) as raised:
        store.get_credentials()
    assert "private" not in str(raised.value)
    store.clear()
    assert path.exists() is False
