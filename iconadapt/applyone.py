"""Apply the current hand-picked icon to a single application.

Writes only that app's icon into an already-installed, tool-owned icon-adapt
theme, optionally fixes this app's user-level desktop Icon=, then leaves
cache refresh to the caller. Never exports a full module set, never switches
the active system theme, and never overwrites a shared Icon= name used by
other apps (a unique install name is used instead).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

from .constants import (
    MASK_COMMON_SIZES,
    OWNED_MARKER,
    SINGLE_APP_ICON_PREFIX,
    SINGLE_APP_PATCHES,
    THEME_DISPLAY_NAME,
    THEME_INTERNAL_NAME,
    TOOL_ID,
    TOOL_VERSION,
)
from .desktop import DesktopApp, is_absolute_icon
from .lookup import collect_all_sizes
from .mask import (
    MaskError,
    MaskSettings,
    apply_mask_to_file,
    load_source_rgba,
    planned_sizes,
    provenance_mask_block,
    validate_settings,
)
from .modules import pick_export_svg_source
from .override import OverrideError, apply_overrides, load_manifest, plan_override, rollback_overrides
from .persist import atomic_write_json
from .rendercache import begin_render_session, end_render_session
from .safeio import is_tool_owned
from .sourcecheck import describe_source


class SingleAppError(Exception):
    pass


_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


def patches_path(state_dir: Path) -> Path:
    return Path(state_dir) / SINGLE_APP_PATCHES


def load_patches(state_dir: Path) -> dict[str, Any]:
    path = patches_path(state_dir)
    if not path.is_file():
        return {"tool": TOOL_ID, "version": 1, "entries": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"tool": TOOL_ID, "version": 1, "entries": {}}
    data.setdefault("entries", {})
    return data


def save_patches(state_dir: Path, data: dict[str, Any]) -> None:
    data["tool"] = TOOL_ID
    data["tool_version"] = TOOL_VERSION
    data["saved_at"] = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    atomic_write_json(patches_path(state_dir), data)


def install_name_for(desktop_id: str) -> str:
    raw = (desktop_id or "").strip() or "app"
    cleaned = _SAFE_NAME.sub("-", raw).strip(".-") or "app"
    return f"{SINGLE_APP_ICON_PREFIX}{cleaned}"


def icon_share_key(icon: str) -> str:
    text = (icon or "").strip()
    if not text:
        return ""
    if is_absolute_icon(text):
        return text
    return text


def sharing_apps(apps: list[DesktopApp] | None, target: DesktopApp) -> list[DesktopApp]:
    key = icon_share_key(target.icon)
    if not key:
        return []
    others: list[DesktopApp] = []
    for app in apps or []:
        if app.desktop_id == target.desktop_id:
            continue
        if not getattr(app, "launchable", True):
            continue
        if icon_share_key(app.icon) == key:
            others.append(app)
    return others


def selection_digest(source_path: str, mask: MaskSettings) -> str:
    raw = Path(source_path).read_bytes() if source_path and Path(source_path).is_file() else b""
    sig = f"{int(bool(mask.enabled))}:{mask.shape}:{mask.background}:{mask.padding}:{mask.custom_path}"
    h = hashlib.sha256()
    h.update(raw)
    h.update(b"\0")
    h.update(sig.encode("utf-8"))
    h.update(b"\0")
    h.update(TOOL_VERSION.encode("utf-8"))
    return h.hexdigest()


def has_override(desktop_id: str, *, state_dir: Path) -> bool:
    """True if this app has a recorded single-app apply or desktop override."""
    if not desktop_id or not Path(state_dir).exists():
        return False
    if desktop_id in ((load_patches(state_dir).get("entries") or {})):
        return True
    if desktop_id in ((load_manifest(state_dir).get("entries") or {})):
        return True
    return False


def _selection_matches_entry(selection: dict | None, entry: dict | None, mask: MaskSettings | None) -> bool:
    if not selection or not entry:
        return False
    if entry.get("source_path") != selection.get("source_path"):
        return False
    if entry.get("source_theme") != selection.get("source_theme"):
        return False
    if not mask or not selection.get("source_path"):
        return True
    try:
        digest = selection_digest(str(selection.get("source_path") or ""), mask)
    except OSError:
        digest = ""
    if digest and entry.get("source_digest") and entry.get("source_digest") != digest:
        return False
    return True


def applied_status(desktop_id: str, selection: dict | None, patches: dict[str, Any] | None) -> str:
    entry = ((patches or {}).get("entries") or {}).get(desktop_id)
    if not selection:
        return "applied_changed" if entry else "none"
    if not entry:
        return "pending"
    if entry.get("source_path") == selection.get("source_path") and entry.get("source_theme") == selection.get(
        "source_theme"
    ):
        return "applied"
    return "applied_changed"


def applied_status_for(desktop_id: str, selection: dict | None, *, state_dir: Path, mask: MaskSettings) -> str:
    override = has_override(desktop_id, state_dir=state_dir)
    entry = (load_patches(state_dir).get("entries") or {}).get(desktop_id) if Path(state_dir).exists() else None
    if _selection_matches_entry(selection, entry, mask):
        return "applied"
    if override:
        return "applied_changed"
    if selection:
        return "pending"
    return "none"


def read_desktop_icon(path: Path) -> str:
    """Icon= in the first [Desktop Entry] group only."""
    if not path or not Path(path).is_file():
        return ""
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    in_entry = False
    for line in text.splitlines():
        raw = line.strip()
        if raw.startswith("[") and raw.endswith("]"):
            in_entry = raw[1:-1].strip() == "Desktop Entry"
            continue
        if in_entry and raw.startswith("Icon="):
            return raw.split("=", 1)[1].strip()
    return ""


def effective_launcher_icon(app: DesktopApp, target_apps_dir: Path | None) -> str:
    """Prefer the user-level overlay if it exists; fall back to the scanned app."""
    if app is not None and target_apps_dir:
        overlay = Path(target_apps_dir) / Path(app.path).name
        value = read_desktop_icon(overlay)
        if value:
            return value
    return (app.icon or "").strip() if app is not None else ""


def _theme_patches_path(theme_dir: Path) -> Path:
    return Path(theme_dir) / SINGLE_APP_PATCHES


def _write_theme_patches_copy(theme_dir: Path, state_dir: Path) -> None:
    if not is_tool_owned(theme_dir):
        return
    data = load_patches(state_dir)
    atomic_write_json(_theme_patches_path(theme_dir), data)


def merge_single_app_outputs(old_dest: Path, new_staging: Path) -> int:
    """Copy previously applied per-app icons into a new theme staging dir.

    Full theme install replaces the owned directory; this keeps unique
    icon-adapt.app.* outputs so single-app applies are not silently wiped.
    Does not overwrite files the new export already wrote.
    """
    if not old_dest.is_dir() or not new_staging.is_dir():
        return 0
    copied = 0
    prefix = f"{SINGLE_APP_ICON_PREFIX}"
    for png in old_dest.rglob("*.png"):
        if not png.name.startswith(prefix):
            continue
        try:
            rel = png.relative_to(old_dest)
        except ValueError:
            continue
        dest = new_staging / rel
        if dest.exists() or dest.is_symlink():
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(png, dest)
        copied += 1
    old_patches = _theme_patches_path(old_dest)
    new_patches = _theme_patches_path(new_staging)
    if old_patches.is_file() and not new_patches.exists():
        shutil.copy2(old_patches, new_patches)
        copied += 1
    return copied


def _pick_source(selection: dict, themes: dict | None):
    path = Path(selection.get("source_path") or "")
    status = describe_source(path)
    if not status.ok:
        raise SingleAppError(f"手选图标无法使用：{status.display}")
    theme_name = selection.get("source_theme") or ""
    icon_name = selection.get("icon_name") or path.stem
    files = []
    theme = (themes or {}).get(theme_name)
    if theme is not None and icon_name:
        files = list(collect_all_sizes(icon_name, theme) or [])
    if not files:
        suffix = path.suffix.lower()
        typ = "Scalable" if suffix in {".svg", ".svgz"} else "Fixed"
        size = 0 if typ == "Scalable" else 48
        rel = "apps/scalable" if typ == "Scalable" else "48x48/apps"
        files = [SimpleNamespace(path=path, rel_dir=rel, size=size, typ=typ)]
    return path, files


def _export_svg(files) -> Path | None:
    svgs: list[Path] = []
    readable = []
    for cand in files:
        p = Path(cand.path)
        st = describe_source(p)
        if not st.ok:
            continue
        readable.append(cand)
        if p.suffix.lower() in {".svg", ".svgz"}:
            svgs.append(p)
    return pick_export_svg_source(svgs, readable)


def _write_plain_png(source: Path, dest: Path, size: int) -> Path:
    img = load_source_rgba(source, size)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not img.save(str(dest), "PNG"):
        raise SingleAppError(f"无法写出图标：{dest}")
    return dest


def _write_one_size(source: Path, dest: Path, size: int, mask: MaskSettings) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if mask.enabled:
        try:
            validate_settings(mask)
            apply_mask_to_file(source, dest, size, mask)
        except MaskError as exc:
            raise SingleAppError(str(exc)) from exc
    else:
        _write_plain_png(source, dest, size)
    if dest.is_symlink():
        raise SingleAppError(f"单应用写入不应写成符号链接：{dest}")
    return dest


def _atomic_copy(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(f".{dest.name}.tmp-{os.getpid()}")
    try:
        shutil.copy2(src, tmp)
        os.replace(tmp, dest)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def _rel_for_size(size: int, icon_name: str) -> str:
    return f"{size}x{size}/apps/{icon_name}.png"


def plan_apply_one(
    *,
    desktop_id: str,
    app: DesktopApp | None,
    selection: dict | None,
    apps: list[DesktopApp] | None,
    state_dir: Path,
    theme_dir: Path,
    current_theme: str,
    mask: MaskSettings,
    themes: dict | None = None,
    target_apps_dir: Path | None = None,
) -> dict[str, Any]:
    """Return a plan or a failed preflight. Does not write."""
    if not selection or not selection.get("source_path"):
        return {"ok": False, "error": "请先点选一个图标，再应用到这个应用。"}
    if app is None:
        return {"ok": False, "error": "未找到该应用"}
    status = describe_source(Path(selection["source_path"]))
    if not status.ok:
        return {"ok": False, "error": f"手选图标无法使用：{status.display}"}

    theme_dir = Path(theme_dir)
    current = (current_theme or "").strip()
    if not theme_dir.exists():
        return {
            "ok": False,
            "error": (
                f"还没有安装本工具主题（{THEME_INTERNAL_NAME}）。"
                "「应用到这个应用」不会重新生成整套主题。"
                f"请先点「安装主题」，并在系统设置中选用「{THEME_DISPLAY_NAME}」。"
                "本工具不会自动切换主题。"
            ),
        }
    if not is_tool_owned(theme_dir):
        return {
            "ok": False,
            "error": (
                f"已安装的 {THEME_INTERNAL_NAME} 目录不是本工具生成的主题，拒绝写入，以免破坏其他文件。"
            ),
        }
    if current != THEME_INTERNAL_NAME:
        shown = current or "（未设置）"
        return {
            "ok": False,
            "error": (
                f"当前系统主题是「{shown}」，不是 {THEME_INTERNAL_NAME}。"
                "应用到单个应用需要正在使用本工具主题，否则改启动器后菜单可能找不到图标。"
                f"本工具不会自动切换主题。请先在系统设置中选用「{THEME_DISPLAY_NAME}」。"
            ),
        }

    install_name = install_name_for(desktop_id)
    shared = sharing_apps(apps, app)
    digest = selection_digest(str(selection["source_path"]), mask)
    patches = load_patches(state_dir)
    status_now = applied_status_for(desktop_id, selection, state_dir=state_dir, mask=mask)
    already = status_now == "applied" and _files_present(theme_dir, install_name)
    if already:
        current_icon = effective_launcher_icon(app, target_apps_dir)
        if current_icon == install_name:
            return {
                "ok": True,
                "status": "unchanged",
                "desktop_id": desktop_id,
                "app_name": app.display_name,
                "install_name": install_name,
                "shared": [a.desktop_id for a in shared],
                "message": f"「{app.display_name}」已经是当前选中的图标，无需修改。",
                "summary": f"「{app.display_name}」已经是当前选中的图标，无需修改。",
            }

    shared_note = ""
    if shared or (app.icon or "").strip() not in {"", install_name}:
        if shared:
            names = "、".join(a.display_name or a.desktop_id for a in shared[:5])
            more = f" 等 {len(shared)} 个" if len(shared) > 5 else ""
            shared_note = (
                f"另有应用（{names}{more}）共用原来的图标名「{app.icon}」，"
                "会给这个应用单独的图标名，以免改到它们。"
            )
        else:
            shared_note = "会写入单独的图标名，只改这个应用的启动器，避免牵动其他应用。"

    summary = (
        f"将只导出并安装「{app.display_name}」当前选中的图标，必要时修正这个应用的启动器，然后刷新图标缓存。"
        "不会重新生成整套主题，也不会改其他应用。只写在你的用户目录里，并先备份。"
    )
    if shared_note:
        summary = summary + shared_note
    return {
        "ok": True,
        "status": "ready",
        "desktop_id": desktop_id,
        "app_name": app.display_name,
        "install_name": install_name,
        "current_icon": app.icon,
        "shared": [a.desktop_id for a in shared],
        "digest": digest,
        "summary": summary,
        "message": summary,
        "target_apps_dir": str(target_apps_dir or ""),
    }


def _files_present(theme_dir: Path, install_name: str) -> bool:
    for size in MASK_COMMON_SIZES:
        path = theme_dir / f"{size}x{size}" / "apps" / f"{install_name}.png"
        if not path.is_file():
            return False
    return True


def apply_one(
    *,
    desktop_id: str,
    app: DesktopApp | None,
    selection: dict | None,
    apps: list[DesktopApp] | None,
    state_dir: Path,
    theme_dir: Path,
    current_theme: str,
    mask: MaskSettings,
    themes: dict | None = None,
    target_apps_dir: Path,
    apply_desktop: Callable[..., dict] | None = None,
) -> dict[str, Any]:
    """Export one icon into the installed theme and fix this app's launcher if needed."""
    plan = plan_apply_one(
        desktop_id=desktop_id,
        app=app,
        selection=selection,
        apps=apps,
        state_dir=state_dir,
        theme_dir=theme_dir,
        current_theme=current_theme,
        mask=mask,
        themes=themes,
        target_apps_dir=target_apps_dir,
    )
    if not plan.get("ok"):
        return {"ok": False, "status": "failed", "error": plan.get("error"), "message": plan.get("error")}
    if plan.get("status") == "unchanged":
        return {
            "ok": True,
            "status": "unchanged",
            "desktop_id": desktop_id,
            "install_name": plan["install_name"],
            "desktop_changed": False,
            "theme_changed": False,
            "shared": plan.get("shared") or [],
            "message": plan["message"],
        }

    assert app is not None and selection is not None
    install_name = plan["install_name"]
    theme_dir = Path(theme_dir)
    state_dir = Path(state_dir)
    _, files = _pick_source(selection, themes)
    svg = _export_svg(files)
    has_svg = svg is not None
    sizes = planned_sizes(files, has_svg=has_svg)
    source_for_sizes = svg or Path(selection["source_path"])

    work = state_dir / "single-app-work" / desktop_id
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True, exist_ok=True)
    backup_root = state_dir / "single-app-backups" / desktop_id
    if backup_root.exists():
        shutil.rmtree(backup_root)
    backup_root.mkdir(parents=True, exist_ok=True)

    written_rels: list[str] = []
    backups: dict[str, str] = {}
    created: list[Path] = []
    desktop_changed = False
    writer = apply_desktop if apply_desktop is not None else apply_overrides

    begin_render_session()
    try:
        for size in sizes:
            rel = _rel_for_size(size, install_name)
            staged = work / rel
            _write_one_size(source_for_sizes, staged, size, mask)
            written_rels.append(rel)

        for rel in written_rels:
            dest = theme_dir / rel
            if dest.exists() or dest.is_symlink():
                bak = backup_root / rel
                bak.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(dest, bak, follow_symlinks=False) if not dest.is_symlink() else shutil.copy2(dest, bak)
                backups[rel] = str(bak)
            else:
                created.append(dest)
            _atomic_copy(work / rel, dest)

        # Record patches before the desktop write so a later crash still shows pending vs applied correctly after rollback.
        entry = {
            "desktop_id": desktop_id,
            "icon_name": install_name,
            "source_theme": selection.get("source_theme") or "",
            "source_path": selection.get("source_path") or "",
            "source_digest": selection_digest(str(selection.get("source_path") or ""), mask),
            "files": written_rels,
            "backups": backups,
            "created_rels": [str(Path(p).relative_to(theme_dir)) if p.exists() or True else "" for p in created],
            "mask": provenance_mask_block(mask),
            "applied_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
            "tool": TOOL_ID,
        }
        # created_rels: use the rels we intended to create
        entry["created_rels"] = [rel for rel in written_rels if rel not in backups]
        data = load_patches(state_dir)
        data.setdefault("entries", {})[desktop_id] = entry
        save_patches(state_dir, data)
        _write_theme_patches_copy(theme_dir, state_dir)

        need_desktop = effective_launcher_icon(app, target_apps_dir) != install_name
        if need_desktop:
            item = plan_override(
                desktop_id=desktop_id,
                source_desktop=app.path,
                target_apps_dir=Path(target_apps_dir),
                new_icon=install_name,
            )
            result = writer(state_dir, Path(target_apps_dir), [item])
            rows = result.get("results") or []
            if not rows or not rows[0].get("ok"):
                err = (rows[0].get("error") if rows else None) or "写入启动器失败"
                raise SingleAppError(err)
            desktop_changed = True

        shared = plan.get("shared") or []
        if shared:
            message = (
                f"已把「{app.display_name}」的图标单独应用到系统（只改了这个应用，并已刷新图标缓存）。"
                f"另有 {len(shared)} 个应用共用原来的图标名「{app.icon}」，它们未被改动。"
            )
        else:
            message = f"已把「{app.display_name}」的图标应用到系统（只改了这个应用，并已刷新图标缓存）。"
        return {
            "ok": True,
            "status": "applied",
            "desktop_id": desktop_id,
            "install_name": install_name,
            "desktop_changed": desktop_changed,
            "theme_changed": True,
            "shared": shared,
            "files": written_rels,
            "message": message,
        }
    except Exception as exc:
        created_rels = [rel for rel in written_rels if rel not in backups]
        _rollback_theme_files(theme_dir, backups, created_rels)
        # revert patch entry if we wrote one this round
        try:
            data = load_patches(state_dir)
            if desktop_id in (data.get("entries") or {}) and not desktop_changed:
                # only drop if desktop was not changed; if desktop changed we shouldn't get here
                # If we recorded a new entry, remove it on failed apply.
                data["entries"].pop(desktop_id, None)
                save_patches(state_dir, data)
                if theme_dir.is_dir() and is_tool_owned(theme_dir):
                    _write_theme_patches_copy(theme_dir, state_dir)
        except OSError:
            pass
        if isinstance(exc, (SingleAppError, OverrideError)):
            return {"ok": False, "status": "failed", "error": str(exc), "message": str(exc)}
        return {"ok": False, "status": "failed", "error": str(exc), "message": f"应用到这个应用失败，已尝试回滚：{exc}"}
    finally:
        end_render_session()
        try:
            if work.exists():
                shutil.rmtree(work)
        except OSError:
            pass


