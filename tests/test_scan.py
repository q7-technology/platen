"""Finding printers that are already on the network.

Discovery, not reconnaissance: one port, private ranges only, and a cap on how
many addresses a single call may touch.
"""

from __future__ import annotations

import socket
import threading

import pytest

from app import printers


class FakeZebra:
    """Answers on a port and replies to ~HI the way a Zebra does."""

    def __init__(self, reply: bytes | None = b"\x02ZTC ZT411-203dpi,V93.21.05Z,8,4096KB\x03") -> None:
        self.reply = reply
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            with conn:
                conn.settimeout(1.0)
                try:
                    conn.recv(64)
                    if self.reply:
                        conn.sendall(self.reply)
                except OSError:
                    pass

    def close(self) -> None:
        self.sock.close()


@pytest.fixture
def zebra():
    printer = FakeZebra()
    yield printer
    printer.close()


def test_a_printer_on_the_network_is_found_and_identified(zebra):
    found = printers.scan("127.0.0.1/32", port=zebra.port)

    assert [f.host for f in found] == ["127.0.0.1"]
    assert found[0].port == zebra.port
    assert found[0].model == "ZTC ZT411-203dpi"
    assert found[0].firmware == "V93.21.05Z"


def test_something_that_answers_but_will_not_say_what_it_is_still_counts():
    """Plenty of ZPL-compatible printers ignore ~HI. Answering on the port is
    the thing that matters; the model is a bonus."""
    quiet = FakeZebra(reply=None)
    try:
        found = printers.scan("127.0.0.1/32", port=quiet.port)
    finally:
        quiet.close()

    assert len(found) == 1
    assert found[0].model is None


def test_nothing_listening_means_nothing_found():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        free = s.getsockname()[1]

    assert printers.scan("127.0.0.1/32", port=free) == []


def test_a_public_range_is_refused():
    with pytest.raises(ValueError) as exc:
        printers.scan("8.8.8.0/24")

    assert "private" in str(exc.value)


def test_a_range_too_big_to_be_a_site_is_refused():
    with pytest.raises(ValueError) as exc:
        printers.scan("10.0.0.0/8")

    assert "addresses" in str(exc.value)


def test_nonsense_is_refused_clearly():
    with pytest.raises(ValueError) as exc:
        printers.scan("the warehouse")

    assert "the warehouse" in str(exc.value)


# ------------------------------------------------------------ over the wire

def test_the_scan_endpoint_marks_what_is_already_set_up(client, zebra):
    client.put("/printers/known", json={
        "name": "Known", "transport": {"kind": "tcp", "host": "127.0.0.1", "port": zebra.port}})

    found = client.post("/printers/scan",
                        json={"network": "127.0.0.1/32", "port": zebra.port}).json()

    assert found["found"][0]["configured_as"] == "known"
    assert found["scanned"] == 1


def test_the_scan_endpoint_refuses_a_public_range(client):
    r = client.post("/printers/scan", json={"network": "8.8.8.0/24"})

    assert r.status_code == 422
    assert "private" in r.json()["detail"]
