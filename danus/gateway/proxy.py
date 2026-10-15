"""Forward MCP stdio over a bound FD or one-shot Unix socket; no store access."""

import os
import selectors
import socket
import sys


def main() -> int:
    endpoint = None
    if len(sys.argv) == 3 and sys.argv[1] == "--socket":
        endpoint = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            endpoint.connect(sys.argv[2])
        except OSError:
            endpoint.close()
            return 1
        channel = endpoint.fileno()
    elif len(sys.argv) == 2:
        channel = int(sys.argv[1])
    else:
        return 2
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
        if endpoint is None:
            os.close(channel)
        else:
            endpoint.close()
        selector.close()


if __name__ == "__main__":
    raise SystemExit(main())
