"""Put the agent on a real phone line, through a tunnel.

Twilio dials in from the internet, so it needs a public HTTPS address for the
webhook and a public WSS address for the media stream. A tunnel provides both
without deploying anything, which makes a real carrier call possible before
any hosting decision has been made.

    brew install cloudflared
    make phone

The ordering is the whole reason this script exists. ``PUBLIC_BASE_URL`` is
half of the webhook signature *and* the address Twilio is told to stream to,
so the backend has to know the tunnel's URL before it starts -- and a quick
tunnel's URL is random, issued when it opens. Starting them in the wrong order
gives a signature that never validates, which looks exactly like a
misconfigured auth token. So the tunnel opens first, its address is read from
its own output, and the API is started with it already set.

Nothing is written to ``.env``: the address is different every run, and a
stale one left in a file is the same failure a day later.
"""

from __future__ import annotations

import os
import re
import signal
import socket
import subprocess
import sys
import time
from types import FrameType

#: Preferred, not required. A development API is often already on 8000 with
#: the dashboard pointed at it, and taking the port from under it to make a
#: phone call would be a rude way to answer one.
PREFERRED_PORT = 8000
URL_PATTERN = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")

#: A quick tunnel usually announces itself in a second or two. Past this
#: something is wrong -- no network, or cloudflared asking for a login -- and
#: waiting longer only delays saying so.
URL_TIMEOUT_SECONDS = 30


def main() -> int:
    if not _installed("cloudflared"):
        print(
            "cloudflared is not installed. It is free and needs no account:\n"
            "    brew install cloudflared\n",
            file=sys.stderr,
        )
        return 1

    port = _free_port()
    if port != PREFERRED_PORT:
        print(
            f"Port {PREFERRED_PORT} is busy, so the phone line is on {port}. "
            "Whatever is on 8000 keeps running.",
            file=sys.stderr,
        )

    print("Opening a tunnel...", file=sys.stderr)
    tunnel = subprocess.Popen(
        ("cloudflared", "tunnel", "--url", f"http://localhost:{port}"),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    try:
        url = _wait_for_url(tunnel)
        if url is None:
            print("The tunnel never reported an address.", file=sys.stderr)
            return 1

        _instructions(url)
        api = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "app.main:app",
                "--app-dir",
                "backend",
                "--port",
                str(port),
            ],
            env={**os.environ, "PUBLIC_BASE_URL": url},
        )
        _die_together(tunnel, api)
        return api.wait()
    finally:
        _stop(tunnel)


def _free_port() -> int:
    """The preferred port if it is free, otherwise whatever the OS offers."""
    for candidate in (PREFERRED_PORT, 0):
        with socket.socket() as probe:
            try:
                probe.bind(("127.0.0.1", candidate))
            except OSError:
                continue
            return int(probe.getsockname()[1])
    raise RuntimeError("no port available")  # pragma: no cover - defensive


def _wait_for_url(tunnel: subprocess.Popen[str]) -> str | None:
    """Read the tunnel's own output until it says where it is."""
    deadline = time.monotonic() + URL_TIMEOUT_SECONDS
    assert tunnel.stderr is not None
    while time.monotonic() < deadline:
        line = tunnel.stderr.readline()
        if not line and tunnel.poll() is not None:
            return None
        found = URL_PATTERN.search(line)
        if found:
            return found.group(0)
    return None


def _instructions(url: str) -> None:
    print(
        f"""
  The clinic line is reachable at:

      {url}

  Point your Twilio number at it, once:

      Console -> Phone Numbers -> Active numbers -> (your number)
      Voice Configuration -> "A call comes in"
      Webhook   POST   {url}/twilio/voice

  Then ring the number. The agent answers, and every turn appears in the
  dashboard the same way a browser call does.

  Two things worth knowing. A trial account plays its own notice before
  connecting, which is Twilio's, not ours. And this address changes every
  time this command runs, so the webhook has to be re-pasted each session --
  which is what deploying fixes.
""",
        file=sys.stderr,
    )


def _die_together(tunnel: subprocess.Popen[str], api: subprocess.Popen[bytes]) -> None:
    """Ctrl-C should end the call line, not half of it.

    A tunnel left running after the API stops is a public address answering
    nothing, and the next run gets a different one anyway.
    """

    def stop(_signum: int, _frame: FrameType | None) -> None:
        _stop(api)
        _stop(tunnel)

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)


def _stop(process: subprocess.Popen) -> None:  # type: ignore[type-arg]
    if process.poll() is None:
        process.terminate()


def _installed(command: str) -> bool:
    return subprocess.run(["which", command], capture_output=True).returncode == 0


if __name__ == "__main__":
    raise SystemExit(main())
