"""Manual selections persist across updates. Never drops user picks on merge."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .constants import CHOICES_FILENAME, TOOL_ID, TOOL_VERSION


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def choices_path(state_dir: Path) -> Path:
    return state_dir / CHOICES_FILENAME


def empty_store() -> dict[str, Any]:
    return {"version": 1, "tool": TOOL_ID, "tool_version": TOOL_VERSION, "selections": {}}


def load_choices(state_dir: Path) -> dict[str, Any]:
    path = choices_path(state_dir)
    if not path.is_file():
        return empty_store()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return empty_store()
    if not isinstance(data, dict):
        return empty_store()
    data.setdefault("version", 1)
    data.setdefault("tool", TOOL_ID)
    data.setdefault("selections", {})
    if not isinstance(data["selections"], dict):
        data["selections"] = {}
    return data


def atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", suffix=".json", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def save_choices(state_dir: Path, store: dict[str, Any]) -> Path:
    store["tool"] = TOOL_ID
    store["tool_version"] = TOOL_VERSION
    store["saved_at"] = _now()
    path = choices_path(state_dir)
    atomic_write_json(path, store)
    return path


def upsert_selection(
    store: dict[str, Any],
    desktop_id: str,
    *,
    icon_name: str,
    source_theme: str,
    source_path: str,
    manual: bool = True,
    note: str = "",
) -> dict[str, Any]:
    sel = {
        "desktop_id": desktop_id,
        "icon_name": icon_name,
        "source_theme": source_theme,
        "source_path": source_path,
        "manual": bool(manual),
        "updated_at": _now(),
    }
    if note:
        sel["note"] = note
    store.setdefault("selections", {})
    store["selections"][desktop_id] = sel
    return sel


def remove_selection(store: dict[str, Any], desktop_id: str) -> bool:
    sels = store.setdefault("selections", {})
    if desktop_id in sels:
        del sels[desktop_id]
        return True
    return False


def merge_keeping_manual(existing: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    """Update/refresh must not overwrite manual selections."""
    out = empty_store()
    out.update({k: existing[k] for k in existing if k != "selections"})
    sels = dict(existing.get("selections") or {})
    for did, sel in (incoming.get("selections") or {}).items():
        old = sels.get(did)
        if old and old.get("manual") and not sel.get("overwrite_manual"):
            continue
        sels[did] = sel
    out["selections"] = sels
    return out
