"""`python -m kestrel_agent` — run the session host.

This is meant to be started once and left running (a systemd user unit on the
Pi; a background terminal in WSL for dev), independently of whether the
server is up. See `kestrel_agent.host` for why it exists and why the wire
protocol looks the way it does.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
from pathlib import Path

from .host import SessionHost, SessionHostAlreadyRunning, default_socket_path

logger = logging.getLogger("kestrel_agent")


def _default_data_dir() -> Path:
    return Path(os.environ.get("KESTREL_DATA", str(Path.home() / ".kestrel")))


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="kestrel_agent", description=__doc__)
    parser.add_argument(
        "--socket",
        type=Path,
        default=None,
        help="Path to the Unix domain socket (default: $KESTREL_DATA/session-host.sock)",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser.parse_args(argv)


async def _run(socket_path: Path) -> None:
    host = SessionHost(socket_path)
    await host.start()
    logger.info("kestrel session host listening on %s", socket_path)

    loop = asyncio.get_running_loop()
    stop: asyncio.Future[None] = loop.create_future()

    def _request_stop() -> None:
        if not stop.done():
            stop.set_result(None)

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _request_stop)
        except (NotImplementedError, RuntimeError):
            # Not every platform/context allows registering signal handlers.
            # Serve anyway rather than refusing to start over a convenience -
            # Ctrl-C still works via KeyboardInterrupt in that case.
            pass

    try:
        await stop
    finally:
        await host.stop()


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING)
    socket_path = args.socket or default_socket_path(_default_data_dir())
    try:
        asyncio.run(_run(socket_path))
    except SessionHostAlreadyRunning as exc:
        # A second host racing the first must lose loudly, not silently steal
        # the socket out from under a live one and strand its sessions.
        print(f"kestrel_agent: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
