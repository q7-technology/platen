"""Everything that differs between a laptop, a compose stack and a customer's
server comes from the environment. Read once at import; nothing else reads
os.environ."""

from __future__ import annotations

import os
from dataclasses import dataclass


def _flag(name: str) -> bool | None:
    """None means "work it out from the request"."""
    value = os.environ.get(name, "").strip().lower()
    if not value:
        return None
    return value in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Settings:
    database_url: str
    redis_url: str
    debug: bool
    # A session cookie marked Secure is never sent back over plain http, so
    # forcing it on would lock out a site running Platen on its own LAN. Left
    # alone it follows the scheme of the request. Set it when a proxy
    # terminates TLS and doesn't pass the scheme through.
    secure_cookies: bool | None

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            database_url=os.environ.get(
                "DATABASE_URL", "postgresql+psycopg://platen:platen@localhost:5432/platen"
            ),
            redis_url=os.environ.get("REDIS_URL", "redis://localhost:6379/0"),
            debug=os.environ.get("PLATEN_DEBUG", "").lower() in ("1", "true", "yes"),
            secure_cookies=_flag("PLATEN_SECURE_COOKIES"),
        )


settings = Settings.from_env()
