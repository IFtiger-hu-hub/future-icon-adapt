"""Describe whether an icon source path is actually usable.

Never follows a dangling symlink with Path.resolve() for error text:
that would report the missing target (search.svg) instead of the
broken link the theme actually ships (kerry.svg → search.svg).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


REASON_OK = "ok"
REASON_MISSING = "missing"
REASON_DANGLING = "dangling-symlink"
REASON_NOT_FILE = "not-a-file"
REASON_UNREADABLE = "unreadable"


@dataclass
class SourceStatus:
    path: Path
    ok: bool
    reason: str
    link_target: str | None = None
    resolved: Path | None = None
    display: str = ""
    detail: str = ""

    def to_dict(self) -> dict:
        return {
            "path": str(self.path),
            "ok": self.ok,
            "reason": self.reason,
            "link_target": self.link_target or "",
            "resolved": str(self.resolved) if self.resolved else "",
            "display": self.display,
            "detail": self.detail,
        }


def describe_source(path: Path | str | None) -> SourceStatus:
    """Inspect a path without requiring the target of a broken symlink."""
    if path is None or str(path) == "":
        return SourceStatus(
            path=Path(""),
            ok=False,
            reason=REASON_MISSING,
            display="没有指定源文件",
            detail="没有指定源文件",
        )
    src = Path(path)
    is_link = False
    try:
        is_link = src.is_symlink()
    except OSError as exc:
        return SourceStatus(
            path=src,
            ok=False,
            reason=REASON_UNREADABLE,
            display=f"无法读取：{src}（{exc}）",
            detail=str(exc),
        )
    link_target = None
    if is_link:
        try:
            link_target = os.readlink(src)
        except OSError as exc:
            return SourceStatus(
                path=src,
                ok=False,
                reason=REASON_UNREADABLE,
                display=f"无法读取符号链接：{src}（{exc}）",
                detail=str(exc),
            )
        # Path.exists() follows the link; False means dangling (or missing mid-chain).
        try:
            target_ok = src.exists()
        except OSError:
            target_ok = False
        if not target_ok:
            return SourceStatus(
                path=src,
                ok=False,
                reason=REASON_DANGLING,
                link_target=link_target,
                resolved=_unstrict_resolve(src),
                display=f"失效的符号链接：{src.name} → {link_target}（目标不存在：{src.parent / link_target}）",
                detail=f"{src} → {link_target}",
            )
    try:
        if src.is_file():
            resolved = None
            try:
                resolved = src.resolve()
            except OSError:
                resolved = src
            return SourceStatus(
                path=src,
                ok=True,
                reason=REASON_OK,
                link_target=link_target,
                resolved=resolved,
                display=str(src),
            )
        if src.exists():
            return SourceStatus(
                path=src,
                ok=False,
                reason=REASON_NOT_FILE,
                link_target=link_target,
                display=f"源路径不是普通文件：{src}",
                detail=str(src),
            )
    except OSError as exc:
        return SourceStatus(
            path=src,
            ok=False,
            reason=REASON_UNREADABLE,
            link_target=link_target,
            display=f"无法读取源图标：{src}（{exc}）",
            detail=str(exc),
        )
    return SourceStatus(
        path=src,
        ok=False,
        reason=REASON_MISSING,
        display=f"源图标不存在：{src}",
        detail=str(src),
    )


def source_is_readable(path: Path | str | None) -> bool:
    return describe_source(path).ok


def _unstrict_resolve(path: Path) -> Path | None:
    try:
        return path.resolve(strict=False)
    except OSError:
        return None


__all__ = [
    "REASON_DANGLING",
    "REASON_MISSING",
    "REASON_NOT_FILE",
    "REASON_OK",
    "REASON_UNREADABLE",
    "SourceStatus",
    "describe_source",
    "source_is_readable",
]
