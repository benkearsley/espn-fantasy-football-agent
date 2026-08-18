"""Smoke tests for the supervised service entrypoint."""

from pytest import MonkeyPatch

import fantasy_football.service as service
from fantasy_football import __version__, main


def test_package_imports() -> None:
    assert __version__ == "0.1.0"


def test_main_delegates_to_supervised_service(
    monkeypatch: MonkeyPatch,
) -> None:
    called: list[bool] = []

    monkeypatch.setattr(service, "main", lambda: called.append(True))
    main()
    assert called == [True]
