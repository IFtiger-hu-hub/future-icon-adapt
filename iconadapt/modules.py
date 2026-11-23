"""Classify installed icon-theme directories into user-facing modules.

Classification prefers the Context field in index.theme. Directory names are
only a fallback, because themes do not share a single layout (Papirus uses
48x48/apps, Breeze uses apps/48, panel/ and applets/ may both be Status).

Inventory and previews use a theme's own files only. Inherited icons are never
presented as if they belonged to the selected pack.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .constants import (
    TOOL_ID,
    TOOL_VERSION,
    MODULE_ACTIONS,
    MODULE_APPS,
    MODULE_IDS,
    MODULE_MIMETYPES,
    MODULE_OTHER,
    MODULE_PLACES,
    MODULE_SOURCE_SYSTEM,
    MODULE_STATUS,
    SETTINGS_FILENAME,
    THEME_INTERNAL_NAME,
)
from .lookup import EXTENSIONS
from .persist import atomic_write_json
from .theme import IconTheme, ThemeDirSpec

# Context values are compared case-insensitively after stripping spaces/dashes.
_CONTEXT_TO_MODULE: dict[str, str] = {
    "applications": MODULE_APPS,
    "application": MODULE_APPS,
    "apps": MODULE_APPS,
    "categories": MODULE_APPS,
    "category": MODULE_APPS,
    "places": MODULE_PLACES,
    "place": MODULE_PLACES,
    "filesystems": MODULE_PLACES,
    "filesystem": MODULE_PLACES,
    "file systems": MODULE_PLACES,
    "devices": MODULE_PLACES,
    "device": MODULE_PLACES,
    "mimetypes": MODULE_MIMETYPES,
    "mime types": MODULE_MIMETYPES,
    "mime-types": MODULE_MIMETYPES,
    "mime": MODULE_MIMETYPES,
    "actions": MODULE_ACTIONS,
    "action": MODULE_ACTIONS,
    "status": MODULE_STATUS,
    "panel": MODULE_STATUS,
    "applets": MODULE_STATUS,
    "applet": MODULE_STATUS,
    "emblems": MODULE_OTHER,
    "emblem": MODULE_OTHER,
    "emotes": MODULE_OTHER,
    "emote": MODULE_OTHER,
    "animations": MODULE_OTHER,
    "animation": MODULE_OTHER,
    "international": MODULE_OTHER,
    "intl": MODULE_OTHER,
    "stock": MODULE_OTHER,
}

# Used only when Context is missing or unrecognized.
_PATH_TOKEN_TO_MODULE: dict[str, str] = {
    **_CONTEXT_TO_MODULE,
    "applications": MODULE_APPS,
    "folders": MODULE_PLACES,
    "folder": MODULE_PLACES,
    "tray": MODULE_STATUS,
    "indicators": MODULE_STATUS,
    "indicator": MODULE_STATUS,
    "notifications": MODULE_STATUS,
}

_SCALE_RE = re.compile(r"@(\d+)x?$", re.IGNORECASE)
_SIZE_NxN_RE = re.compile(r"^(\d+)x(\d+)$", re.IGNORECASE)
_SIZE_LIKE_RE = re.compile(r"^(scalable(\W.*)?|\d+)$", re.IGNORECASE)

# Prefer these names in the preview grid when the pack actually contains them.
REPRESENTATIVE_NAMES: dict[str, tuple[str, ...]] = {
    MODULE_APPS: (
        "system-file-manager",
        "utilities-terminal",
        "preferences-system",
        "help-browser",
        "internet-web-browser",
        "accessories-text-editor",
        "system-software-install",
        "preferences-desktop-theme",
    ),
    MODULE_PLACES: (
        "folder",
        "user-home",
        "user-desktop",
        "folder-documents",
        "folder-download",
        "drive-harddisk",
        "user-trash",
        "network-workgroup",
    ),
    MODULE_MIMETYPES: (
        "text-plain",
        "application-pdf",
        "image-x-generic",
        "audio-x-generic",
        "video-x-generic",
        "inode-directory",
        "application-x-executable",
        "package-x-generic",
    ),
    MODULE_ACTIONS: (
        "document-open",
        "document-save",
        "document-new",
        "edit-copy",
        "edit-delete",
        "go-home",
        "list-add",
        "view-refresh",
    ),
    MODULE_STATUS: (
        "dialog-information",
        "dialog-warning",
        "dialog-error",
        "audio-volume-high",
        "network-wireless",
        "battery-good",
        "user-available",
        "emblem-default",
    ),
    MODULE_OTHER: (
        "emblem-default",
        "emblem-important",
        "emblem-favorite",
        "face-smile",
        "face-plain",
        "flag",
    ),
}

PREFERRED_SIZE: dict[str, int] = {
    MODULE_APPS: 48,
    MODULE_PLACES: 48,
    MODULE_MIMETYPES: 48,
    MODULE_ACTIONS: 22,
    MODULE_STATUS: 22,
    MODULE_OTHER: 22,
}

# Path tokens that often hold tray / panel icons. Presence does not prove an
# app will honor the theme; SNI host pixels and hardcoded assets will not.
_TRAYISH_TOKENS = frozenset({"panel", "applets", "applet", "tray", "indicators", "indicator"})


@dataclass(frozen=True)
class ModuleInfo:
    id: str
    title: str
    hint: str
    note: str = ""


# User-facing copy. Keep these short and free of developer jargon.
MODULE_INFO: tuple[ModuleInfo, ...] = (
    ModuleInfo(
        "apps",
        "应用图标",
        "启动器和菜单里的应用图标。还可以在下面为某个应用单独再选一个。",
    ),
    ModuleInfo(
        "places",
        "文件夹与位置",
        "文件夹、主目录、磁盘和设备位置图标。",
    ),
    ModuleInfo(
        "mimetypes",
        "文件类型",
        "文档、图片、音频等文件在文件管理器里显示的图标。",
    ),
    ModuleInfo(
        "actions",
        "系统操作",
        "打开、保存、复制、删除等按钮和菜单图标。系统可能会按配色重新上色，和预览不完全一样。",
    ),
    ModuleInfo(
        "status",
        "状态与托盘",
        "通知、音量、网络等状态图标。部分托盘由程序自己绘制，或使用固定像素，可能不生效。系统也可能按配色重新上色。",
        "无法识别的托盘、程序自带像素（例如部分 SNI 托盘）可能不会跟着主题更换。",
    ),
    ModuleInfo(
        "other",
        "其他装饰",
        "徽章、表情等剩余图标。不选则跟随基础主题。",
    ),
)

MODULE_INFO_BY_ID = {m.id: m for m in MODULE_INFO}


@dataclass
class ModuleFile:
    name: str
    path: Path
    rel_dir: str
    size: int
    scale: int
    typ: str
    context: str
    theme: str
    module: str
    maybe_unreliable_tray: bool = False


def _norm_token(value: str) -> str:
    return re.sub(r"[\s_\-]+", " ", (value or "").strip().lower())


def _strip_scale(part: str) -> tuple[str, int]:
    raw = (part or "").strip()
    m = _SCALE_RE.search(raw)
    if not m:
        return raw, 1
    try:
        scale = int(m.group(1)) or 1
    except ValueError:
        scale = 1
    return raw[: m.start()], scale


def parse_rel_scale(rel: str, declared: int = 1) -> int:
    if declared and declared != 1:
        return declared
    scale = 1
    for part in (rel or "").replace("\\", "/").split("/"):
        _, part_scale = _strip_scale(part)
        if part_scale > 1:
            scale = part_scale
    return scale


def _path_tokens(rel: str) -> list[str]:
    tokens: list[str] = []
    for raw in (rel or "").replace("\\", "/").split("/"):
        part, _ = _strip_scale(raw)
        part = part.strip()
        if not part:
            continue
        if _SIZE_NxN_RE.match(part) or _SIZE_LIKE_RE.match(part):
            continue
        tokens.append(_norm_token(part))
    return tokens


def classify_context(context: str) -> str | None:
    key = _norm_token(context)
    if not key:
        return None
    return _CONTEXT_TO_MODULE.get(key)


def classify_rel(rel: str, context: str = "") -> str:
    """Return a module id for a directory. Context wins when recognized."""
    mapped = classify_context(context)
    if mapped:
        return mapped
    for token in _path_tokens(rel):
        if token in _PATH_TOKEN_TO_MODULE:
            return _PATH_TOKEN_TO_MODULE[token]
    return MODULE_OTHER


def classify_spec(spec: ThemeDirSpec) -> str:
    return classify_rel(spec.rel, spec.context)


def module_title(module_id: str) -> str:
    info = MODULE_INFO_BY_ID.get(module_id)
    return info.title if info else module_id


def is_system_source(value: str | None) -> bool:
    if value is None:
        return True
    text = str(value).strip()
    if not text:
        return True
    return text.lower() in {
        MODULE_SOURCE_SYSTEM,
        "system",
        "inherit",
        "default",
        "none",
        "base",
    }


def normalize_source(value: str | None) -> str:
    if is_system_source(value):
        return MODULE_SOURCE_SYSTEM
    return str(value).strip()


def normalize_module_map(raw: dict[str, Any] | None) -> dict[str, str]:
    out = {mid: MODULE_SOURCE_SYSTEM for mid in MODULE_IDS}
    if not isinstance(raw, dict):
        return out
    for key, value in raw.items():
        mid = str(key).strip()
        if mid not in out:
            continue
        out[mid] = normalize_source(value)
    return out


def has_explicit_module_sources(raw: dict[str, Any] | None) -> bool:
    mapping = normalize_module_map(raw)
    return any(src != MODULE_SOURCE_SYSTEM for src in mapping.values())


def default_module_map() -> dict[str, str]:
    return {mid: MODULE_SOURCE_SYSTEM for mid in MODULE_IDS}


def settings_path(state_dir: Path) -> Path:
    return Path(state_dir) / SETTINGS_FILENAME


def load_settings_file(state_dir: Path) -> dict[str, Any]:
    path = settings_path(state_dir)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def update_settings_file(state_dir: Path, updates: dict[str, Any]) -> Path:
    """Merge top-level keys into settings.json without dropping siblings."""
    path = settings_path(state_dir)
    payload: dict[str, Any] = {}
    if path.is_file():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(existing, dict):
                payload = existing
        except (OSError, json.JSONDecodeError):
            payload = {}
    payload.update(updates)
    payload.setdefault("version", 1)
    payload["tool"] = TOOL_ID
    payload["tool_version"] = TOOL_VERSION
    atomic_write_json(path, payload)
    return path


def load_module_map(state_dir: Path) -> dict[str, str]:
    data = load_settings_file(state_dir)
    raw = data.get("modules") if isinstance(data.get("modules"), dict) else {}
    return normalize_module_map(raw)


def save_module_map(state_dir: Path, mapping: dict[str, Any]) -> Path:
    return update_settings_file(state_dir, {"modules": normalize_module_map(mapping)})


def iter_module_specs(theme: IconTheme, module_id: str) -> list[ThemeDirSpec]:
    return [spec for spec in theme.directories if classify_spec(spec) == module_id]


def trayish_rel(rel: str) -> bool:
    tokens = set(_path_tokens(rel))
    # also honor raw parts such as "panel" that we already mapped
    return bool(tokens & _TRAYISH_TOKENS)


def maybe_unreliable_tray(module_id: str, rel: str, name: str) -> bool:
    if module_id != MODULE_STATUS:
        return False
    lowered = (name or "").lower()
    if "tray" in lowered or "indicator" in lowered:
        return True
    return trayish_rel(rel)


def list_module_files(theme: IconTheme, module_id: str) -> list[ModuleFile]:
    """List this theme's own files for a module. Does not follow Inherits."""
    if not theme.valid:
        return []
    out: list[ModuleFile] = []
    seen: set[str] = set()
    for spec in iter_module_specs(theme, module_id):
        scale = parse_rel_scale(spec.rel, spec.scale)
        for base in theme.all_paths():
            folder = base / spec.rel
            if not folder.is_dir():
                continue
            try:
                entries = list(folder.iterdir())
            except OSError:
                continue
            for entry in entries:
                if entry.suffix.lower() not in EXTENSIONS:
                    continue
                # Include dangling or unusual symlinks so export can skip them safely.
                if not (entry.is_file() or entry.is_symlink()):
                    continue
                key = str(entry)
                if key in seen:
                    continue
                seen.add(key)
                out.append(
                    ModuleFile(
                        name=entry.stem,
                        path=entry,
                        rel_dir=spec.rel,
                        size=int(spec.size or 0),
                        scale=scale,
                        typ=spec.typ or "Threshold",
                        context=spec.context,
                        theme=theme.internal_name,
                        module=module_id,
                        maybe_unreliable_tray=maybe_unreliable_tray(
                            module_id, spec.rel, entry.stem
                        ),
                    )
                )
    return out


