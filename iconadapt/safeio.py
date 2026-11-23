"""Owned-directory staging and atomic replace for generated themes.

All generation happens in a sibling staging directory. The destination is
renamed aside and replaced only after the new tree is complete. Failures
delete only the staging directory or put the backup back; they never
rmtree an existing destination first.
"""

from __future__ import annotations

import json
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .constants import OWNED_MARKER, THEME_INTERNAL_NAME, TOOL_ID, TOOL_VERSION
from .persist import atomic_write_json


class SafeIOError(Exception):
    pass


def is_tool_owned(theme_dir: Path) -> bool:
    marker = theme_dir / OWNED_MARKER
    if not marker.is_file():
        return False
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return data.get("tool") == TOOL_ID


def write_owned_marker(theme_dir: Path, extra: dict | None = None) -> None:
    payload = {
        "tool": TOOL_ID,
        "version": TOOL_VERSION,
        "theme_internal_name": THEME_INTERNAL_NAME,
        "created_or_updated": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
    }
    if extra:
        payload.update(extra)
    atomic_write_json(theme_dir / OWNED_MARKER, payload)


def assert_can_replace(dest: Path, *, overwrite_owned: bool = True) -> None:
    """Raise before any write if dest cannot be replaced. Does not modify dest."""
    if not dest.exists() and not dest.is_symlink():
        return
    if is_tool_owned(dest):
        if overwrite_owned:
            return
        raise SafeIOError(f"主题目录已存在（本工具所有）。请明确覆盖或换名：{dest}")
    raise SafeIOError(f"拒绝覆盖已有同名目录（非本工具所有）：{dest}")


def _same_parent(path: Path, dest: Path) -> bool:
    try:
        return path.resolve().parent == dest.resolve().parent
    except OSError:
        return False


def is_staging_dir(path: Path, dest: Path) -> bool:
    name = path.name
    return (
        _same_parent(path, dest)
        and name.startswith(f".{dest.name}.staging-")
        and len(name) > len(f".{dest.name}.staging-")
    )


def is_backup_dir(path: Path, dest: Path) -> bool:
    name = path.name
    return (
        _same_parent(path, dest)
        and name.startswith(f".{dest.name}.old-")
        and len(name) > len(f".{dest.name}.old-")
    )


def create_owned_staging(dest: Path) -> Path:
    """Create an empty tool-owned staging directory next to dest."""
    dest = Path(dest)
    parent = dest.parent
    parent.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex[:12]
    staging = parent / f".{dest.name}.staging-{token}"
    if staging.exists() or staging.is_symlink():
        raise SafeIOError(f"临时目录已存在，已中止以免覆盖：{staging}")
    staging.mkdir(mode=0o755)
    write_owned_marker(staging, extra={"kind": "staging", "final_name": dest.name})
    return staging


def discard_staging(staging: Path, dest: Path) -> None:
    """Remove a staging directory we created. Never deletes dest itself."""
    staging = Path(staging)
    dest = Path(dest)
    if not staging.exists() and not staging.is_symlink():
        return
    if staging.resolve() == dest.resolve():
        raise SafeIOError(f"拒绝删除：临时目录不能是目标目录本身：{staging}")
    if not is_staging_dir(staging, dest):
        raise SafeIOError(f"拒绝删除：不是本次导出的临时目录：{staging}")
    if not is_tool_owned(staging):
        raise SafeIOError(f"拒绝删除：临时目录缺少本工具所有权标记：{staging}")
    shutil.rmtree(staging)


def _discard_backup(backup: Path, dest: Path) -> None:
    backup = Path(backup)
    dest = Path(dest)
    if not backup.exists() and not backup.is_symlink():
        return
    if backup.resolve() == dest.resolve():
        raise SafeIOError(f"拒绝删除：备份目录不能是目标目录本身：{backup}")
    if not is_backup_dir(backup, dest):
        raise SafeIOError(f"拒绝删除：不是本次替换的备份目录：{backup}")
    if not is_tool_owned(backup):
        raise SafeIOError(f"拒绝删除：备份目录缺少本工具所有权标记：{backup}")
    shutil.rmtree(backup)


def replace_owned_dir(dest: Path, staging: Path) -> None:
    """Atomically (as far as rename allows) put staging at dest.

    If dest already exists it must be tool-owned. It is renamed aside first,
    then staging is renamed onto dest. Any failure after the first rename
    puts the backup back. Dest is never rmtree'd in place.
    """
    dest = Path(dest)
    staging = Path(staging)
    if not is_staging_dir(staging, dest):
        raise SafeIOError(f"拒绝切换：来源不是目标旁的临时目录：{staging}")
    if not is_tool_owned(staging):
        raise SafeIOError(f"拒绝切换：临时目录不是本工具所有：{staging}")
    assert_can_replace(dest, overwrite_owned=True)

    backup: Path | None = None
    dest_existed = dest.exists() or dest.is_symlink()
    try:
        if dest_existed:
            token = uuid.uuid4().hex[:12]
            backup = dest.parent / f".{dest.name}.old-{token}"
            if backup.exists() or backup.is_symlink():
                raise SafeIOError(f"备份名冲突，已中止：{backup}")
            os_rename = os_rename_dir(dest, backup)
            if not os_rename:
                raise SafeIOError(f"无法把旧目录移到备份位置：{dest}")
        if dest.exists() or dest.is_symlink():
            # Should not happen after rename.
            raise SafeIOError(f"切换失败：目标位置仍被占用：{dest}")
        if not os_rename_dir(staging, dest):
            raise SafeIOError(f"无法把临时目录切换到目标位置：{dest}")
    except Exception:
        if backup is not None and backup.exists() and not dest.exists():
            os_rename_dir(backup, dest)
        raise
    if backup is not None:
        _discard_backup(backup, dest)


def os_rename_dir(src: Path, dest: Path) -> bool:
    try:
        src.rename(dest)
        return True
    except OSError:
        return False


__all__ = [
    "SafeIOError",
    "assert_can_replace",
    "create_owned_staging",
    "discard_staging",
    "is_tool_owned",
    "replace_owned_dir",
    "write_owned_marker",
]
