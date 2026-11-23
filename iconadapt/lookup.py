"""Freedesktop-style icon lookup with explicit fallback classification."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .constants import HICOLOR
from .theme import IconTheme, ThemeDirSpec, inherit_chain

EXTENSIONS = (".png", ".svg", ".svgz", ".xpm")

# (theme_path, icon_name, preferred_size) -> Candidate | None
_LOOKUP_CACHE: dict[tuple[str, str, int], Candidate | None] = {}


@dataclass
class LookupHit:
    path: Path
    theme: str
    rel_dir: str
    size: int
    in_theme_proper: bool
    via_inherit: bool
    via_extra_fallback: bool = False  # e.g. KDE breeze not in declared Inherits


@dataclass
class LookupResult:
    icon: str
    found: bool
    hit: LookupHit | None = None
    chain: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def status(self) -> str:
        if not self.found:
            return "missing"
        if self.hit and self.hit.in_theme_proper:
            return "in_current"
        if self.hit and self.hit.via_extra_fallback:
            return "extra_fallback"
        return "fallback"


@dataclass
class Candidate:
    theme: str
    path: Path
    rel_dir: str
    size: int
    display_name: str = ""
    typ: str = ""


def _iter_theme_files(theme: IconTheme, spec: ThemeDirSpec, stem: str) -> list[Path]:
    found: list[Path] = []
    for base in theme.all_paths():
        folder = base / spec.rel
        if not folder.is_dir():
            continue
        for ext in EXTENSIONS:
            p = folder / f"{stem}{ext}"
            if p.is_file() or p.is_symlink():
                # broken symlink: still record if exists() is false? is_file follows symlink
                if p.exists():
                    found.append(p)
    return found


def lookup_in_theme_files(
    icon_name: str,
    theme: IconTheme,
    *,
    preferred_size: int = 48,
) -> Candidate | None:
    """Search only this theme's own files, not Inherits."""
    if not theme.valid or not icon_name:
        return None
    cache_key = (str(theme.path), icon_name, preferred_size)
    if cache_key in _LOOKUP_CACHE:
        return _LOOKUP_CACHE[cache_key]
    exact: list[Candidate] = []
    any_hit: list[Candidate] = []
    # Prefer Applications context / apps path; fall back to the rest of the index.
    apps_specs = [s for s in theme.directories if _looks_like_apps(s.rel) or s.context.lower() in {"applications", "apps"}]
    other_specs = [s for s in theme.directories if s not in apps_specs]
    for spec in (*apps_specs, *other_specs):
        # After an exact-size apps hit, we can stop: later dirs cannot be better.
        if exact and apps_specs and spec in other_specs:
            break
        for path in _iter_theme_files(theme, spec, icon_name):
            cand = Candidate(
                theme=theme.internal_name,
                path=path,
                rel_dir=spec.rel,
                size=spec.size,
                display_name=theme.display_name,
                typ=spec.typ or "",
            )
            any_hit.append(cand)
            if _dir_matches(spec, preferred_size):
                exact.append(cand)
    if exact:
        hit = exact[0]
    elif any_hit:
        # closest size
        any_hit.sort(key=lambda c: abs((c.size or preferred_size) - preferred_size))
        hit = any_hit[0]
    elif theme.directories:
        hit = None
    else:
        # Only scan the tree when the theme index lists no usable directories.
        hit = _scan_conventional(theme, icon_name, preferred_size)
    _LOOKUP_CACHE[cache_key] = hit
    return hit


def _dir_matches(spec: ThemeDirSpec, size: int) -> bool:
    typ = (spec.typ or "Threshold").lower()
    scale = spec.scale or 1
    if typ == "fixed":
        return spec.size * scale == size
    if typ == "scalable":
        lo = spec.min_size or 1
        hi = spec.max_size or max(spec.size, size)
        return lo <= size <= hi
    # Threshold
    thr = spec.threshold or 2
    return (spec.size - thr) <= size <= (spec.size + thr)


def _scan_conventional(theme: IconTheme, icon_name: str, preferred_size: int) -> Candidate | None:
    hits: list[Candidate] = []
    for base in theme.all_paths():
        if not base.is_dir():
            continue
        for folder in base.rglob("*"):
            if not folder.is_dir():
                continue
            # only apps-like, skip actions/status/mimetypes unless no index at all
            rel = str(folder.relative_to(base)).replace("\\", "/")
            if "/apps" not in ("/" + rel) and not rel.endswith("apps"):
                continue
            for ext in EXTENSIONS:
                p = folder / f"{icon_name}{ext}"
                if p.is_file() and p.exists():
                    hits.append(
                        Candidate(
                            theme=theme.internal_name,
                            path=p,
                            rel_dir=rel,
                            size=_guess_size(rel, preferred_size),
                            display_name=theme.display_name,
                        )
                    )
    if not hits:
        return None
    hits.sort(key=lambda c: abs(c.size - preferred_size))
    return hits[0]


