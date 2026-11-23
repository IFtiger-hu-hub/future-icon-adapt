"""Background generate / install / uninstall of this tool's theme.

Rendering uses QSvgRenderer + QImage on a dedicated worker thread (never QPixmap).
The worker must not create or touch GUI widgets. Dest writes stay transactional.
"""

from __future__ import annotations

import json
import os
import shutil
import threading
from pathlib import Path
from typing import Any, Callable

from PyQt6.QtCore import QObject, QThread, pyqtSignal, pyqtSlot

from .applyone import merge_single_app_outputs
from .constants import THEME_INTERNAL_NAME
from .export import (
    ExportCancelled,
    ExportError,
    ExportHooks,
    export_theme,
    is_tool_owned,
    uninstall_owned_theme,
)
from .mask import MaskError
from .refresh import RefreshError, RefreshHooks, refresh_desktop_icons
from .safeio import (
    SafeIOError,
    assert_can_replace,
    create_owned_staging,
    discard_staging,
    replace_owned_dir,
)


def _copytree_with_progress(src: Path, dst: Path, hooks: ExportHooks) -> int:
    """Copy a built theme into staging. Cancel is checked before and after the copy."""
    total = 0
    for _, _, files in os.walk(src):
        total += len(files)
    hooks.emit(
        stage="copy",
        done=0,
        total=total,
        message=f"正在复制主题文件（共 {total} 个）…" if total else "正在复制主题文件…",
    )
    hooks.check()
    # Keep shutil.copytree(..., symlinks=True) so theme symlinks stay links.
    # Staging already contains the ownership marker, so dirs_exist_ok is required.
    shutil.copytree(src, dst, symlinks=True, dirs_exist_ok=True)
    hooks.emit(
        stage="copy",
        done=total,
        total=total,
        message=f"已复制 {total} 个文件",
    )
    return total


def run_theme_job(snapshot: dict[str, Any], hooks: ExportHooks) -> dict[str, Any]:
    """Run a frozen snapshot. Never reads live UI settings. Raises ExportCancelled at safe points."""
    kind = snapshot["kind"]
    hooks.emit(stage="validate", done=0, total=0, message="正在准备…")
    if kind == "uninstall":
        return _run_uninstall(snapshot, hooks)
    if kind == "generate":
        return _run_export(snapshot, hooks)
    if kind in {"install", "install_existing"}:
        return _run_install(snapshot, hooks)
    raise ExportError(f"未知任务：{kind}")


def _run_export(snapshot: dict[str, Any], hooks: ExportHooks) -> dict[str, Any]:
    hooks.emit(stage="export", done=0, total=0, message="正在生成主题…")
    build = Path(snapshot["build_dir"])
    prov = export_theme(
        build,
        apps=snapshot["apps"],
        themes=snapshot["themes"],
        status=snapshot["strategy"],
        selections=snapshot["selections"] or {},
        overwrite_owned=True,
        theme_internal_name=THEME_INTERNAL_NAME,
        mask=snapshot["mask"],
        modules=snapshot["modules"],
        hooks=hooks,
    )
    summary = prov.get("source_issue_summary") or ""
    return {
        "ok": True,
        "kind": "generate",
        "dest": str(build),
        "icons": len(prov.get("icons", [])),
        "skipped": len(prov.get("skipped", [])),
        "source_issues": len(prov.get("source_issues") or []),
        "source_issue_summary": summary,
        "mask_enabled": bool(getattr(snapshot["mask"], "enabled", False)),
    }


def _run_install(snapshot: dict[str, Any], hooks: ExportHooks) -> dict[str, Any]:
    kind = snapshot["kind"]
    build = Path(snapshot["build_dir"])
    need_export = kind == "install" or not is_tool_owned(build)
    result: dict[str, Any] = {"ok": True, "kind": kind, "mask_enabled": bool(getattr(snapshot["mask"], "enabled", False))}
    if need_export:
        exported = _run_export(snapshot, hooks)
        result.update(exported)
        result["kind"] = kind
    elif not is_tool_owned(build):
        raise ExportError("还没有生成主题，请先安装或先生成预览。")
    hooks.check()
    dest = Path(snapshot["dest_dir"])
    hooks.emit(stage="copy", done=0, total=0, message="正在复制到用户图标目录…")
    try:
        assert_can_replace(dest, overwrite_owned=True)
    except SafeIOError as exc:
        raise ExportError(str(exc)) from exc
    staging = create_owned_staging(dest)
    try:
        hooks.check()
        _copytree_with_progress(build, staging, hooks)
        if dest.exists() and is_tool_owned(dest):
            hooks.emit(stage="copy", message="正在合并已应用到单个应用的图标…")
            merge_single_app_outputs(dest, staging)
        hooks.emit(stage="commit", message="正在写入主题目录…")
        hooks.check()
        replace_owned_dir(dest, staging)
    except Exception:
        try:
            discard_staging(staging, dest)
        except SafeIOError:
            pass
        raise
    result["dest"] = str(dest)
    hooks.emit(stage="refresh", message="正在刷新图标缓存…")
    result["refresh"] = _refresh_from_snapshot(snapshot, theme_dir=dest, allow_bounce=bool(snapshot.get("allow_bounce")))
    return result


