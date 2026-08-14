"""Smoke tests for the service foundation."""

from fantasy_football import __version__, main


def test_package_imports() -> None:
    assert __version__ == "0.1.0"


def test_main(capsys) -> None:  # type: ignore[no-untyped-def]
    main()
    assert capsys.readouterr().out == "Fantasy Football Agent Manager\n"