def _guess_size(rel: str, default: int) -> int:
    head = rel.split("/", 1)[0]
    if "x" in head:
        try:
            return int(head.split("x", 1)[0])
        except ValueError:
            return default
    if head.isdigit():
        return int(head)
    if head == "scalable":
        return default
    return default


def lookup_icon(
    icon_name: str,
    start_theme: str,
    themes: dict[str, IconTheme],
    *,
    preferred_size: int = 48,
    append_hicolor: bool = True,
    kde_breeze_extra: bool = True,
) -> LookupResult:
    if not icon_name:
        return LookupResult(icon=icon_name, found=False, warnings=["empty icon name"])
    chain, warnings = inherit_chain(
        start_theme,
        themes,
        append_hicolor=append_hicolor,
        append_breeze_if_missing=False,
    )
    # Walk declared chain; first theme in chain is the current theme proper.
    for idx, name in enumerate(chain):
        theme = themes.get(name)
        if not theme:
            continue
        cand = lookup_in_theme_files(icon_name, theme, preferred_size=preferred_size)
        if cand:
            proper = idx == 0 and name == start_theme
            hit = LookupHit(
                path=cand.path,
                theme=name,
                rel_dir=cand.rel_dir,
                size=cand.size,
                in_theme_proper=proper,
                via_inherit=not proper,
            )
            return LookupResult(icon=icon_name, found=True, hit=hit, chain=chain, warnings=warnings)
    # Extra KDE breeze fallback if breeze is installed and not already searched
    if kde_breeze_extra and "breeze" in themes and "breeze" not in chain:
        warnings = [*warnings, "searched extra KDE breeze fallback (not in Inherits)"]
        cand = lookup_in_theme_files(icon_name, themes["breeze"], preferred_size=preferred_size)
        if cand:
            hit = LookupHit(
                path=cand.path,
                theme="breeze",
                rel_dir=cand.rel_dir,
                size=cand.size,
                in_theme_proper=False,
                via_inherit=False,
                via_extra_fallback=True,
            )
            return LookupResult(icon=icon_name, found=True, hit=hit, chain=chain, warnings=warnings)
    return LookupResult(icon=icon_name, found=False, chain=chain, warnings=warnings)


def collect_exact_candidates(
    icon_name: str,
    themes: dict[str, IconTheme],
    *,
    preferred_size: int = 48,
    apps_only: bool = True,
) -> list[Candidate]:
    """Exact filename stem in each theme's own files. One best hit per theme."""
    out: list[Candidate] = []
    for theme in themes.values():
        if not theme.valid:
            continue
        cand = lookup_in_theme_files(icon_name, theme, preferred_size=preferred_size)
        if not cand:
            continue
        if apps_only and not _looks_like_apps(cand.rel_dir):
            # still allow if that's all the theme has for this name
            pass
        out.append(cand)
    out.sort(key=lambda c: c.theme.lower())
    return out


def collect_all_sizes(icon_name: str, theme: IconTheme) -> list[Candidate]:
    hits: list[Candidate] = []
    seen: set[str] = set()
    for spec in theme.directories:
        if not _looks_like_apps(spec.rel):
            continue
        for path in _iter_theme_files(theme, spec, icon_name):
            key = str(path)
            if key in seen:
                continue
            seen.add(key)
            hits.append(
                Candidate(
                    theme=theme.internal_name,
                    path=path,
                    rel_dir=spec.rel,
                    size=spec.size,
                    display_name=theme.display_name,
                    typ=spec.typ or "",
                )
            )
    if not hits and not theme.directories:
        # conventional apps scan only when the index lists no directories
        for base in theme.all_paths():
            if not base.is_dir():
                continue
            for folder in base.rglob("*"):
                if not folder.is_dir():
                    continue
                rel = str(folder.relative_to(base)).replace("\\", "/")
                if not _looks_like_apps(rel):
                    continue
                for ext in EXTENSIONS:
                    p = folder / f"{icon_name}{ext}"
                    if p.is_file() and p.exists():
                        key = str(p)
                        if key in seen:
                            continue
                        seen.add(key)
                        hits.append(
                            Candidate(
                                theme=theme.internal_name,
                                path=p,
                                rel_dir=rel,
                                size=_guess_size(rel, 48),
                                display_name=theme.display_name,
                            )
                        )
    return hits


def _looks_like_apps(rel: str) -> bool:
    parts = rel.replace("\\", "/").lower().split("/")
    return "apps" in parts or "applications" in parts