def _rollback_theme_files(
    theme_dir: Path,
    backups: dict[str, str],
    created_rels: list[str],
    written_rels: list[str] | None = None,
) -> None:
    for rel, bak in backups.items():
        dest = theme_dir / rel
        src = Path(bak)
        if src.is_file():
            try:
                _atomic_copy(src, dest)
            except OSError:
                pass
    rels = created_rels or (written_rels or [])
    for rel in rels:
        if rel in backups:
            continue
        dest = theme_dir / rel
        try:
            if dest.is_file() or dest.is_symlink():
                dest.unlink()
        except OSError:
            pass


def rollback_one(
    *,
    desktop_id: str,
    state_dir: Path,
    theme_dir: Path,
) -> dict[str, Any]:
    """Restore this app's launcher overlay and unique theme files recorded by apply_one."""
    data = load_patches(state_dir)
    entry = (data.get("entries") or {}).get(desktop_id)
    desktop_result = rollback_overrides(state_dir, [desktop_id])
    theme_dir = Path(theme_dir)
    restored = 0
    removed = 0
    if entry and is_tool_owned(theme_dir):
        backups = entry.get("backups") or {}
        created = entry.get("created_rels") or []
        _rollback_theme_files(theme_dir, backups, created, entry.get("files") or [])
        restored = len(backups)
        removed = len(created)
        data["entries"].pop(desktop_id, None)
        save_patches(state_dir, data)
        try:
            _write_theme_patches_copy(theme_dir, state_dir)
        except OSError:
            pass
    elif entry:
        data["entries"].pop(desktop_id, None)
        save_patches(state_dir, data)
    rows = desktop_result.get("results") or []
    desk_ok = not rows or rows[0].get("ok")
    if desk_ok:
        message = "已撤销对这个应用的单独应用。"
        return {
            "ok": True,
            "status": "rolled_back",
            "desktop_id": desktop_id,
            "theme_restored": restored,
            "theme_removed": removed,
            "desktop": desktop_result,
            "message": message,
        }
    err = rows[0].get("error") if rows else "撤销启动器失败"
    return {"ok": False, "status": "failed", "error": err, "message": err, "desktop": desktop_result}
