"""Everything that differs between a laptop, a compose stack and a customer's
server comes from the environment. Read once at import; nothing else reads
os.environ."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    database_url: str
    redis_url: str
    debug: bool

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            database_url=os.environ.get(
                "DATABASE_URL", "postgresql+psycopg://platen:platen@localhost:5432/platen"
            ),
            redis_url=os.environ.get("REDIS_URL", "redis://localhost:6379/0"),
            debug=os.environ.get("PLATEN_DEBUG", "").lower() in ("1", "true", "yes"),
        )


settings = Settings.from_env()
