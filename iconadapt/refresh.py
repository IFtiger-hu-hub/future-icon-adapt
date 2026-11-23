"""Refresh Plasma / KDE icon caches after installing or uninstalling this tool's theme.

Primary path does not write kdeglobals. A brief plasma-changeicons bounce is
only used as a fallback when the current theme is already this tool's theme
and its directory still exists. Unit tests must inject hooks and never talk
to the real session bus or plasma-changeicons.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from .constants import THEME_INTERNAL_NAME

# Official KSharedDataCache::deleteCache("icon-cache") removes exactly this file:
# GenericCacheLocation / "icon-cache.kcache"
ICON_CACHE_BASENAME = "icon-cache.kcache"
KICONLOADER_PATH = "/KIconLoader"
KICONLOADER_INTERFACE = "org.kde.KIconLoader"
KICONLOADER_MEMBER = "iconChanged"
# KIconLoader::Desktop .. Dialog  (0 .. LastGroup-1)
KICONLOADER_GROUPS = (0, 1, 2, 3, 4, 5)
FALLBACK_THEMES = ("Papirus", "breeze")
CHANGEICONS_BIN = "/usr/libexec/plasma-changeicons"


class RefreshError(Exception):
    pass


@dataclass
class RefreshHooks:
    """Injected side effects. Leave a hook as None to skip that step.

    Tests must pass fake emit / sycoca / change_icons implementations and
    must not point change_icons at the real plasma-changeicons binary.
    """

    emit_icon_changed: Callable[[int], None] | None = None
    run_kbuildsycoca: Callable[[], None] | None = None
    change_icons: Callable[[str], None] | None = None
    update_theme_cache: Callable[[Path], None] | None = None
    theme_dir_exists: Callable[[str], bool] | None = None


@dataclass
class RefreshResult:
    cache_cleared: bool = False
    cache_path: str = ""
    cache_skipped_reason: str = ""
    signals: list[int] = field(default_factory=list)
    sycoca: bool = False
    index_touched: bool = False
    theme_cache_updated: bool = False
    bounced: bool = False
    bounce_via: str = ""
    restored_theme: str = ""
    skipped_bounce_reason: str = ""
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "cache_cleared": self.cache_cleared,
            "cache_path": self.cache_path,
            "cache_skipped_reason": self.cache_skipped_reason,
            "signals": list(self.signals),
            "sycoca": self.sycoca,
            "index_touched": self.index_touched,
            "theme_cache_updated": self.theme_cache_updated,
            "bounced": self.bounced,
            "bounce_via": self.bounce_via,
            "restored_theme": self.restored_theme,
            "skipped_bounce_reason": self.skipped_bounce_reason,
            "warnings": list(self.warnings),
        }


def generic_cache_home(cache_home: Path | None = None) -> Path:
    """Same rule as QStandardPaths::GenericCacheLocation / KSharedDataCache."""
    if cache_home is not None:
        return Path(cache_home)
    raw = os.environ.get("XDG_CACHE_HOME", "").strip()
    if raw:
        return Path(raw)
    return Path.home() / ".cache"


def icon_cache_path(cache_home: Path | None = None) -> Path:
    return generic_cache_home(cache_home) / ICON_CACHE_BASENAME


def safe_theme_name(name: str) -> str:
    """Reject values that would be treated as flags or paths (e.g. --help)."""
    value = (name or "").strip()
    if not value:
        raise RefreshError("主题名为空，拒绝执行切换")
    if value.startswith("-") or "/" in value or "\\" in value or "\x00" in value:
        raise RefreshError(f"拒绝不安全的主题名：{name}")
    if any(ch in value for ch in " \t\n,;"):
        raise RefreshError(f"拒绝不安全的主题名：{name}")
    return value


def clear_icon_shared_cache(cache_home: Path | None = None, *, path: Path | None = None) -> dict[str, Any]:
    """Unlink only KSharedDataCache's icon-cache.kcache. Never rmtree or glob-delete."""
    target = Path(path) if path is not None else icon_cache_path(cache_home)
    if target.name != ICON_CACHE_BASENAME:
        raise RefreshError(f"拒绝删除非 icon-cache 文件：{target}")
    # Official deleteCache unlinks the file (does not truncate) to avoid SIGBUS
    # for processes that still map the inode.
    if target.is_symlink() or target.is_file():
        target.unlink()
        return {"removed": True, "path": str(target)}
    if target.exists():
        raise RefreshError(f"拒绝删除：{target} 不是普通缓存文件")
    return {"removed": False, "path": str(target), "reason": "not present"}


def touch_index_theme(theme_dir: Path) -> bool:
    index = theme_dir / "index.theme"
    if not index.is_file():
        return False
    # Bump mtime only; do not rewrite contents.
    os.utime(index, None)
    return True


