"""Which sites somebody looks after, and so which printers and runs they see.

An administrator sees every site. An operator looks after at most one and a
manager any number. A printer in no site is shared and everyone sees it, so an
install that has never made a site works exactly as it did before sites
existed.

A key isn't a person. An operator key made with sites keeps to them, like an
operator; one made without any sees every site, which is what a key could do
before keys had sites. Scripts written then would otherwise quietly stop
finding their printers.
"""

from __future__ import annotations

from sqlalchemy import ColumnElement, or_, select
from sqlalchemy.orm import Session

from db.models import AppUser, Printer, Site, UserSite

ALL: None = None         # what `allowed` returns for someone who sees everything


class NotYours(Exception):
    """The printer or run is in a site this person doesn't look after."""


def key_sites(user: AppUser) -> list[str] | None:
    """A key's own sites, carried on the stand-in user current_user makes."""
    return getattr(user, "key_sites", None) if user.username.startswith("key:") else None


def sees_everything(user: AppUser) -> bool:
    if user.role == "admin":
        return True
    return user.username.startswith("key:") and not key_sites(user)


def assigned(s: Session, username: str) -> list[str]:
    return list(s.scalars(
        select(UserSite.site_id).where(UserSite.username == username)
        .order_by(UserSite.site_id)))


def allowed(s: Session, user: AppUser) -> set[str] | None:
    """The site ids this person looks after, or ALL."""
    if sees_everything(user):
        return ALL
    if user.username.startswith("key:"):
        return set(key_sites(user) or [])
    return set(assigned(s, user.username))


def printer_filter(s: Session, user: AppUser) -> ColumnElement[bool] | None:
    """A where-clause on Printer for what this person may see; None for all."""
    sites = allowed(s, user)
    if sites is ALL:
        return None
    return or_(Printer.site_id.is_(None), Printer.site_id.in_(sites))


def may_use(s: Session, user: AppUser, printer: Printer) -> bool:
    sites = allowed(s, user)
    return sites is ALL or printer.site_id is None or printer.site_id in sites


def check(s: Session, user: AppUser, printer: Printer) -> None:
    if not may_use(s, user, printer):
        site = s.get(Site, printer.site_id) if printer.site_id else None
        raise NotYours(
            f"{printer.name} is at {site.name if site else printer.site_id}, "
            "which isn't one of your sites; ask a manager or an administrator")


def assign(s: Session, username: str, role: str, site_ids: list[str]) -> list[str]:
    """Replace the sites a person looks after. Doesn't commit."""
    wanted = sorted(set(site_ids))
    if role == "admin":
        wanted = []                      # they see everything; a list would only mislead
    if role == "operator" and len(wanted) > 1:
        raise ValueError("an operator works at one site; make them a manager "
                         "to look after more than one")
    missing = [i for i in wanted if s.get(Site, i) is None]
    if missing:
        raise ValueError(f"no site {', '.join(repr(m) for m in missing)}")
    archived = [i for i in wanted if i not in assigned(s, username)
                and s.get(Site, i).archived_at is not None]
    if archived:
        raise ValueError(f"{', '.join(archived)} is archived; restore it first")
    for row in s.scalars(select(UserSite).where(UserSite.username == username)):
        s.delete(row)
    s.flush()
    for site_id in wanted:
        s.add(UserSite(username=username, site_id=site_id))
    return wanted