def _run_uninstall(snapshot: dict[str, Any], hooks: ExportHooks) -> dict[str, Any]:
    hooks.emit(stage="export", message="正在卸载本工具主题…")
    msgs: list[str] = []
    dest = Path(snapshot["dest_dir"])
    build = Path(snapshot["build_dir"])
    hooks.check()
    if dest.exists():
        msgs.append(uninstall_owned_theme(dest))
    hooks.check()
    if build.exists():
        try:
            msgs.append(uninstall_owned_theme(build))
        except ExportError as exc:
            msgs.append(str(exc))
    refresh = None
    if msgs:
        hooks.emit(stage="refresh", message="正在刷新图标缓存…")
        refresh = _refresh_from_snapshot(snapshot, theme_dir=None, allow_bounce=False, just_uninstalled=True)
    return {"ok": True, "kind": "uninstall", "messages": msgs, "refresh": refresh, "dest": str(dest)}


def _refresh_from_snapshot(
    snapshot: dict[str, Any],
    *,
    theme_dir: Path | None,
    allow_bounce: bool,
    just_uninstalled: bool = False,
) -> dict[str, Any]:
    hooks: RefreshHooks = snapshot["refresh_hooks"]
    cache_home = snapshot.get("cache_home")
    cache_path = Path(cache_home) if cache_home else None
    try:
        result = refresh_desktop_icons(
            current_theme=snapshot["current_theme"],
            adapt_name=THEME_INTERNAL_NAME,
            theme_dir=theme_dir,
            cache_home=cache_path,
            icon_dirs=snapshot["icon_dirs"],
            hooks=hooks,
            allow_theme_bounce=allow_bounce,
            just_uninstalled=just_uninstalled,
        )
        data = result.to_dict()
        log = snapshot.get("refresh_log")
        if log is not None:
            data["log"] = {k: log.get(k) for k in ("signals", "sycoca", "change_icons", "theme_cache")}
        data["pending_icon_changed"] = True
        return data
    except RefreshError as exc:
        return {"ok": False, "error": str(exc), "warnings": [str(exc)], "pending_icon_changed": False}


class ThemeBuildWorker(QObject):
    """Runs run_theme_job on a QThread. Cancel is an Event, checked at safe boundaries."""

    progress = pyqtSignal(str)
    finished = pyqtSignal(str)

    def __init__(self, snapshot: dict[str, Any]) -> None:
        super().__init__()
        self._snapshot = snapshot
        self._cancel = threading.Event()

    def request_cancel(self) -> None:
        self._cancel.set()

    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def _on_progress(self, payload: dict[str, Any]) -> None:
        self.progress.emit(json.dumps(payload, ensure_ascii=False))

    @pyqtSlot()
    def run(self) -> None:
        hooks = ExportHooks(progress=self._on_progress, should_cancel=self.cancelled)
        try:
            result = run_theme_job(self._snapshot, hooks)
        except ExportCancelled as exc:
            result = {"ok": False, "cancelled": True, "error": str(exc), "kind": self._snapshot.get("kind")}
        except (ExportError, MaskError, SafeIOError, OSError) as exc:
            result = {"ok": False, "cancelled": False, "error": str(exc), "kind": self._snapshot.get("kind")}
        except Exception as exc:  # noqa: BLE001 — surface to GUI
            result = {
                "ok": False,
                "cancelled": False,
                "error": str(exc),
                "kind": self._snapshot.get("kind"),
            }
        self.finished.emit(json.dumps(result, ensure_ascii=False))
        thread = QThread.currentThread()
        if thread is not None:
            thread.quit()
