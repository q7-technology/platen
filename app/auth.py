"""Who is at the keyboard, and what they are allowed to do.

Two roles, because that is what a floor looks like. An administrator wires up
connections, printers and templates. An operator answers a question and presses
print, and never needs to see a connection string.

Passwords are hashed with scrypt from the standard library — no new dependency,
and a real memory-hard KDF rather than a bare digest. A session is a random
token in an http-only cookie; the database keeps only its hash, so a stolen
backup cannot be replayed as a login.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import secrets
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from db import session as dbsession
from db.models import ApiToken, AppUser, UserSession

log = logging.getLogger("platen.auth")

COOKIE = "platen_session"
ROLES = ("admin", "operator")
TOKEN_ROLES = ("admin", "operator", "agent")
TOKEN_PREFIX = "plt_"

# About 64 MB and a tenth of a second per hash: nothing to a person signing in
# once a shift, a great deal to someone working through a stolen database.
# maxmem is set explicitly because OpenSSL's default ceiling is below this.
SCRYPT = {"n": 2**16, "r": 8, "p": 1, "dklen": 32, "maxmem": 128 * 1024 * 1024}
MIN_PASSWORD = 12

MAX_FAILURES = 10
LOCKOUT = timedelta(minutes=15)
SESSION_LIFE = timedelta(hours=12)      # a shift, refreshed while it is in use
SESSION_CAP = timedelta(days=7)         # but never longer than this in total


class BadLogin(Exception):
    """Wrong name, wrong password, or an account switched off. Deliberately
    one exception: telling them which was wrong tells them half the answer."""


class LockedOut(Exception):
    """Too many tries."""


def now() -> datetime:
    return datetime.now(UTC)


# ------------------------------------------------------------------ passwords

def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode()


def hash_password(password: str) -> str:
    if len(password) < MIN_PASSWORD:
        raise ValueError(f"a password needs at least {MIN_PASSWORD} characters")
    salt = secrets.token_bytes(16)
    key = hashlib.scrypt(password.encode(), salt=salt, **SCRYPT)
    return f"scrypt${SCRYPT['n']}${SCRYPT['r']}${SCRYPT['p']}${_b64(salt)}${_b64(key)}"


def verify_password(stored: str, password: str) -> bool:
    try:
        kind, n, r, p, salt, want = stored.split("$")
        if kind != "scrypt":
            return False
        expected = base64.b64decode(want)
        got = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt),
                             n=int(n), r=int(r), p=int(p), dklen=len(expected),
                             maxmem=SCRYPT["maxmem"])
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(got, expected)


_DUMMY: str | None = None


def _dummy_hash() -> str:
    """Something to verify against when the username doesn't exist, so a
    missing account and a wrong password take the same time to refuse."""
    global _DUMMY
    if _DUMMY is None:
        _DUMMY = hash_password("x" * MIN_PASSWORD)
    return _DUMMY


# ---------------------------------------------------------------------- users

def create_user(session: Session, username: str, password: str, *,
                role: str = "operator", display_name: str = "") -> AppUser:
    username = username.strip().lower()
    if not username:
        raise ValueError("a user needs a name")
    if role not in ROLES:
        raise ValueError(f"role must be one of {', '.join(ROLES)}")
    user = AppUser(username=username, display_name=display_name or username,
                   role=role, password=hash_password(password))
    session.add(user)
    session.commit()
    return user


def set_password(session: Session, user: AppUser, password: str) -> None:
    """Changing a password ends every session that was opened with the old
    one. A reset you do because a password leaked is no use if the session
    somebody already has keeps working."""
    user.password = hash_password(password)
    user.failed_logins, user.locked_until = 0, None
    session.commit()
    end_all_sessions(session, user.username)


def login(session: Session, username: str, password: str) -> AppUser:
    user = session.get(AppUser, (username or "").strip().lower())
    if user is not None and user.locked_until and user.locked_until > now():
        raise LockedOut(
            "too many attempts; this account is locked for "
            f"{round((user.locked_until - now()).total_seconds() / 60)} more minutes"
        )

    ok = verify_password(user.password if user else _dummy_hash(), password)
    if user is None or user.disabled or not ok:
        if user is not None:
            user.failed_logins += 1
            if user.failed_logins >= MAX_FAILURES:
                user.locked_until = now() + LOCKOUT
            session.commit()
        raise BadLogin("that username or password isn't right")

    user.failed_logins, user.locked_until, user.last_login_at = 0, None, now()
    session.commit()
    return user


# ------------------------------------------------------------------- sessions

def token_id(token: str) -> str:
    # the token is 256 bits of randomness, so a fast digest is enough here:
    # there is nothing to brute-force back
    return hashlib.sha256(token.encode()).hexdigest()


def start_session(session: Session, user: AppUser) -> str:
    token = secrets.token_urlsafe(32)
    session.add(UserSession(id=token_id(token), username=user.username,
                            created_at=now(), expires_at=now() + SESSION_LIFE,
                            last_seen_at=now()))
    session.commit()
    return token


def user_for_token(session: Session, token: str) -> AppUser | None:
    row = session.get(UserSession, token_id(token))
    if row is None:
        return None
    if row.expires_at <= now() or row.created_at + SESSION_CAP <= now():
        session.delete(row)
        session.commit()
        return None
    user = session.get(AppUser, row.username)
    if user is None or user.disabled:
        return None
    row.last_seen_at = now()
    row.expires_at = min(now() + SESSION_LIFE, row.created_at + SESSION_CAP)
    session.commit()
    return user


def end_session(session: Session, token: str) -> None:
    row = session.get(UserSession, token_id(token))
    if row is not None:
        session.delete(row)
        session.commit()


def end_all_sessions(session: Session, username: str) -> None:
    for row in session.scalars(select(UserSession).where(UserSession.username == username)):
        session.delete(row)
    session.commit()


# --------------------------------------------------------------- machine keys

def create_token(session: Session, *, name: str, role: str,
                 agent_id: str | None = None, created_by: str = "") -> str:
    """Returns the token. It is never recoverable afterwards — only its hash
    is kept, for the same reason a session's is."""
    if role not in TOKEN_ROLES:
        raise ValueError(f"role must be one of {', '.join(TOKEN_ROLES)}")
    token = TOKEN_PREFIX + secrets.token_urlsafe(32)
    session.add(ApiToken(id=token_id(token), name=name, role=role,
                         agent_id=agent_id, created_by=created_by))
    session.commit()
    return token


