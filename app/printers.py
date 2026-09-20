"""Getting bytes to a printer. Three ways, one interface."""

from __future__ import annotations

import ipaddress
import re
import socket
import subprocess
import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from db.models import AgentJob, PrintAgent
from db.models import Printer as PrinterRow


class Transport(Protocol):
    def send(self, data: bytes) -> None: ...
    def probe(self) -> bool: ...


@dataclass
class RawTcp:
    """The usual one: ZPL straight down port 9100. No driver, no spooler."""

    host: str
    port: int = 9100
    timeout: float = 8.0

    def send(self, data: bytes) -> None:
        with socket.create_connection((self.host, self.port), self.timeout) as s:
            s.sendall(data)

    def probe(self) -> bool:
        try:
            with socket.create_connection((self.host, self.port), 2.0):
                return True
        except OSError:
            return False

    def status(self) -> str:
        """~HQES asks the printer how it is. Media out, head open, paused."""
        with socket.create_connection((self.host, self.port), self.timeout) as s:
            s.sendall(b"~HQES")
            s.settimeout(3.0)
            try:
                return s.recv(4096).decode("ascii", "replace")
            except TimeoutError:
                return ""


@dataclass
class Cups:
    """For printers already set up on the print server. -o raw stops CUPS
    from helpfully turning our ZPL into a picture of ZPL."""

    queue: str

    def send(self, data: bytes) -> None:
        try:
            subprocess.run(
                ["lp", "-d", self.queue, "-o", "raw", "-"],
                input=data, check=True, capture_output=True,
            )
        except FileNotFoundError:
            raise RuntimeError(
                "lp isn't on this machine, so Platen can't reach a CUPS queue. "
                "Install cups-client, or point this printer at a socket instead."
            ) from None
        except subprocess.CalledProcessError as exc:
            detail = exc.stderr.decode("utf-8", "replace").strip() or f"exit {exc.returncode}"
            raise RuntimeError(f"lp refused the job for {self.queue!r}: {detail}") from None

    def probe(self) -> bool:
        try:
            return subprocess.run(["lpstat", "-p", self.queue],
                                  capture_output=True).returncode == 0
        except FileNotFoundError:
            return False


# How long after its last check-in an agent still counts as there, and how
# long a label will wait for one before the run gives up and retries.
AGENT_ONLINE = timedelta(seconds=90)
AGENT_TIMEOUT = 60.0


@dataclass
class Agent:
    """A USB printer on someone's desk.

    The agent on that workstation asks Platen for work; Platen never reaches
    into the office network, so nothing has to be open inbound. `send` waits
    for the agent to say the label came out, because on a socket printer a
    returning `send` means the printer took the bytes, and "printed" should
    mean the same thing here.
    """

    agent_id: str
    device: str
    session: Session | None = field(default=None, repr=False)

    def send(self, data: bytes) -> None:
        if self.session is None:
            raise RuntimeError(
                f"{self.agent_id}: an agent printer can only be used where Platen "
                "has its database to hand"
            )
        job = AgentJob(id=uuid.uuid4().hex, agent_id=self.agent_id, device=self.device,
                       zpl=data.decode("ascii", "replace"))
        self.session.add(job)
        self.session.commit()

        deadline = time.monotonic() + AGENT_TIMEOUT
        while time.monotonic() < deadline:
            # commit first so the next read starts a fresh transaction: the
            # agent acks from another process
            self.session.commit()
            done_at, error = self.session.execute(
                select(AgentJob.done_at, AgentJob.error).where(AgentJob.id == job.id)
            ).one()
            if done_at is not None:
                if error:
                    raise RuntimeError(f"{self.agent_id}: {error}")
                return
            time.sleep(0.05)
        raise TimeoutError(
            f"{self.agent_id} didn't collect that label within "
            f"{AGENT_TIMEOUT:.0f} seconds; is the agent running?"
        )

    def probe(self) -> bool:
        if self.session is None:
            return False
        row = self.session.get(PrintAgent, self.agent_id)
        return bool(row and row.last_seen_at
                    and row.last_seen_at > datetime.now(UTC) - AGENT_ONLINE)


@dataclass
class Printer:
    id: str
    name: str
    model: str
    dpi: int
    transport: Transport