_COUNT_CACHE: dict[tuple, dict[str, int]] = {}


def clear_module_cache() -> None:
    _COUNT_CACHE.clear()


def unique_names(files: Iterable[ModuleFile]) -> list[str]:
    seen: dict[str, None] = {}
    for item in files:
        if item.name not in seen:
            seen[item.name] = None
    return list(seen.keys())


def _theme_cache_key(theme: IconTheme) -> tuple:
    return (str(theme.path), tuple(str(p) for p in theme.extra_paths))


def counts_by_module(theme: IconTheme) -> dict[str, int]:
    """Count unique stems for every module in one directory pass. No is_file() stat."""
    key = _theme_cache_key(theme)
    cached = _COUNT_CACHE.get(key)
    if cached is not None:
        return cached
    names: dict[str, set[str]] = {mid: set() for mid in MODULE_IDS}
    if theme.valid:
        for spec in theme.directories:
            mid = classify_spec(spec)
            for base in theme.all_paths():
                folder = base / spec.rel
                if not folder.is_dir():
                    continue
                try:
                    entries = folder.iterdir()
                except OSError:
                    continue
                for entry in entries:
                    # Name-only: avoids a stat() on every icon in large packs such as hicolor.
                    if entry.suffix.lower() in EXTENSIONS:
                        names[mid].add(entry.stem)
    result = {mid: len(names[mid]) for mid in MODULE_IDS}
    _COUNT_CACHE[key] = result
    return result


