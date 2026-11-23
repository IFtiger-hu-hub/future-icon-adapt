"""Instant, case-insensitive app search. Combines with status filters."""

from __future__ import annotations

from typing import Any, Iterable


def normalize_query(query: str | None) -> str:
    return (query or "").casefold().strip()


def _haystack(*parts: str | None) -> str:
    return " ".join(p for p in parts if p)


def matches_query(
    query: str | None,
    *,
    name: str = "",
    desktop_id: str = "",
    icon: str = "",
    generic_name: str = "",
    comment: str = "",
) -> bool:
    """Substring match against name, desktop id, Icon=, GenericName, Comment.

    English is case-insensitive via casefold; CJK characters are compared as-is.
    An empty query matches everything.
    """
    q = normalize_query(query)
    if not q:
        return True
    text = _haystack(name, desktop_id, icon, generic_name, comment).casefold()
    return q in text


def matches_app(app: Any, query: str | None) -> bool:
    return matches_query(
        query,
        name=getattr(app, "name", "") or getattr(app, "display_name", "") or "",
        desktop_id=getattr(app, "desktop_id", "") or "",
        icon=getattr(app, "icon", "") or "",
        generic_name=getattr(app, "generic_name", "") or "",
        comment=getattr(app, "comment", "") or "",
    )


def matches_report(row: Any, query: str | None) -> bool:
    if isinstance(row, dict):
        return matches_query(
            query,
            name=row.get("name", "") or "",
            desktop_id=row.get("desktop_id", "") or "",
            icon=row.get("icon", "") or "",
            generic_name=row.get("generic_name", "") or "",
            comment=row.get("comment", "") or "",
        )
    return matches_query(
        query,
        name=getattr(row, "name", "") or "",
        desktop_id=getattr(row, "desktop_id", "") or "",
        icon=getattr(row, "icon", "") or "",
        generic_name=getattr(row, "generic_name", "") or "",
        comment=getattr(row, "comment", "") or "",
    )


def filter_items(items: Iterable[Any], query: str | None, matcher=matches_app) -> list:
    q = normalize_query(query)
    items = list(items)
    if not q:
        return items
    return [it for it in items if matcher(it, query)]
