"""Getting bytes to a printer. Three ways, one interface."""

from __future__ import annotations

import ipaddress
import re
import socket
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable, Protocol

import httpx
from sqlalchemy.orm import Session

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
            except socket.timeout:
                return ""


@dataclass
class Cups:
    """For printers already set up on the print server. -o raw stops CUPS
    from helpfully turning our ZPL into a picture of ZPL."""

    queue: str

    def send(self, data: bytes) -> None:
        subprocess.run(
            ["lp", "-d", self.queue, "-o", "raw", "-"],
            input=data, check=True, capture_output=True,
        )

    def probe(self) -> bool:
        r = subprocess.run(["lpstat", "-p", self.queue], capture_output=True)
        return r.returncode == 0


@dataclass
class Agent:
    """A USB printer on someone's desk. A small agent on that machine holds a
    connection open to us and forwards whatever we post to it."""

    relay_url: str
    agent_id: str
    device: str
    token: str

    def send(self, data: bytes) -> None:
        r = httpx.post(
            f"{self.relay_url}/agents/{self.agent_id}/print",
            params={"device": self.device},
            content=data,
            headers={"authorization": f"Bearer {self.token}",
                     "content-type": "application/octet-stream"},
            timeout=20.0,
        )
        r.raise_for_status()

    def probe(self) -> bool:
        r = httpx.get(f"{self.relay_url}/agents/{self.agent_id}", timeout=5.0)
        return r.status_code == 200 and r.json().get("online", False)


@dataclass
class Printer:
    id: str
    name: str
    model: str
    dpi: int
    transport: Transport


# kind -> constructor. A dict rather than an if-chain so a deployment (or a
# test) can register a transport without editing this file.
TRANSPORTS: dict[str, Callable[[dict], Transport]] = {
    "tcp": lambda c: RawTcp(c["host"], c.get("port", 9100)),
    "cups": lambda c: Cups(c["queue"]),
    "agent": lambda c: Agent(c["relay_url"], c["agent_id"], c["device"], c["token"]),
}


def build(kind: str, config: dict) -> Transport:
    try:
        factory = TRANSPORTS[kind]
    except KeyError:
        raise ValueError(
            f"unknown transport {kind!r}; one of {', '.join(sorted(TRANSPORTS))}"
        ) from None
    return factory(config)


def from_row(row: PrinterRow) -> Printer:
    return Printer(id=row.id, name=row.name, model=row.model, dpi=row.dpi,
                   transport=build(row.transport_kind, row.transport_config))


def load(session: Session, printer_id: str) -> Printer | None:
    row = session.get(PrinterRow, printer_id)
    return from_row(row) if row else None


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