def default_update_theme_cache(theme_dir: Path) -> None:
    """Best-effort gtk-update-icon-cache on a single owned theme directory."""
    exe = _which(["gtk-update-icon-cache", "gtk4-update-icon-cache"])
    if not exe:
        return
    subprocess.run(
        [exe, "-f", "-t", str(theme_dir)],
        check=False,
        timeout=30,
        capture_output=True,
    )


def default_run_kbuildsycoca() -> None:
    exe = _which(["kbuildsycoca6"])
    if not exe:
        raise RefreshError("未找到 kbuildsycoca6")
    subprocess.run([exe], check=False, timeout=60, capture_output=True)


def default_emit_icon_changed(group: int) -> None:
    from PyQt6.QtDBus import QDBusConnection, QDBusMessage

    msg = QDBusMessage.createSignal(KICONLOADER_PATH, KICONLOADER_INTERFACE, KICONLOADER_MEMBER)
    msg.setArguments([int(group)])
    if not QDBusConnection.sessionBus().send(msg):
        raise RefreshError("无法发送 iconChanged 信号")


def default_change_icons(theme: str) -> None:
    name = safe_theme_name(theme)
    exe = CHANGEICONS_BIN if Path(CHANGEICONS_BIN).is_file() else (_which(["plasma-changeicons"]) or "")
    if not exe:
        raise RefreshError("未找到 plasma-changeicons")
    completed = subprocess.run(
        [exe, name],
        check=False,
        timeout=30,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        err = (completed.stderr or completed.stdout or "").strip()
        raise RefreshError(f"切换图标主题失败（{name}）：{err or completed.returncode}")


def _which(names: Iterable[str]) -> str | None:
    from shutil import which

    for name in names:
        found = which(name)
        if found:
            return found
    return None


def theme_installed(name: str, icon_dirs: Iterable[Path]) -> bool:
    try:
        safe_theme_name(name)
    except RefreshError:
        return False
    for base in icon_dirs:
        index = Path(base) / name / "index.theme"
        if index.is_file():
            return True
    return False


def should_bounce(
    current_theme: str,
    *,
    adapt_name: str = THEME_INTERNAL_NAME,
    adapt_dir_exists: bool,
    just_uninstalled: bool = False,
) -> tuple[bool, str]:
    """Bounce only when the live theme is (or just was) this tool's theme and the dir still exists."""
    if current_theme != adapt_name and not (just_uninstalled and current_theme == adapt_name):
        if current_theme != adapt_name:
            return False, "current theme is not the tool theme; will not switch"
    if current_theme != adapt_name:
        return False, "current theme is not the tool theme; will not switch"
    if not adapt_dir_exists:
        # After uninstall the directory is gone; bouncing back to a missing theme is harmful.
        return False, "tool theme directory is gone; skip bounce to avoid switching back to a missing theme"
    return True, ""


def bounce_theme(
    current_theme: str,
    *,
    change_icons: Callable[[str], None],
    theme_exists: Callable[[str], bool],
    fallbacks: Iterable[str] = FALLBACK_THEMES,
) -> dict[str, Any]:
    """Switch to an installed fallback, then always switch back to current_theme.

    Any failure after leaving current_theme retries the restore. The caller
    must not swallow a final restore failure — the user must not be left on
    the fallback.
    """
    origin = safe_theme_name(current_theme)
    alt = None
    for name in fallbacks:
        if name == origin:
            continue
        try:
            safe_theme_name(name)
        except RefreshError:
            continue
        if theme_exists(name):
            alt = name
            break
    if not alt:
        return {"bounced": False, "reason": "no installed fallback theme"}

    try:
        change_icons(alt)
    except Exception as exc:
        # Never left, or left partially — force origin.
        try:
            change_icons(origin)
        except Exception as restore_exc:
            raise RefreshError(
                f"切换到备用主题失败，且恢复原主题「{origin}」也不成功。请立刻在系统设置中选回原主题。"
            ) from restore_exc
        return {"bounced": False, "reason": f"failed to switch to fallback {alt}: {exc}", "restored": origin}

    last_exc: Exception | None = None
    for _ in range(3):
        try:
            change_icons(origin)
            return {"bounced": True, "via": alt, "restored": origin}
        except Exception as exc:
            last_exc = exc
    raise RefreshError(
        f"已暂时切到「{alt}」，但未能切回「{origin}」。请立刻在系统设置中选回「{origin}」。"
    ) from last_exc


def refresh_desktop_icons(
    *,
    current_theme: str,
    adapt_name: str = THEME_INTERNAL_NAME,
    theme_dir: Path | None = None,
    cache_home: Path | None = None,
    icon_dirs: Iterable[Path] | None = None,
    hooks: RefreshHooks | None = None,
    allow_theme_bounce: bool = False,
    just_uninstalled: bool = False,
) -> RefreshResult:
    """Primary refresh: owned index touch, icon-cache unlink, iconChanged, kbuildsycoca.

    Optional bounce is off unless allow_theme_bounce is True and should_bounce agrees.
    """
    hooks = hooks or RefreshHooks()
    result = RefreshResult()
    icon_dirs = list(icon_dirs or [])

    def exists(name: str) -> bool:
        if hooks.theme_dir_exists:
            return hooks.theme_dir_exists(name)
        return theme_installed(name, icon_dirs)

    # 1. Touch / update caches inside the owned theme dir only.
    if theme_dir is not None and theme_dir.is_dir():
        try:
            result.index_touched = touch_index_theme(theme_dir)
        except OSError as exc:
            result.warnings.append(f"无法更新 index.theme 时间戳：{exc}")
        if hooks.update_theme_cache:
            try:
                hooks.update_theme_cache(theme_dir)
                result.theme_cache_updated = True
            except Exception as exc:
                result.warnings.append(f"更新 icon-theme.cache 失败：{exc}")

    # 2. KSharedDataCache::deleteCache("icon-cache")
    try:
        cleared = clear_icon_shared_cache(cache_home)
        result.cache_path = cleared.get("path", "")
        result.cache_cleared = bool(cleared.get("removed"))
        result.cache_skipped_reason = cleared.get("reason", "")
    except RefreshError as exc:
        result.warnings.append(str(exc))

    # 3. Broadcast iconChanged (all official groups, matching emitChange loop)
    if hooks.emit_icon_changed:
        for group in KICONLOADER_GROUPS:
            try:
                hooks.emit_icon_changed(group)
                result.signals.append(group)
            except Exception as exc:
                result.warnings.append(f"发送 iconChanged({group}) 失败：{exc}")
                break

    # 4. rebuildKSycoca
    if hooks.run_kbuildsycoca:
        try:
            hooks.run_kbuildsycoca()
            result.sycoca = True
        except Exception as exc:
            result.warnings.append(f"kbuildsycoca6 失败：{exc}")

    # 5. Optional fallback bounce — never if current theme is some other pack.
    adapt_dir_exists = theme_dir is not None and theme_dir.is_dir()
    if not adapt_dir_exists:
        adapt_dir_exists = exists(adapt_name)
    ok_bounce, why = should_bounce(
        current_theme,
        adapt_name=adapt_name,
        adapt_dir_exists=adapt_dir_exists,
        just_uninstalled=just_uninstalled,
    )
    if not allow_theme_bounce:
        result.skipped_bounce_reason = why or "bounce not enabled for this operation"
        return result
    if not ok_bounce:
        result.skipped_bounce_reason = why
        return result
    if hooks.change_icons is None:
        result.skipped_bounce_reason = "no change-icons hook (refusing to call the real session from tests)"
        return result
    try:
        bounced = bounce_theme(
            current_theme,
            change_icons=hooks.change_icons,
            theme_exists=exists,
        )
    except RefreshError:
        raise
    if bounced.get("bounced"):
        result.bounced = True
        result.bounce_via = str(bounced.get("via") or "")
        result.restored_theme = str(bounced.get("restored") or current_theme)
    else:
        result.skipped_bounce_reason = str(bounced.get("reason") or "bounce skipped")
        if bounced.get("restored"):
            result.restored_theme = str(bounced["restored"])
            result.warnings.append(result.skipped_bounce_reason)
    return result


def production_hooks() -> RefreshHooks:
    """Hooks for a real user session. Do not use this inside unit tests."""
    return RefreshHooks(
        emit_icon_changed=default_emit_icon_changed,
        run_kbuildsycoca=default_run_kbuildsycoca,
        change_icons=default_change_icons,
        update_theme_cache=default_update_theme_cache,
    )


def isolated_hooks(log: dict[str, Any] | None = None) -> RefreshHooks:
    """Safe hooks for tests and isolated backends: record only, never touch the session."""
    log = log if log is not None else {}
    log.setdefault("signals", [])
    log.setdefault("sycoca", 0)
    log.setdefault("change_icons", [])
    log.setdefault("theme_cache", [])

    def emit(group: int) -> None:
        log["signals"].append(int(group))

    def sycoca() -> None:
        log["sycoca"] += 1

    def cache(theme_dir: Path) -> None:
        log["theme_cache"].append(str(theme_dir))

    return RefreshHooks(
        emit_icon_changed=emit,
        run_kbuildsycoca=sycoca,
        change_icons=None,  # bounce stays off unless a test supplies a fake
        update_theme_cache=cache,
    )
