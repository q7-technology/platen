"""One place that decides what Platen says about itself.

The API and the worker are separate processes and a site will want both going
to the same place, so the setup lives here and both pick it up on import.

Nothing in here ever writes a password, a token or a connection string. A log
somebody is comfortable shipping off the box is worth more than one with
everything in it.
"""

from __future__ import annotations

import logging
import os
import sys

FORMAT = "%(asctime)s %(levelname)-7s %(name)s %(message)s"
LEVELS = {"debug": logging.DEBUG, "info": logging.INFO, "warning": logging.WARNING,
          "error": logging.ERROR, "critical": logging.CRITICAL}

_done = False


def level_from_env() -> int:
    return LEVELS.get(os.environ.get("PLATEN_LOG_LEVEL", "").strip().lower(), logging.INFO)


def setup() -> None:
    """Idempotent: the worker, the API and the CLIs all call it."""
    global _done
    if _done:
        return
    _done = True
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter(FORMAT))
    platen = logging.getLogger("platen")
    platen.setLevel(level_from_env())
    platen.addHandler(handler)
    platen.propagate = False


def where(**fields: object) -> str:
    """key=value for the things worth grepping for, in a fixed order."""
    return " ".join(f"{k}={v}" for k, v in fields.items() if v is not None)
