"""Run the danus CLI: ``python -m danus.orchestration <verb> …`` (bin/danus execs this)."""

from __future__ import annotations

import sys

from .cli import main

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "research":
        from .planning import main as research_main

        sys.exit(research_main(sys.argv[2:]))
    sys.exit(main())
