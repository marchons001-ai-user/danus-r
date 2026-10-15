"""Allow python -m danus.tui as an alias for the existing TUI entry point."""
from . import main

if __name__ == "__main__":
    raise SystemExit(main())
