"""Delete what has aged out, from the command line.

    python -m app.prune            # apply the retention settings
    python -m app.prune --dry-run  # say what would go, and change nothing
"""

from __future__ import annotations

import argparse
import logging
import sys

from db import session as dbsession

from . import retention


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.prune")
    parser.add_argument("--dry-run", action="store_true",
                        help="say what would go without deleting anything")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    dbsession.engine()
    with dbsession.SessionLocal() as s:
        keep = retention.settings(s)
        print("keeping:", ", ".join(f"{k.replace('_days', '')} {v} days" if v else
                                    f"{k.replace('_days', '')} forever"
                                    for k, v in keep.items()))
        if args.dry_run:
            counts = retention.would_remove(s)
            print("would remove:", _describe(counts))
            return 0
        print("removed:", _describe(retention.prune(s)))
    return 0


def _describe(counts: dict[str, int]) -> str:
    listed = ", ".join(f"{n} {k}" for k, n in counts.items() if n)
    return listed or "nothing"


if __name__ == "__main__":
    sys.exit(main())
