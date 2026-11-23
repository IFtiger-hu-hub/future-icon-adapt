"""Install or remove this tool's own user-level application menu launcher.

Only writes ~/.local/share/applications/future-icon-adapt.desktop (or an
isolated applications dir in tests). Never touches other apps' desktop files
or the icon theme. If a foreign file already occupies the name, refuse to
overwrite it.
"""

from __future__ import annotations

import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .constants import (
    LAUNCHER_DESKTOP_FILENAME,
    LAUNCHER_DESKTOP_ID,
    LAUNCHER_ICON_NAME,
    LAUNCHER_MENU_NAME,
    LAUNCHER_OWNED_KEY,
    THEME_DISPLAY_NAME,
    TOOL_ID,
    TOOL_VERSION,
)
from .persist import atomic_write_json


class SelfLaunchError(Exception):
    pass


PYTHON_BIN = "/usr/bin/python3"


def project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def app_script() -> Path:
    return project_root() / "app.py"


def default_target_path(applications_dir: Path) -> Path:
    return Path(applications_dir) / LAUNCHER_DESKTOP_FILENAME


def is_owned_launcher(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return any(
        line.strip() in {f"{LAUNCHER_OWNED_KEY}={TOOL_ID}", f"{LAUNCHER_OWNED_KEY}={TOOL_ID};"}
        or line.strip().startswith(f"{LAUNCHER_OWNED_KEY}={TOOL_ID}")
        for line in text.splitlines()
    )


def launcher_body(
    *,
    python_bin: str = PYTHON_BIN,
    script: Path | None = None,
    workdir: Path | None = None,
) -> str:
    script = Path(script) if script is not None else app_script()
    workdir = Path(workdir) if workdir is not None else project_root()
    return "\n".join(
        [
            "[Desktop Entry]",
            "Type=Application",
            "Version=1.0",
            f"Name={LAUNCHER_MENU_NAME}",
            f"GenericName={THEME_DISPLAY_NAME}",
            "Comment=从已装图标包搭配并应用到桌面图标",
            f"Exec={python_bin} {script}",
            f"TryExec={python_bin}",
            f"Path={workdir}",
            f"Icon={LAUNCHER_ICON_NAME}",
            "Terminal=false",
            "StartupNotify=true",
            f"StartupWMClass={LAUNCHER_DESKTOP_ID}",
            "Categories=Settings;DesktopSettings;",
            f"{LAUNCHER_OWNED_KEY}={TOOL_ID}",
            f"X-Future-Icon-Adapt-Version={TOOL_VERSION}",
            "",
        ]
    )


def _record_path(state_dir: Path) -> Path:
    return Path(state_dir) / "self-launcher.json"


def _save_record(state_dir: Path, data: dict[str, Any]) -> None:
    data["tool"] = TOOL_ID
    data["tool_version"] = TOOL_VERSION
    data["saved_at"] = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    Path(state_dir).mkdir(parents=True, exist_ok=True)
    atomic_write_json(_record_path(state_dir), data)


def launcher_status(*, applications_dir: Path, state_dir: Path | None = None) -> dict[str, Any]:
    target = default_target_path(applications_dir)
    exists = target.is_file()
    owned = is_owned_launcher(target) if exists else False
    return {
        "ok": True,
        "path": str(target),
        "exists": exists,
        "owned": owned,
        "installed": bool(exists and owned),
        "menu_name": LAUNCHER_MENU_NAME,
        "icon": LAUNCHER_ICON_NAME,
        "python": PYTHON_BIN,
        "script": str(app_script()),
        "workdir": str(project_root()),
        "foreign": bool(exists and not owned),
        "move_note": "若项目目录被搬走，需要重新安装这个启动器以更新绝对路径。",
    }


def install_self_launcher(
    *,
    applications_dir: Path,
    state_dir: Path,
    python_bin: str = PYTHON_BIN,
    script: Path | None = None,
    workdir: Path | None = None,
    run_sycoca: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """Write this tool's desktop file. Backs up an already-owned file; refuses a foreign one."""
    script = Path(script) if script is not None else app_script()
    workdir = Path(workdir) if workdir is not None else project_root()
    if not Path(python_bin).is_file():
        raise SelfLaunchError(f"找不到 Python 解释器：{python_bin}")
    if not script.is_file():
        raise SelfLaunchError(f"找不到启动脚本：{script}")
    applications_dir = Path(applications_dir)
    applications_dir.mkdir(parents=True, exist_ok=True)
    target = default_target_path(applications_dir)
    backup = None
    replaced = False
    if target.exists() or target.is_symlink():
        if not is_owned_launcher(target):
            raise SelfLaunchError(
                f"已有同名启动器且不属于本工具，拒绝覆盖：{target}"
            )
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        bdir = Path(state_dir) / "launcher-backups"
        bdir.mkdir(parents=True, exist_ok=True)
        backup = bdir / f"{LAUNCHER_DESKTOP_ID}.{stamp}.desktop.bak"
        n = 1
        while backup.exists():
            backup = bdir / f"{LAUNCHER_DESKTOP_ID}.{stamp}.{n}.desktop.bak"
            n += 1
        shutil.copy2(target, backup)
        replaced = True
    body = launcher_body(python_bin=python_bin, script=script, workdir=workdir)
    tmp = target.with_name(f".{target.name}.tmp-{TOOL_ID}")
    try:
        tmp.write_text(body, encoding="utf-8")
        tmp.chmod(0o644)
        tmp.replace(target)
        target.chmod(0o644)
    except OSError as exc:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
        raise SelfLaunchError(f"无法写入启动器：{exc}") from exc
    sycoca = False
    warnings: list[str] = []
    if run_sycoca:
        try:
            run_sycoca()
            sycoca = True
        except Exception as exc:
            warnings.append(f"刷新应用菜单缓存失败：{exc}")
    record = {
        "path": str(target),
        "backup": str(backup) if backup else None,
        "replaced": replaced,
        "icon": LAUNCHER_ICON_NAME,
        "exec": f"{python_bin} {script}",
        "workdir": str(workdir),
    }
    try:
        _save_record(state_dir, record)
    except OSError as exc:
        warnings.append(f"启动器已写入，但状态记录失败：{exc}")
    message = (
        f"已把「{LAUNCHER_MENU_NAME}」添加到应用菜单。"
        if not replaced
        else f"已更新应用菜单里的「{LAUNCHER_MENU_NAME}」启动器（已备份）。"
    )
    if warnings:
        message += "".join(warnings)
    message += "若以后移动了项目目录，请重新安装这个启动器。"
    return {
        "ok": True,
        "status": "updated" if replaced else "installed",
        "path": str(target),
        "backup": str(backup) if backup else None,
        "sycoca": sycoca,
        "warnings": warnings,
        "message": message,
    }


def remove_self_launcher(
    *,
    applications_dir: Path,
    state_dir: Path,
    run_sycoca: Callable[[], None] | None = None,
) -> dict[str, Any]:
    target = default_target_path(applications_dir)
    if not target.exists() and not target.is_symlink():
        return {
            "ok": True,
            "status": "absent",
            "path": str(target),
            "message": "应用菜单里没有本工具的启动器。",
        }
    if not is_owned_launcher(target):
        raise SelfLaunchError(f"该启动器不属于本工具，拒绝删除：{target}")
    try:
        target.unlink()
    except OSError as exc:
        raise SelfLaunchError(f"无法删除启动器：{exc}") from exc
    sycoca = False
    warnings: list[str] = []
    if run_sycoca:
        try:
            run_sycoca()
            sycoca = True
        except Exception as exc:
            warnings.append(f"刷新应用菜单缓存失败：{exc}")
    try:
        rec = _record_path(state_dir)
        if rec.is_file():
            rec.unlink()
    except OSError:
        pass
    message = f"已从应用菜单移除「{LAUNCHER_MENU_NAME}」。"
    if warnings:
        message += "".join(warnings)
    return {
        "ok": True,
        "status": "removed",
        "path": str(target),
        "sycoca": sycoca,
        "warnings": warnings,
        "message": message,
    }
