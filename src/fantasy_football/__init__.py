"""Fantasy Football Agent Manager service package."""

__all__ = ["__version__", "main"]

__version__ = "0.1.0"


def main() -> None:
    """Run the supervised read-only service entry point."""

    from .service import main as service_main

    service_main()
