#!/usr/bin/env python3
"""The Platen print agent.

Runs on the workstation the printer is plugged into. It asks Platen for
labels and writes them to the printer, so nothing has to be open inbound to
this machine — which is the whole reason it exists rather than Platen simply
opening a socket.

Standard library only, on purpose: this gets installed on a warehouse PC by
somebody who would rather not set up a Python environment.

    python platen_agent.py --server https://platen.example \\
        --agent wks-office-02 --token plt_... --device /dev/usb/lp0

    python platen_agent.py --server ... --agent ... --token ... --cups zd621
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
import time
import urllib.error
import urllib.request

log = logging.getLogger("platen.agent")


class Server:
    def __init__(self, base: str, agent: str, token: str, timeout: float = 30.0) -> None:
        self.base = base.rstrip("/")
        self.agent = agent
        self.token = token
        self.timeout = timeout

    def post(self, path: str, body: dict) -> dict:
        request = urllib.request.Request(
            f"{self.base}{path}", data=json.dumps(body).encode(), method="POST",
            headers={"content-type": "application/json",
                     "authorization": f"Bearer {self.token}"},
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            return json.loads(response.read() or b"{}")

    def poll(self, devices: list[str]) -> list[dict]:
        return self.post(f"/agents/{self.agent}/poll", {"devices": devices})["jobs"]

    def done(self, job_id: str, error: str | None = None) -> None:
        self.post(f"/agents/{self.agent}/jobs/{job_id}/done", {"error": error})


def write_to_device(path: str, zpl: str) -> None:
    with open(path, "wb") as device:
        device.write(zpl.encode("ascii", "replace") + b"\n")


def write_to_cups(queue: str, zpl: str) -> None:
    result = subprocess.run(
        ["lp", "-d", queue, "-o", "raw", "-"],
        input=zpl.encode("ascii", "replace") + b"\n", capture_output=True,
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", "replace").strip() or f"exit {result.returncode}"
        raise RuntimeError(f"lp refused the job: {detail}")


def run(server: Server, printer: str, use_cups: bool, interval: float) -> None:
    devices = [printer]
    backoff = interval
    while True:
        try:
            jobs = server.poll(devices)
            backoff = interval
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                log.error("Platen turned this agent away (%s). Check the key.", exc.code)
                return
            log.warning("Platen answered %s; waiting %.0fs", exc.code, backoff)
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)
            continue
        except OSError as exc:
            log.warning("cannot reach Platen (%s); waiting %.0fs", exc, backoff)
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)
            continue

        if not jobs:
            time.sleep(interval)
            continue

        for job in jobs:
            try:
                if use_cups:
                    write_to_cups(printer, job["zpl"])
                else:
                    write_to_device(printer, job["zpl"])
            except Exception as exc:                  # noqa: BLE001 — reported, not swallowed
                log.error("label %s did not print: %s", job["id"], exc)
                _tell(server, job["id"], f"{type(exc).__name__}: {exc}")
            else:
                log.info("printed label %s", job["id"])
                _tell(server, job["id"], None)


def _tell(server: Server, job_id: str, error: str | None) -> None:
    """Platen is waiting on this answer, so it is worth a few goes."""
    for attempt in range(5):
        try:
            server.done(job_id, error)
            return
        except OSError as exc:
            log.warning("could not tell Platen about %s (%s)", job_id, exc)
            time.sleep(min(2 ** attempt, 10))
    log.error("gave up telling Platen about %s; it will time out and retry", job_id)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="platen_agent")
    parser.add_argument("--server", required=True, help="where Platen is, e.g. https://platen.example")
    parser.add_argument("--agent", required=True, help="this agent's id, as registered in Platen")
    parser.add_argument("--token", required=True, help="the key Platen gave you when you made it")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--device", help="a printer device, e.g. /dev/usb/lp0")
    group.add_argument("--cups", help="a CUPS queue on this machine")
    parser.add_argument("--interval", type=float, default=2.0, help="seconds between asks")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    log.info("agent %s talking to %s", args.agent, args.server)
    try:
        run(Server(args.server, args.agent, args.token),
            args.cups or args.device, bool(args.cups), args.interval)
    except KeyboardInterrupt:
        log.info("stopping")
    return 0


if __name__ == "__main__":
    sys.exit(main())