def token_principal(session: Session, token: str) -> ApiToken | None:
    row = session.get(ApiToken, token_id(token or ""))
    if row is None:
        return None
    row.last_used_at = now()
    session.commit()
    return row


def revoke_tokens(session: Session, *, agent_id: str | None = None,
                  name: str | None = None) -> int:
    stmt = select(ApiToken)
    if agent_id is not None:
        stmt = stmt.where(ApiToken.agent_id == agent_id)
    if name is not None:
        stmt = stmt.where(ApiToken.name == name)
    rows = list(session.scalars(stmt))
    for row in rows:
        session.delete(row)
    session.commit()
    return len(rows)


# ------------------------------------------------------------------ first run

def bootstrap() -> None:
    """Make the first administrator, once, from the environment.

    With no users and nothing in the environment, nobody can sign in — which
    is the safe way round. The log says how to fix it.
    """
    dbsession.engine()
    with dbsession.SessionLocal() as s:
        if s.scalar(select(func.count()).select_from(AppUser)):
            return
        username = os.environ.get("PLATEN_ADMIN_USERNAME", "").strip()
        password = os.environ.get("PLATEN_ADMIN_PASSWORD", "")
        if not (username and password):
            log.warning(
                "Platen has no users, so nobody can sign in. Set "
                "PLATEN_ADMIN_USERNAME and PLATEN_ADMIN_PASSWORD and restart, "
                "or run: python -m app.adduser <name> --role admin"
            )
            return
        try:
            create_user(s, username, password, role="admin")
        except ValueError as exc:
            log.error("could not make the first administrator: %s", exc)
            return
        log.info("made the first administrator %r from the environment", username)