def module_icon_names(theme: IconTheme, module_id: str) -> set[str]:
    """Unique icon stems; used when a precise set is needed. May stat files."""
    return {item.name for item in list_module_files(theme, module_id)}


def module_icon_count(theme: IconTheme, module_id: str) -> int:
    return counts_by_module(theme).get(module_id, 0)


def module_is_empty(theme: IconTheme, module_id: str) -> bool:
    return module_icon_count(theme, module_id) == 0


def pick_preview_file(files: list[ModuleFile], preferred_size: int) -> ModuleFile | None:
    if not files:
        return None

    def score(item: ModuleFile) -> tuple:
        size = item.size or 0
        suffix = item.path.suffix.lower()
        exact = 0 if size == preferred_size else 1
        distance = abs((size or preferred_size) - preferred_size)
        # Prefer SVG when sizes are otherwise comparable so previews stay sharp.
        typ = 0 if suffix in {".svg", ".svgz"} and size in {0, preferred_size} else 1
        raster_exact = 0 if suffix in {".png", ".xpm"} and size == preferred_size else 1
        scale_pen = 0 if item.scale == 1 else 1
        return (exact, distance, typ, raster_exact, scale_pen, -size, item.rel_dir)

    return sorted(files, key=score)[0]


def pick_export_svg_source(svgs: list[Path], files) -> Path | None:
    """SVG a multi-size or masked export should raster from.

    Packs such as McMojave-circle list a small Fixed directory (apps/22)
    before apps/scalable. Taking the first SVG in that inventory order
    upscales the 22px asset — often a monochrome or simplified draft —
    into every launcher size, while the GUI preview already prefers the
    scalable file. Prefer Type=Scalable or a scalable/ directory, then
    the larger Size. Names stay distinct: microsoft-edge and
    microsoft-edge-symbolic are still written as two files.
    """
    if not svgs:
        return None
    meta: dict[str, object] = {}
    for cand in files or []:
        path = getattr(cand, "path", None)
        if path is None:
            continue
        meta[str(Path(path))] = cand
    def score(path: Path) -> tuple:
        cand = meta.get(str(Path(path)))
        typ = (getattr(cand, "typ", "") or "").lower() if cand else ""
        rel = (getattr(cand, "rel_dir", "") or "").replace("\\", "/").lower() if cand else ""
        size = int(getattr(cand, "size", 0) or 0) if cand else 0
        tokens = [part.split("@", 1)[0] for part in rel.split("/") if part]
        is_scalable = typ == "scalable" or "scalable" in tokens
        return (1 if is_scalable else 0, size)
    return max(svgs, key=score)


