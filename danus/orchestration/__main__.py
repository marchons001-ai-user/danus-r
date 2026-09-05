"""Run the danus CLI: ``python -m danus.orchestration <verb> …`` (bin/danus execs this)."""

from __future__ import annotations

import sys

from .cli import main

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "research":
        from .supervisor_llm import research_main

        sys.exit(research_main(sys.argv[2:]))
    if len(sys.argv) > 1 and sys.argv[1] == "guide":
        from .steering import main as guide_main

        sys.exit(guide_main(sys.argv[2:]))
    if len(sys.argv) > 1 and sys.argv[1] == "resume":
        from .steering import resume_main

        sys.exit(resume_main(sys.argv[2:]))
    if len(sys.argv) > 1 and sys.argv[1] == "supervise":
        from .supervisor_llm import supervise_main

        sys.exit(supervise_main(sys.argv[2:]))
    if len(sys.argv) > 1 and sys.argv[1] == "tui":
        from danus.tui import main as tui_main

        sys.exit(tui_main(sys.argv[2:]))
    sys.exit(main())
