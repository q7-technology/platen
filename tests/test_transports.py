"""The raw socket transport, against a real socket.

It is the one every site uses and the only one the pytest suite had never
touched — it was exercised by hand in a browser and nowhere else, so a
regression would have reached a printer before it reached a test.
"""

from __future__ import annotations

import socket
import threading

import pytest

from app import printers


class Listener:
    """Answers on a port, keeps what it was sent, optionally replies."""

    def __init__(self, reply: bytes = b"") -> None:
        self.reply = reply
        self.received: list[bytes] = []
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            with conn:
                conn.settimeout(2.0)
                data = b""
                try:
                    while chunk := conn.recv(4096):
                        data += chunk
                        if self.reply:
                            conn.sendall(self.reply)
                            break
                except OSError:
                    pass
                if data:
                    self.received.append(data)

    def close(self) -> None:
        self.sock.close()


@pytest.fixture
def listener():
    got = Listener()
    yield got
    got.close()


def _closed_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_zpl_arrives_at_the_socket_byte_for_byte(listener):
    printers.RawTcp("127.0.0.1", listener.port).send(b"^XA^FDhello^FS^XZ")

    for _ in range(50):
        if listener.received:
            break
        import time
        time.sleep(0.02)
    assert listener.received == [b"^XA^FDhello^FS^XZ"]


def test_a_printer_that_is_there_probes_true(listener):
    assert printers.RawTcp("127.0.0.1", listener.port).probe() is True


def test_a_printer_that_is_not_there_probes_false():
    assert printers.RawTcp("127.0.0.1", _closed_port()).probe() is False


def test_sending_to_nothing_raises_rather_than_going_quiet():
    with pytest.raises(OSError):
        printers.RawTcp("127.0.0.1", _closed_port()).send(b"^XA^XZ")


def test_asking_a_printer_how_it_is():
    """~HQES is how a Zebra says media out, head open, paused."""
    talking = Listener(reply=b"\x02   PRINTER STATUS   ERRORS: 1 00000000 00000001\x03")
    try:
        status = printers.RawTcp("127.0.0.1", talking.port).status()
    finally:
        talking.close()

    assert "PRINTER STATUS" in status
    assert talking.received and talking.received[0] == b"~HQES"


def test_a_printer_that_will_not_say_how_it_is_gives_an_empty_answer(listener):
    assert printers.RawTcp("127.0.0.1", listener.port, timeout=1.0).status() == ""


def test_a_run_goes_down_a_real_socket_end_to_end(seeded, listener):
    seeded.put("/printers/socket", json={
        "name": "Socket", "transport": {"kind": "tcp", "host": "127.0.0.1",
                                        "port": listener.port}})
    run = seeded.post("/runs", json={"template_id": "carton", "printer_id": "socket",
                                     "params": {"despatch_date": "2026-09-18"}}).json()

    from app import jobs
    jobs.print_run(run["id"])

    assert seeded.get(f"/runs/{run['id']}").json()["status"] == "done"
    assert len(listener.received) == 12
    assert all(b"^XA" in label for label in listener.received)