def _files_for_stem(theme: IconTheme, module_id: str, stem: str) -> list[ModuleFile]:
    out: list[ModuleFile] = []
    for spec in iter_module_specs(theme, module_id):
        scale = parse_rel_scale(spec.rel, spec.scale)
        for base in theme.all_paths():
            folder = base / spec.rel
            if not folder.is_dir():
                continue
            for ext in EXTENSIONS:
                entry = folder / f"{stem}{ext}"
                if not entry.is_file() and not entry.is_symlink():
                    continue
                if not entry.exists():
                    continue
                out.append(
                    ModuleFile(
                        name=stem,
                        path=entry,
                        rel_dir=spec.rel,
                        size=int(spec.size or 0),
                        scale=scale,
                        typ=spec.typ or "Threshold",
                        context=spec.context,
                        theme=theme.internal_name,
                        module=module_id,
                        maybe_unreliable_tray=maybe_unreliable_tray(module_id, spec.rel, stem),
                    )
                )
    return out


def module_has_icons(theme: IconTheme, module_id: str) -> bool:
    """True if this theme has at least one own file in the module. Early-exits."""
    cached = _COUNT_CACHE.get(_theme_cache_key(theme))
    if cached is not None:
        return cached.get(module_id, 0) > 0
    if not theme.valid:
        return False
    for spec in iter_module_specs(theme, module_id):
        for base in theme.all_paths():
            folder = base / spec.rel
            if not folder.is_dir():
                continue
            try:
                entries = folder.iterdir()
            except OSError:
                continue
            for entry in entries:
                if entry.suffix.lower() in EXTENSIONS:
                    return True
    return False


