"""Build per-app icon coverage reports. Absolute paths are not 'missing'."""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from pathlib import Path

from .desktop import DesktopApp, expand_icon_path, is_absolute_icon
from .hints import collect_hint_candidates
from .lookup import LookupResult, collect_exact_candidates, lookup_icon
from .theme import IconTheme


@dataclass
class AppIconReport:
    desktop_id: str
    name: str
    desktop_path: str
    icon: str
    icon_kind: str
    status: str
    # status: ok | fallback | extra_fallback | missing | absolute_ok | absolute_missing | empty
    lookup_theme: str = ""
    lookup_path: str = ""
    in_current: bool = False
    chain: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    hints: list[dict] = field(default_factory=list)
    candidates: list[dict] = field(default_factory=list)
    selection: dict | None = None
    generic_name: str = ""
    comment: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def classify_app(
    app: DesktopApp,
    current_theme: str,
    themes: dict[str, IconTheme],
    *,
    preferred_size: int = 48,
    include_candidates: bool = False,
    include_hints: bool = True,
) -> AppIconReport:
    icon = app.icon
    base = AppIconReport(
        desktop_id=app.desktop_id,
        name=app.display_name,
        desktop_path=str(app.path),
        icon=icon,
        icon_kind=app.icon_kind,
        status="empty",
        generic_name=app.generic_name or "",
        comment=getattr(app, "comment", "") or "",
    )
    if not icon:
        base.status = "empty"
        if include_hints:
            base.hints = []
        return base
    if is_absolute_icon(icon):
        path = expand_icon_path(icon)
        exists = path.is_file() or (path.exists() and not path.is_dir())
        base.lookup_path = str(path)
        base.status = "absolute_ok" if exists else "absolute_missing"
        # Absolute path is not a theme miss. Still offer exact-name candidates
        # only if the last path stem might be a theme name — as hints, not status.
        stem = path.stem
        if include_candidates and stem:
            base.candidates = [
                {"theme": c.theme, "path": str(c.path), "size": c.size, "rel_dir": c.rel_dir}
                for c in collect_exact_candidates(stem, themes, preferred_size=preferred_size)
            ]
        # Name-mismatch hints are useful when the file is missing; skip on existing paths unless asked.
        if include_hints and stem and (base.status == "absolute_missing" or include_candidates):
            base.hints = collect_hint_candidates(stem, themes, preferred_size=preferred_size)
        return base

    result: LookupResult = lookup_icon(
        icon, current_theme, themes, preferred_size=preferred_size
    )
    base.chain = result.chain
    base.warnings = result.warnings
    if result.hit:
        base.lookup_theme = result.hit.theme
        base.lookup_path = str(result.hit.path)
        base.in_current = result.hit.in_theme_proper
        if result.hit.in_theme_proper:
            base.status = "ok"
        elif result.hit.via_extra_fallback:
            base.status = "extra_fallback"
        else:
            base.status = "fallback"
    else:
        base.status = "missing"

    if include_candidates:
        base.candidates = [
            {"theme": c.theme, "path": str(c.path), "size": c.size, "rel_dir": c.rel_dir}
            for c in collect_exact_candidates(icon, themes, preferred_size=preferred_size)
        ]
    # Alias/case hints only when the exact Icon= name is missing from the chain.
    # Do not scan every ok/fallback row (that made live reports tens of seconds).
    if include_hints and base.status in {"missing"}:
        base.hints = collect_hint_candidates(icon, themes, preferred_size=preferred_size)
    return base


def report_apps(
    apps: list[DesktopApp],
    current_theme: str,
    themes: dict[str, IconTheme],
    **kwargs,
) -> list[AppIconReport]:
    return [classify_app(a, current_theme, themes, **kwargs) for a in apps]


def summarize(rows: list[AppIconReport]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for r in rows:
        counts[r.status] = counts.get(r.status, 0) + 1
    counts["total"] = len(rows)
    return counts
