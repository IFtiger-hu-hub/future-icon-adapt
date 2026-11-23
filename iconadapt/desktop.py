"""Scan and parse XDG .desktop files. User entries override system by desktop id."""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class DesktopApp:
    desktop_id: str
    path: Path
    name: str
    generic_name: str = ""
    comment: str = ""
    icon: str = ""
    icon_kind: str = "name"  # "name" | "absolute"
    exec_cmd: str = ""
    try_exec: str = ""
    typ: str = ""
    hidden: bool = False
    no_display: bool = False
    only_show_in: list[str] = field(default_factory=list)
    not_show_in: list[str] = field(default_factory=list)
    data_dir: str = ""
    launchable: bool = False
    skip_reason: str = ""

    @property
    def display_name(self) -> str:
        return self.name or self.desktop_id


def desktop_id_from(apps_root: Path, file_path: Path) -> str:
    rel = file_path.resolve().relative_to(apps_root.resolve()) if _is_relative(apps_root, file_path) else Path(file_path.name)
    s = rel.as_posix()
    if s.endswith(".desktop"):
        s = s[: -len(".desktop")]
    return s.replace("/", "-")


def _is_relative(root: Path, file_path: Path) -> bool:
    try:
        file_path.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


def is_absolute_icon(icon: str) -> bool:
    if not icon:
        return False
    if icon.startswith("/") or icon.startswith("~/") or icon.startswith("file:"):
        return True
    # Windows-style or obviously a path with a suffix and a separator
    if "/" in icon or icon.startswith("\\"):
        return True
    return False


def expand_icon_path(icon: str) -> Path:
    if icon.startswith("file:"):
        icon = icon[5:]
        if icon.startswith("//"):
            icon = icon[1:]  # file://path -> /path after one slash remains... handle file:///
            # file:///usr/x -> /usr/x; file://localhost/usr -> skip
        if icon.startswith("///"):
            icon = icon[2:]
        elif icon.startswith("//"):
            # file://host/path — treat remainder after host poorly; best effort
            parts = icon[2:].split("/", 1)
            icon = "/" + parts[1] if len(parts) == 2 else icon
    return Path(os.path.expanduser(icon))


def _truthy(val: str) -> bool:
    return val.strip().lower() in {"true", "1", "yes"}


def _list_field(val: str) -> list[str]:
    return [p for p in val.replace(";", ",").split(",") if p]


def parse_desktop_file(path: Path, preferred_locales: list[str] | None = None) -> dict[str, str]:
    """Parse the first [Desktop Entry] group into a raw key/value dict."""
    locales = preferred_locales or _default_locales()
    raw: dict[str, str] = {}
    in_entry = False
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return {"__error__": str(exc)}
    for line in text.splitlines():
        if not line or line.lstrip().startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip()
            if section == "Desktop Entry":
                in_entry = True
            elif in_entry:
                break
            else:
                in_entry = False
            continue
        if not in_entry or "=" not in line:
            continue
        key, val = line.split("=", 1)
        raw[key.strip()] = val
    # resolve localized Name
    name = _localized(raw, "Name", locales)
    if name:
        raw["Name"] = name
    gname = _localized(raw, "GenericName", locales)
    if gname:
        raw["GenericName"] = gname
    comment = _localized(raw, "Comment", locales)
    if comment:
        raw["Comment"] = comment
    return raw


def _default_locales() -> list[str]:
    out: list[str] = []
    for var in ("LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG"):
        v = os.environ.get(var, "")
        if not v:
            continue
        for part in v.split(":"):
            part = part.strip()
            if part and part not in out and part != "C":
                out.append(part)
                if "." in part:
                    noenc = part.split(".", 1)[0]
                    if noenc not in out:
                        out.append(noenc)
                if "_" in part.split(".", 1)[0]:
                    lang = part.split(".", 1)[0].split("_", 1)[0]
                    if lang not in out:
                        out.append(lang)
    if "zh_CN" not in out and "zh" in "".join(out):
        pass
    return out


