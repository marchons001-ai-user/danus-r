"""Run a trusted stdio Gateway or a host-bound worker Gateway."""

import argparse
import json

from .server import build_app

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker-binding")
    args = parser.parse_args()
    binding = json.loads(args.worker_binding) if args.worker_binding else None
    if binding is None:
        build_app().run()
    else:
        from .capability import bind_stdio_peer
        app = build_app(worker_binding=binding)
        bind_stdio_peer()
        app.run()