# kind -> constructor. A dict rather than an if-chain so a deployment (or a
# test) can register a transport without editing this file.
TRANSPORTS: dict[str, Callable[..., Transport]] = {
    "tcp": lambda c, s=None: RawTcp(c["host"], c.get("port", 9100)),
    "cups": lambda c, s=None: Cups(c["queue"]),
    "agent": lambda c, s=None: Agent(c["agent_id"], c.get("device", ""), s),
}


def build(kind: str, config: dict, session: Session | None = None) -> Transport:
    try:
        factory = TRANSPORTS[kind]
    except KeyError:
        raise ValueError(
            f"unknown transport {kind!r}; one of {', '.join(sorted(TRANSPORTS))}"
        ) from None
    return factory(config, session)


def from_row(row: PrinterRow, session: Session | None = None) -> Printer:
    return Printer(id=row.id, name=row.name, model=row.model, dpi=row.dpi,
                   transport=build(row.transport_kind, row.transport_config, session))


def load(session: Session, printer_id: str) -> Printer | None:
    row = session.get(PrinterRow, printer_id)
    return from_row(row, session) if row else None


# ------------------------------------------------------------------ discovery

# A site's printers live on the site's own network. Refusing anything else is
# both the safe rule and the correct one — nobody's despatch printer is on a
# public address — and one /22 is more than any floor needs at a time.
MAX_ADDRESSES = 1024


@dataclass(frozen=True)
class Discovered:
    host: str
    port: int
    model: str | None = None
    firmware: str | None = None


def _network(text: str) -> ipaddress.IPv4Network | ipaddress.IPv6Network:
    try:
        net = ipaddress.ip_network(text.strip(), strict=False)
    except ValueError:
        raise ValueError(
            f"{text!r} isn't an address or a range; try something like 10.20.4.0/24"
        ) from None
    if not (net.is_private or net.is_loopback or net.is_link_local):
        raise ValueError(
            f"{net} isn't a private range. Platen only looks for printers on your "
            "own network, so give it something like 10.20.4.0/24 or 192.168.1.0/24"
        )
    if net.num_addresses > MAX_ADDRESSES:
        raise ValueError(
            f"{net} covers {net.num_addresses} addresses, and Platen scans at most "
            f"{MAX_ADDRESSES} at a time. Narrow it to a /22 or smaller"
        )
    return net


def identify(host: str, port: int, timeout: float) -> Discovered | None:
    """Open the port, then ask ~HI who is there. A printer that answers the
    port but not the question still counts — plenty of ZPL-compatible units
    ignore ~HI, and the port is what Platen actually needs."""
    try:
        with socket.create_connection((host, port), timeout) as s:
            s.settimeout(timeout)
            try:
                s.sendall(b"~HI")
                reply = s.recv(256)
            except OSError:
                reply = b""
    except OSError:
        return None

    text = re.sub(r"[\x00-\x1f]", "", reply.decode("ascii", "replace")).strip()
    if not text:
        return Discovered(host=host, port=port)
    bits = [b.strip() for b in text.split(",")]
    return Discovered(host=host, port=port, model=bits[0] or None,
                      firmware=bits[1] if len(bits) > 1 and bits[1] else None)


def scan(network: str, *, port: int = 9100, timeout: float = 0.6,
         workers: int = 64) -> list[Discovered]:
    """Every address in `network` that answers on one port. Nothing else is
    touched: one connection each, no other ports, no payload but ~HI."""
    net = _network(network)
    hosts = [str(h) for h in (net.hosts() or [net.network_address])]
    if not hosts:
        hosts = [str(net.network_address)]
    with ThreadPoolExecutor(max_workers=min(workers, len(hosts))) as pool:
        results = pool.map(lambda h: identify(h, port, timeout), hosts)
    return [r for r in results if r is not None]


TEST_LABEL = (
    "^XA^PW600^LL400\n"
    "^FO40,40^A0N,48,48^FDPlaten test^FS\n"
    "^FO40,120^BY3,3,90^BCN,90,Y,N,N^FDPLATEN-TEST^FS\n"
    "^FO40,280^A0N,28,28^FDIf you can read this, the path works.^FS\n"
    "^XZ"
).encode("ascii")