def _localized(raw: dict[str, str], key: str, locales: list[str]) -> str:
    for loc in locales:
        k = f"{key}[{loc}]"
        if k in raw and raw[k].strip():
            return raw[k]
        # LANGUAGE=zh_CN.UTF-8 already split; also try short
    return raw.get(key, "")


def _desktops_match(current: str, only: list[str], not_in: list[str]) -> bool:
    tokens = {p.strip() for p in current.replace(":", ";").split(";") if p.strip()}
    tokens_up = {t.lower() for t in tokens}
    if only:
        if not any(item.strip().lower() in tokens_up for item in only):
            return False
    if not_in:
        if any(item.strip().lower() in tokens_up for item in not_in):
            return False
    return True


def _try_exec_ok(try_exec: str) -> bool:
    if not try_exec:
        return True
    p = Path(os.path.expanduser(try_exec))
    if p.is_file() and os.access(p, os.X_OK):
        return True
    return shutil.which(try_exec) is not None


def app_from_raw(path: Path, apps_root: Path, raw: dict[str, str], current_desktop: str) -> DesktopApp:
    did = desktop_id_from(apps_root, path)
    icon = raw.get("Icon", "").strip()
    kind = "absolute" if is_absolute_icon(icon) else "name"
    typ = raw.get("Type", "").strip()
    hidden = _truthy(raw.get("Hidden", ""))
    no_display = _truthy(raw.get("NoDisplay", ""))
    only = _list_field(raw.get("OnlyShowIn", ""))
    not_in = _list_field(raw.get("NotShowIn", ""))
    try_exec = raw.get("TryExec", "").strip()
    skip = ""
    launchable = True
    if typ and typ != "Application":
        launchable, skip = False, f"Type={typ or 'empty'}"
    elif typ != "Application":
        launchable, skip = False, "Type missing"
    elif hidden:
        launchable, skip = False, "Hidden=true"
    elif no_display:
        launchable, skip = False, "NoDisplay=true"
    elif not _desktops_match(current_desktop, only, not_in):
        launchable, skip = False, "filtered by OnlyShowIn/NotShowIn"
    elif try_exec and not _try_exec_ok(try_exec):
        launchable, skip = False, "TryExec not found"
    return DesktopApp(
        desktop_id=did,
        path=path,
        name=raw.get("Name", "").strip() or did,
        generic_name=raw.get("GenericName", "").strip(),
        comment=raw.get("Comment", "").strip(),
        icon=icon,
        icon_kind=kind,
        exec_cmd=raw.get("Exec", "").strip(),
        try_exec=try_exec,
        typ=typ,
        hidden=hidden,
        no_display=no_display,
        only_show_in=only,
        not_show_in=not_in,
        data_dir=str(apps_root),
        launchable=launchable,
        skip_reason=skip,
    )


def iter_desktop_files(apps_root: Path) -> list[Path]:
    if not apps_root.is_dir():
        return []
    out: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(apps_root):
        # skip hidden dirs
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for fn in filenames:
            if fn.endswith(".desktop") and not fn.startswith("."):
                out.append(Path(dirpath) / fn)
    return sorted(out)


def scan_applications(
    app_dirs: list[Path],
    current_desktop: str = "KDE",
    include_non_launchable: bool = False,
) -> list[DesktopApp]:
    """Deduplicate by desktop id. Earlier directories win (user first)."""
    chosen: dict[str, DesktopApp] = {}
    for apps_root in app_dirs:
        for fp in iter_desktop_files(apps_root):
            raw = parse_desktop_file(fp)
            if "__error__" in raw and len(raw) == 1:
                continue
            app = app_from_raw(fp, apps_root, raw, current_desktop)
            if app.desktop_id in chosen:
                continue
            chosen[app.desktop_id] = app
    apps = list(chosen.values())
    if not include_non_launchable:
        apps = [a for a in apps if a.launchable]
    apps.sort(key=lambda a: (a.display_name.lower(), a.desktop_id))
    return apps
