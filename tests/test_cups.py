"""The CUPS transport, against a stand-in for lp and lpstat.

It has never been exercised. -o raw is the whole point of it: without that,
CUPS helpfully turns the ZPL into a picture of ZPL.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from app import printers

LP = """#!/bin/sh
echo "$@" > "{log}/lp.args"
cat > "{log}/lp.stdin"
exit {code}
"""

LPSTAT = """#!/bin/sh
echo "$@" > "{log}/lpstat.args"
exit {code}
"""


@pytest.fixture
def fake_cups(tmp_path, monkeypatch):
    def install(lp_code: int = 0, lpstat_code: int = 0) -> Path:
        binaries = tmp_path / "bin"
        binaries.mkdir(exist_ok=True)
        for name, body, code in (("lp", LP, lp_code), ("lpstat", LPSTAT, lpstat_code)):
            script = binaries / name
            script.write_text(body.format(log=tmp_path, code=code))
            script.chmod(script.stat().st_mode | stat.S_IEXEC)
        monkeypatch.setenv("PATH", f"{binaries}{os.pathsep}{os.environ['PATH']}")
        return tmp_path
    return install


def test_zpl_goes_to_the_queue_raw(fake_cups):
    log = fake_cups()

    printers.Cups("zd621-workshop").send(b"^XAtest^XZ")

    assert (log / "lp.args").read_text().split() == ["-d", "zd621-workshop", "-o", "raw", "-"]
    assert (log / "lp.stdin").read_bytes() == b"^XAtest^XZ"


def test_a_queue_that_refuses_the_job_is_not_silent(fake_cups):
    fake_cups(lp_code=1)

    with pytest.raises(Exception) as exc:
        printers.Cups("gone").send(b"^XA^XZ")

    assert "gone" in str(exc.value) or "1" in str(exc.value)


def test_probing_asks_lpstat_about_that_queue(fake_cups):
    log = fake_cups()

    assert printers.Cups("zd621-workshop").probe() is True
    assert (log / "lpstat.args").read_text().split() == ["-p", "zd621-workshop"]


def test_a_queue_lpstat_has_never_heard_of_is_not_online(fake_cups):
    fake_cups(lpstat_code=1)

    assert printers.Cups("nowhere").probe() is False


def test_a_machine_with_no_cups_at_all_says_so(monkeypatch, tmp_path):
    """A server without cups-client installed should give a sentence, not a
    FileNotFoundError out of subprocess."""
    monkeypatch.setenv("PATH", str(tmp_path))

    assert printers.Cups("anything").probe() is False
    with pytest.raises(Exception) as exc:
        printers.Cups("anything").send(b"^XA^XZ")
    assert "lp" in str(exc.value)


def test_a_cups_printer_round_trips_through_the_api(client, fake_cups):
    fake_cups()
    client.put("/printers/workshop", json={
        "name": "Workshop", "model": "ZD621", "transport": {"kind": "cups", "queue": "zd621"}})

    assert client.get("/printers/workshop").json()["transport"] == {
        "kind": "cups", "queue": "zd621"}
    assert client.post("/printers/workshop/test").status_code == 202
