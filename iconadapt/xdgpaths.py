"""XDG and tool-owned paths. Never writes by itself."""

from __future__ import annotations

import os
from pathlib import Path

from .constants import STATE_DIRNAME


def home() -> Path:
    return Path(os.path.expanduser("~"))


def xdg_config_home() -> Path:
    raw = os.environ.get("XDG_CONFIG_HOME")
    return Path(raw) if raw else home() / ".config"


def xdg_data_home() -> Path:
    raw = os.environ.get("XDG_DATA_HOME")
    return Path(raw) if raw else home() / ".local" / "share"


def xdg_data_dirs() -> list[Path]:
    raw = os.environ.get("XDG_DATA_DIRS", "/usr/local/share:/usr/share")
    dirs: list[Path] = []
    for part in raw.split(":"):
        part = part.strip()
        if part:
            dirs.append(Path(part))
    return dirs


def applications_dirs(extra: list[Path] | None = None) -> list[Path]:
    """Search order: user data home first, then XDG_DATA_DIRS."""
    ordered: list[Path] = []
    seen: set[Path] = set()
    for base in [xdg_data_home(), *xdg_data_dirs(), *(extra or [])]:
        p = (base / "applications").resolve() if base.exists() else base / "applications"
        key = Path(os.path.normpath(str(base / "applications")))
        if key in seen:
            continue
        seen.add(key)
        ordered.append(base / "applications")
    return ordered


def icon_search_dirs(extra: list[Path] | None = None) -> list[Path]:
    """Theme search bases, user first. Includes ~/.icons for compatibility."""
    ordered: list[Path] = []
    seen: set[str] = set()

    def add(p: Path) -> None:
        s = str(p)
        if s in seen:
            return
        seen.add(s)
        ordered.append(p)

    add(xdg_data_home() / "icons")
    add(home() / ".icons")
    for d in xdg_data_dirs():
        add(d / "icons")
    for p in extra or []:
        add(p)
    return ordered


def current_desktop() -> str:
    return os.environ.get("XDG_CURRENT_DESKTOP", "") or "KDE"


def _parse_icons_theme(cfg: Path) -> str | None:
    if not cfg.is_file():
        return None
    in_icons = False
    try:
        text = cfg.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("[") and s.endswith("]"):
            in_icons = s.lower() == "[icons]"
            continue
        if in_icons and s.lower().startswith("theme="):
            return s.split("=", 1)[1].strip().strip('"') or None
    return None


def kde_icon_theme_name(config_home: Path | None = None) -> str | None:
    """Read the configured Plasma icon theme. Does not change any config.

    Plasma 6 may keep the default in ~/.config/kdedefaults/kdeglobals
    when ~/.config/kdeglobals has no [Icons] Theme key. User kdeglobals
    still wins if it sets one. Isolated tests pass config_home and never
    consult the real system or kreadconfig6.
    """
    explicit = config_home is not None
    cfg_home = config_home or xdg_config_home()
    candidates = [
        cfg_home / "kdeglobals",
        cfg_home / "kdedefaults" / "kdeglobals",
    ]
    if not explicit:
        for part in os.environ.get("XDG_CONFIG_DIRS", "/etc/xdg").split(":"):
            part = part.strip()
            if part:
                candidates.append(Path(part) / "kdeglobals")
    for cfg in candidates:
        name = _parse_icons_theme(cfg)
        if name:
            return name
    return None


def tool_state_dir(data_home: Path | None = None) -> Path:
    return (data_home or xdg_data_home()) / STATE_DIRNAME


def tool_config_dir(config_home: Path | None = None) -> Path:
    return (config_home or xdg_config_home()) / STATE_DIRNAME


def user_icons_dir(data_home: Path | None = None) -> Path:
    return (data_home or xdg_data_home()) / "icons"


def user_applications_dir(data_home: Path | None = None) -> Path:
    return (data_home or xdg_data_home()) / "applications"