def preview_items(
    theme: IconTheme,
    module_id: str,
    *,
    limit: int = 32,
    preferred_size: int | None = None,
) -> list[dict[str, Any]]:
    """Representative then a short fill. Only this theme's own files; does not scan every icon."""
    preferred = preferred_size or PREFERRED_SIZE.get(module_id, 48)
    limit = max(1, limit)
    by_name: dict[str, list[ModuleFile]] = {}
    ordered: list[str] = []

    for name in REPRESENTATIVE_NAMES.get(module_id, ()):
        group = _files_for_stem(theme, module_id, name)
        if not group:
            continue
        by_name[name] = group
        ordered.append(name)
        if len(ordered) >= limit:
            break

    if len(ordered) < limit:
        for spec in iter_module_specs(theme, module_id):
            if len(ordered) >= limit:
                break
            scale = parse_rel_scale(spec.rel, spec.scale)
            for base in theme.all_paths():
                if len(ordered) >= limit:
                    break
                folder = base / spec.rel
                if not folder.is_dir():
                    continue
                try:
                    entries = folder.iterdir()
                except OSError:
                    continue
                for entry in entries:
                    if entry.suffix.lower() not in EXTENSIONS:
                        continue
                    name = entry.stem
                    if name in by_name:
                        continue
                    if not entry.is_file() and not (entry.is_symlink() and entry.exists()):
                        continue
                    by_name[name] = _files_for_stem(theme, module_id, name)
                    ordered.append(name)
                    if len(ordered) >= limit:
                        break

    items: list[dict[str, Any]] = []
    for name in ordered[:limit]:
        chosen = pick_preview_file(by_name[name], preferred)
        if not chosen:
            continue
        sizes = sorted({f.size for f in by_name[name] if f.size})
        items.append(module_file_to_dict(chosen, sizes=sizes, available=len(by_name[name])))
    return items


