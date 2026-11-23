"""Optional user-level desktop Icon= overrides. Applied only on explicit request.

Never writes unless apply() is called with a destination applications dir.
Backups and a manifest live in the tool state directory.
Rollback only restores files this tool recorded.
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .constants import OVERRIDE_MANIFEST, OWNED_MARKER, TOOL_ID, TOOL_VERSION
from .desktop import parse_desktop_file
from .persist import atomic_write_json


class OverrideError(Exception):
    pass


def manifest_path(state_dir: Path) -> Path:
    return state_dir / OVERRIDE_MANIFEST


def backup_dir(state_dir: Path) -> Path:
    return state_dir / "desktop-backups"


def load_manifest(state_dir: Path) -> dict[str, Any]:
    path = manifest_path(state_dir)
    if not path.is_file():
        return {"tool": TOOL_ID, "version": 1, "entries": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"tool": TOOL_ID, "version": 1, "entries": {}}
    data.setdefault("entries", {})
    return data


def save_manifest(state_dir: Path, data: dict[str, Any]) -> None:
    data["tool"] = TOOL_ID
    data["tool_version"] = TOOL_VERSION
    data["saved_at"] = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    atomic_write_json(manifest_path(state_dir), data)


def _rewrite_icon(text: str, new_icon: str) -> str:
    """Replace Icon= in the first Desktop Entry group only."""
    out: list[str] = []
    in_entry = False
    replaced = False
    for line in text.splitlines(keepends=True):
        raw = line.rstrip("\r\n")
        ending = line[len(raw):]
        if raw.startswith("[") and raw.endswith("]"):
            section = raw[1:-1].strip()
            if section == "Desktop Entry":
                in_entry = True
            elif in_entry and not replaced:
                out.append(f"Icon={new_icon}{ending}")
                replaced = True
                in_entry = False
            else:
                in_entry = False
            out.append(line)
            continue
        if in_entry and raw.startswith("Icon="):
            out.append(f"Icon={new_icon}{ending}")
            replaced = True
            continue
        out.append(line)
    if in_entry and not replaced:
        out.append(f"Icon={new_icon}\n")
    return "".join(out)


def plan_override(
    *,
    desktop_id: str,
    source_desktop: Path,
    target_apps_dir: Path,
    new_icon: str,
) -> dict[str, Any]:
    target = target_apps_dir / source_desktop.name
    # If source is already in target dir, still plan a rewrite
    return {
        "desktop_id": desktop_id,
        "source": str(source_desktop),
        "target": str(target),
        "new_icon": new_icon,
        "target_exists": target.exists(),
    }


def apply_overrides(
    state_dir: Path,
    target_apps_dir: Path,
    items: list[dict[str, Any]],
) -> dict[str, Any]:
    """Write user-level desktop files. Requires explicit call. Backs up first."""
    target_apps_dir.mkdir(parents=True, exist_ok=True)
    bdir = backup_dir(state_dir)
    bdir.mkdir(parents=True, exist_ok=True)
    manifest = load_manifest(state_dir)
    results: list[dict[str, Any]] = []
    for item in items:
        source = Path(item["source"])
        target = Path(item["target"])
        new_icon = item["new_icon"]
        did = item["desktop_id"]
        if not source.is_file():
            results.append({"desktop_id": did, "ok": False, "error": "源 desktop 不存在"})
            continue
        if target.exists() and not _is_ours_or_recorded(target, manifest, did):
            # Backup existing user file before replacing
            pass
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        backup_path = None
        if target.exists():
            backup_path = bdir / f"{did}.{stamp}.desktop.bak"
            # unique
            n = 1
            while backup_path.exists():
                backup_path = bdir / f"{did}.{stamp}.{n}.desktop.bak"
                n += 1
            shutil.copy2(target, backup_path)
        try:
            # Existing user file: rewrite Icon= on its current content (already backed up).
            # New overlay: start from the source desktop and set Icon=.
            base = target if target.exists() and backup_path else source
            # After backup, target still holds the old content until we write:
            if backup_path:
                base = target
                # we copied old target to backup; rewrite Icon on that existing content
                raw = backup_path.read_text(encoding="utf-8", errors="replace")
            else:
                raw = source.read_text(encoding="utf-8", errors="replace")
            new_text = _rewrite_icon(raw, new_icon)
            target.write_text(new_text, encoding="utf-8")
        except OSError as exc:
            results.append({"desktop_id": did, "ok": False, "error": str(exc)})
            continue
        manifest["entries"][did] = {
            "desktop_id": did,
            "target": str(target),
            "source": str(source),
            "new_icon": new_icon,
            "backup": str(backup_path) if backup_path else None,
            "created": backup_path is None,
            "applied_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
            "tool": TOOL_ID,
        }
        results.append({"desktop_id": did, "ok": True, "target": str(target), "backup": str(backup_path) if backup_path else None})
    save_manifest(state_dir, manifest)
    # marker in backup dir
    atomic_write_json(bdir / OWNED_MARKER, {"tool": TOOL_ID, "kind": "desktop-backups"})
    return {"results": results, "manifest": str(manifest_path(state_dir))}


def _is_ours_or_recorded(target: Path, manifest: dict, did: str) -> bool:
    ent = (manifest.get("entries") or {}).get(did)
    if not ent:
        return False
    return Path(ent.get("target", "")) == target


def rollback_overrides(state_dir: Path, desktop_ids: list[str] | None = None) -> dict[str, Any]:
    manifest = load_manifest(state_dir)
    entries = manifest.get("entries") or {}
    ids = list(desktop_ids) if desktop_ids is not None else list(entries.keys())
    results: list[dict[str, Any]] = []
    for did in ids:
        ent = entries.get(did)
        if not ent:
            results.append({"desktop_id": did, "ok": False, "error": "清单中无此记录"})
            continue
        target = Path(ent["target"])
        backup = Path(ent["backup"]) if ent.get("backup") else None
        try:
            if ent.get("created") and not backup:
                # We created the overlay; remove only if it still looks like our target
                if target.is_file():
                    target.unlink()
                results.append({"desktop_id": did, "ok": True, "action": "removed-overlay"})
            elif backup and backup.is_file():
                shutil.copy2(backup, target)
                results.append({"desktop_id": did, "ok": True, "action": "restored-backup"})
            elif not target.exists():
                results.append({"desktop_id": did, "ok": True, "action": "already-absent"})
            else:
                results.append({"desktop_id": did, "ok": False, "error": "无备份且文件已存在，拒绝盲目删除"})
                continue
        except OSError as exc:
            results.append({"desktop_id": did, "ok": False, "error": str(exc)})
            continue
        del entries[did]
    manifest["entries"] = entries
    save_manifest(state_dir, manifest)
    return {"results": results}


def uninstall_tool_state(state_dir: Path, *, must_be_owned: bool = True) -> str:
    """Remove the tool state directory only if it is clearly ours or empty-enough."""
    if not state_dir.exists():
        return "状态目录不存在"
    marker = state_dir / OWNED_MARKER
    if must_be_owned and not marker.is_file():
        # If the dir only contains our known files, still require marker to be safe
        raise OverrideError(f"拒绝删除未标记的状态目录：{state_dir}")
    if marker.is_file():
        try:
            data = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise OverrideError(f"状态目录标记无法识别：{exc}") from exc
        if data.get("tool") != TOOL_ID:
            raise OverrideError("状态目录标记不属于本工具")
    shutil.rmtree(state_dir)
    return f"已删除本工具状态目录：{state_dir}"
