"""Getting bytes to a printer. Three ways, one interface."""

from __future__ import annotations

import socket
import subprocess
from dataclasses import dataclass
from typing import Protocol

import httpx


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


PRINTERS: dict[str, Printer] = {}


def build(config: dict) -> Transport:
    kind = config["kind"]
    if kind == "tcp":
        return RawTcp(config["host"], config.get("port", 9100))
    if kind == "cups":
        return Cups(config["queue"])
    if kind == "agent":
        return Agent(config["relay_url"], config["agent_id"],
                     config["device"], config["token"])
    raise ValueError(f"unknown transport: {kind}")


TEST_LABEL = (
    "^XA^PW600^LL400\n"
    "^FO40,40^A0N,48,48^FDPlaten test^FS\n"
    "^FO40,120^BY3,3,90^BCN,90,Y,N,N^FDPLATEN-TEST^FS\n"
    "^FO40,280^A0N,28,28^FDIf you can read this, the path works.^FS\n"
    "^XZ"
).encode("ascii")
