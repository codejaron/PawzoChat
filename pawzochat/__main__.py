"""Allow ``python -m pawzochat`` to use the project command line."""

from pawzochat.cli import main


if __name__ == "__main__":
    raise SystemExit(main())
