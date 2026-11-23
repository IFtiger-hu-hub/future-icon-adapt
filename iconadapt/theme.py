"""Enumerate installed icon themes and parse index.theme."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .constants import HICOLOR


ICON_INDEX_NAMES = ("index.theme", "index.desktop")


@dataclass
class ThemeDirSpec:
    rel: str
    size: int = 0
    scale: int = 1
    context: str = ""
    typ: str = "Threshold"
    min_size: int = 0
    max_size: int = 0
    threshold: int = 2


@dataclass
class IconTheme:
    internal_name: str
    path: Path
    display_name: str = ""
    comment: str = ""
    inherits: list[str] = field(default_factory=list)
    directories: list[ThemeDirSpec] = field(default_factory=list)
    hidden: bool = False
    follows_color_scheme: bool = False
    example: str = ""
    valid: bool = True
    error: str = ""
    # Additional directories of the same internal name (theme spread across bases)
    extra_paths: list[Path] = field(default_factory=list)

    def all_paths(self) -> list[Path]:
        return [self.path, *self.extra_paths]


def _parse_bool(val: str) -> bool:
    return val.strip().lower() in {"true", "1", "yes"}


def parse_index_theme(theme_dir: Path) -> IconTheme:
    name = theme_dir.name
    index = None
    for fn in ICON_INDEX_NAMES:
        p = theme_dir / fn
        if p.is_file():
            index = p
            break
    if index is None:
        return IconTheme(internal_name=name, path=theme_dir, valid=False, error="no index.theme")
    section = "Icon Theme" if index.name == "index.theme" else "KDE Icon Theme"
    try:
        text = index.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return IconTheme(internal_name=name, path=theme_dir, valid=False, error=str(exc))

    groups: dict[str, dict[str, str]] = {}
    current = None
    for line in text.splitlines():
        if not line or line.lstrip().startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            current = line[1:-1]
            groups.setdefault(current, {})
            continue
        if current is None or "=" not in line:
            continue
        k, v = line.split("=", 1)
        groups[current][k.strip()] = v.strip()

    main = groups.get(section) or groups.get("Icon Theme") or {}
    inherits_raw = main.get("Inherits", "")
    inherits = [p.strip() for p in inherits_raw.split(",") if p.strip()]
    dir_list = []
    for key in ("Directories", "ScaledDirectories"):
        for part in main.get(key, "").split(","):
            part = part.strip()
            if part:
                dir_list.append(part)
    specs: list[ThemeDirSpec] = []
    seen: set[str] = set()
    for rel in dir_list:
        if rel in seen:
            continue
        seen.add(rel)
        g = groups.get(rel, {})
        size = _int(g.get("Size", "0"))
        scale = _int(g.get("Scale", "1")) or 1
        typ = g.get("Type", "Threshold") or "Threshold"
        specs.append(
            ThemeDirSpec(
                rel=rel,
                size=size,
                scale=scale,
                context=g.get("Context", ""),
                typ=typ,
                min_size=_int(g.get("MinSize", str(size))),
                max_size=_int(g.get("MaxSize", str(size))),
                threshold=_int(g.get("Threshold", "2")) or 2,
            )
        )
    return IconTheme(
        internal_name=name,
        path=theme_dir,
        display_name=main.get("Name", name) or name,
        comment=main.get("Comment", ""),
        inherits=inherits,
        directories=specs,
        hidden=_parse_bool(main.get("Hidden", "")),
        follows_color_scheme=_parse_bool(main.get("FollowsColorScheme", "")),
        example=main.get("Example", ""),
        valid=True,
    )


def _int(val: str) -> int:
    try:
        return int(str(val).strip() or "0")
    except ValueError:
        return 0


def enumerate_themes(search_dirs: list[Path], include_invalid: bool = False) -> dict[str, IconTheme]:
    """Earlier search dirs win for index.theme; later same-name dirs are extra_paths."""
    themes: dict[str, IconTheme] = {}
    for base in search_dirs:
        if not base.is_dir():
            continue
        try:
            entries = list(base.iterdir())
        except OSError:
            continue
        for child in entries:
            if not child.is_dir():
                continue
            # skip cursor-only or no index (unless first empty)
            index_exists = any((child / fn).is_file() for fn in ICON_INDEX_NAMES)
            if not index_exists:
                continue
            theme = parse_index_theme(child)
            if theme.internal_name in themes:
                existing = themes[theme.internal_name]
                if child.resolve() != existing.path.resolve() and child.resolve() not in [
                    p.resolve() for p in existing.extra_paths
                ]:
                    existing.extra_paths.append(child)
                continue
            if theme.valid or include_invalid:
                themes[theme.internal_name] = theme
    return themes


def inherit_chain(
    start: str,
    themes: dict[str, IconTheme],
    *,
    append_hicolor: bool = True,
    append_breeze_if_missing: bool = False,
) -> tuple[list[str], list[str]]:
    """Depth-first parent walk with cycle detection.

    Returns (chain_including_start, warnings).
    hicolor is appended last if requested and not already present.
    append_breeze_if_missing records KDE's typical extra fallback without
    pretending it is listed in Inherits.
    """
    warnings: list[str] = []
    chain: list[str] = []
    visiting: set[str] = set()
    done: set[str] = set()

    def walk(name: str) -> None:
        if name in done:
            return
        if name in visiting:
            warnings.append(f"inheritance cycle involving {name}")
            return
        if name not in themes and name != HICOLOR:
            warnings.append(f"inherited theme not installed: {name}")
            # still record the name so callers see the declared intent
            chain.append(name)
            done.add(name)
            return
        visiting.add(name)
        chain.append(name)
        theme = themes.get(name)
        if theme:
            for parent in theme.inherits:
                walk(parent)
        visiting.remove(name)
        done.add(name)

    walk(start)
    if append_breeze_if_missing:
        if "breeze" in themes and "breeze" not in done:
            # Note only; caller may search it as an extra fallback
            warnings.append("KDE may also fall back to breeze if not in Inherits")
    if append_hicolor and HICOLOR not in chain:
        chain.append(HICOLOR)
    return chain, warnings