def module_file_to_dict(
    item: ModuleFile, *, sizes: list[int] | None = None, available: int = 1
) -> dict[str, Any]:
    return {
        "name": item.name,
        "path": str(item.path),
        "rel_dir": item.rel_dir,
        "size": item.size,
        "scale": item.scale,
        "theme": item.theme,
        "module": item.module,
        "context": item.context,
        "maybe_unreliable_tray": bool(item.maybe_unreliable_tray),
        "sizes": sizes or ([item.size] if item.size else []),
        "available": available,
    }


def usable_source_themes(themes: dict[str, IconTheme]) -> dict[str, IconTheme]:
    out: dict[str, IconTheme] = {}
    for name, theme in themes.items():
        if not theme.valid:
            continue
        if name == THEME_INTERNAL_NAME:
            continue
        out[name] = theme
    return out


def theme_options_for_module(
    themes: dict[str, IconTheme], module_id: str
) -> list[dict[str, Any]]:
    options: list[dict[str, Any]] = [
        {
            "id": MODULE_SOURCE_SYSTEM,
            "name": MODULE_SOURCE_SYSTEM,
            "display": "系统原样（跟随基础主题）",
            "count": 0,
            "empty": False,
            "system": True,
        }
    ]
    for name, theme in sorted(usable_source_themes(themes).items(), key=lambda kv: kv[0].lower()):
        if theme.hidden:
            continue
        count = module_icon_count(theme, module_id)
        options.append(
            {
                "id": name,
                "name": name,
                "display": theme.display_name or name,
                "count": count,
                "empty": count == 0,
                "system": False,
            }
        )
    return options


def modules_overview(
    themes: dict[str, IconTheme],
    mapping: dict[str, str],
    *,
    preview_limit: int = 8,
    inherit: list[str] | None = None,
    decorate: bool = False,
) -> list[dict[str, Any]]:
    """Per-module source + previews. inherit is the exporter's Inherits list."""
    from .preview import compose_module_previews, decorate_previews, pack_flags_for_module

    mapping = normalize_module_map(mapping)
    rows: list[dict[str, Any]] = []
    usable = usable_source_themes(themes)
    for info in MODULE_INFO:
        source = mapping.get(info.id, MODULE_SOURCE_SYSTEM)
        system = is_system_source(source)
        theme = None if system else usable.get(source)
        missing_theme = (not system) and theme is None
        composed = compose_module_previews(
            source, info.id, themes, inherit=inherit, limit=preview_limit
        )
        if theme is not None and composed["has_content"]:
            cached = _COUNT_CACHE.get(_theme_cache_key(theme))
            count = cached.get(info.id, 0) if cached is not None else 1
        else:
            count = 0
        previews = composed["previews"]
        if decorate and previews:
            previews = decorate_previews(previews)
        rows.append(
            {
                "id": info.id,
                "title": info.title,
                "hint": info.hint,
                "note": info.note,
                "source": MODULE_SOURCE_SYSTEM if system else source,
                "source_label": (
                    "系统原样（跟随基础主题）"
                    if system
                    else (theme.display_name if theme is not None else source)
                ),
                "icon_count": count,
                "empty": composed["empty"],
                "missing_theme": missing_theme or composed["missing_theme"],
                "has_content": composed["has_content"],
                "fallback": composed.get("fallback", False),
                "kind": composed.get("kind", "icons"),
                "previews": previews,
                "preview_caption": composed.get("preview_caption") or "",
                "status_line": composed.get("status_line") or "",
                "chain": composed.get("chain") or [],
                "pack_flags": pack_flags_for_module(usable, info.id),
            }
        )
    return rows


def files_by_name(theme: IconTheme, module_id: str) -> dict[str, list[ModuleFile]]:
    grouped: dict[str, list[ModuleFile]] = {}
    for item in list_module_files(theme, module_id):
        grouped.setdefault(item.name, []).append(item)
    return grouped


def specs_by_rel(theme: IconTheme, module_id: str) -> dict[str, ThemeDirSpec]:
    return {spec.rel: spec for spec in iter_module_specs(theme, module_id)}
