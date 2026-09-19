"""Forward MCP stdio over an inherited capability FD; no store access."""

import os
import selectors
import sys


def main() -> int:
    channel = int(sys.argv[1])
    selector = selectors.DefaultSelector()
    selector.register(0, selectors.EVENT_READ, channel)
    selector.register(channel, selectors.EVENT_READ, 1)
    try:
        while True:
            for key, _ in selector.select():
                data = os.read(key.fd, 65536)
                if not data:
                    return 1
                while data:
                    data = data[os.write(key.data, data):]
    except OSError:
        return 1
    finally:
        os.close(channel)
        selector.close()


if __name__ == "__main__":
    raise SystemExit(main())
