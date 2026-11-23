"""Export a user-level standard icon theme.

Legacy mode writes an application-icon overlay and leaves other contexts to Inherits.
Composed mode copies the modules the user explicitly picked from installed packs.
"""

from __future__ import annotations

import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from .constants import (
    MODULE_APPS,
    MODULE_IDS,
    MODULE_SOURCE_SYSTEM,
    OWNED_MARKER,
    PROVENANCE_FILENAME,
    THEME_DISPLAY_NAME,
    THEME_INTERNAL_NAME,
    TOOL_ID,
    TOOL_VERSION,
)
from .desktop import DesktopApp
from .lookup import Candidate, collect_all_sizes, lookup_in_theme_files
from .mask import (
    MASK_NOTE,
    MaskError,
    MaskSettings,
    apply_mask_to_file,
    last_raster_report,
    planned_sizes,
    provenance_mask_block,
    validate_settings,
)
from .rendercache import begin_render_session, cache_stats, end_render_session
from .modules import (
    files_by_name,
    has_explicit_module_sources,
    is_system_source,
    list_module_files,
    module_title,
    normalize_module_map,
    pick_export_svg_source,
    specs_by_rel,
)
from .persist import atomic_write_json
from .safeio import (
    SafeIOError,
    assert_can_replace,
    create_owned_staging,
    discard_staging,
    is_tool_owned,
    replace_owned_dir,
    write_owned_marker,
)
from .sourcecheck import describe_source, source_is_readable
from .strategy import StrategyStatus, is_feishu_related
from .svgutil import (
    flush_svg_log_summary,
    inspect_svg_file,
    reset_svg_log_dedup,
    source_issue_summary,
    svg_log_stats,
)
from .theme import IconTheme, ThemeDirSpec


class ExportError(Exception):
    pass


class ExportCancelled(ExportError):
    """Raised when the user cancels at a safe point, before dest is replaced."""

    def __init__(self, message: str = "已取消") -> None:
        super().__init__(message)


class ExportHooks:
    """Optional progress / cancel hooks. Safe to call from a worker thread."""

    def __init__(
        self,
        progress: Callable[[dict[str, Any]], None] | None = None,
        should_cancel: Callable[[], bool] | None = None,
    ) -> None:
        self.progress = progress
        self.should_cancel = should_cancel

    def check(self) -> None:
        if self.should_cancel and self.should_cancel():
            raise ExportCancelled("已取消")

    def emit(self, **payload: Any) -> None:
        self.check()
        if self.progress:
            self.progress(payload)


def _remember_svg_source_issues(
    bucket: dict[str, dict | None],
    src: Path | str | None,
    *,
    report: dict | None = None,
) -> None:
    """Record static and runtime SVG problems, keyed by the resolved real file.

    Alias paths are attached to that real source so reuse of cached pixels
    still reports every affected name. Does not rewrite the source file.
    """
    if not src:
        return
    path = Path(src)
    status = describe_source(path)
    resolved = str(status.resolved) if status.resolved else str(path)
    alias = str(path)
    row = bucket.get(resolved)
    if resolved not in bucket:
        issues = []
        if path.suffix.lower() in {".svg", ".svgz"} and status.ok:
            issues = inspect_svg_file(path)
        row = {
            "path": resolved,
            "name": Path(resolved).name,
            "issues": [item.to_dict() for item in issues],
            "kinds": sorted({item.kind for item in issues}),
            "aliases": [],
            "sizes": {},
            "downgrades": {},
        }
        bucket[resolved] = row
    row = bucket.get(resolved)
    if not row:
        return
    if alias and alias not in row["aliases"]:
        row["aliases"].append(alias)
    if not report:
        return
    existing = {(item.get("kind"), item.get("detail")) for item in row.get("issues") or []}
    for item in report.get("issues") or []:
        key = (item.get("kind"), item.get("detail"))
        if key in existing:
            continue
        row["issues"].append(item)
        existing.add(key)
    row["kinds"] = sorted({item.get("kind") for item in row["issues"] if item.get("kind")})
    size = report.get("size")
    if size:
        marker = report.get("process") or report.get("downgrade") or ""
        row["sizes"][str(size)] = marker
        if report.get("downgrade"):
            down = str(report.get("downgrade"))
            if report.get("hires_size"):
                down = f"{down}:{report['hires_size']}"
            row["downgrades"][str(size)] = down
    if report.get("font_adjusted"):
        row["font_adjusted"] = True


def _finalize_source_issues(provenance: dict, bucket: dict[str, dict | None]) -> None:
    rows = []
    for row in bucket.values():
        if not row:
            continue
        if not row.get("issues") and not row.get("downgrades") and not row.get("font_adjusted"):
            continue
        row["aliases"] = sorted(row.get("aliases") or [])
        row["alias_count"] = len(row["aliases"])
        rows.append(row)
    rows.sort(key=lambda r: (r.get("name") or "", r.get("path") or ""))
    # Stable list; full details including aliases stay in provenance.
    provenance["source_issues"] = rows
    provenance["source_issue_summary"] = source_issue_summary(rows)


def ensure_writable_theme_dir(theme_dir: Path, *, overwrite_owned: bool) -> None:
    if theme_dir.exists():
        if is_tool_owned(theme_dir):
            if overwrite_owned:
                _safe_clear_owned(theme_dir)
            else:
                raise ExportError(f"主题目录已存在（本工具所有）。请明确覆盖或换名：{theme_dir}")
        else:
            raise ExportError(f"拒绝覆盖已有同名目录（非本工具所有）：{theme_dir}")
    theme_dir.mkdir(parents=True, exist_ok=True)
    write_owned_marker(theme_dir)


def _index_theme_text(
    inherits: list[str],
    directories: list[str],
    specs: dict[str, ThemeDirSpec] | None = None,
    *,
    composed: bool = False,
    follows_color_scheme: bool = False,
) -> str:
    regular: list[str] = []
    scaled: list[str] = []
    for rel in directories:
        spec = (specs or {}).get(rel)
        scale = int(spec.scale) if spec and spec.scale else 1
        if scale != 1:
            scaled.append(rel)
        else:
            regular.append(rel)
    comment = (
        "User-composed icon theme generated by future-icon-adapt. Contexts not copied fall back via Inherits."
        if composed
        else "User-built application icon overlay generated by future-icon-adapt. Actions/status come from Inherits."
    )
    follows = "true" if follows_color_scheme else "false"
    lines = [
        "[Icon Theme]",
        f"Name={THEME_DISPLAY_NAME}",
        f"Comment={comment}",
        f"Inherits={','.join(inherits)}",
        "Example=folder",
        f"FollowsColorScheme={follows}",
        "DesktopDefault=48",
        "DesktopSizes=16,22,24,32,48,64",
        "ToolbarDefault=22",
        "MainToolbarDefault=22",
        "SmallDefault=16",
        "PanelDefault=22",
        "DialogDefault=48",
        "KDE-Extensions=.svg,.png,.svgz,.xpm",
        f"Directories={','.join(regular)}",
    ]
    if scaled:
        lines.append(f"ScaledDirectories={','.join(scaled)}")
    lines.append("")
    for rel in directories:
        spec = (specs or {}).get(rel)
        if spec is not None:
            size = int(spec.size or 0) or 48
            typ = spec.typ or "Fixed"
            lo = int(spec.min_size or size)
            hi = int(spec.max_size or size)
            ctx = spec.context or _spec_for_rel(rel)[4]
            scale = int(spec.scale or 1) or 1
        else:
            size, typ, lo, hi, ctx = _spec_for_rel(rel)
            scale = 1
        lines.append(f"[{rel}]")
        lines.append(f"Size={size}")
        lines.append(f"Context={ctx}")
        lines.append(f"Type={typ}")
        if scale != 1:
            lines.append(f"Scale={scale}")
        if typ == "Scalable":
            lines.append(f"MinSize={lo}")
            lines.append(f"MaxSize={hi}")
        elif typ == "Threshold":
            lines.append("Threshold=2")
        lines.append("")
    return "\n".join(lines) + "\n"


def _spec_for_rel(rel: str) -> tuple[int, str, int, int, str]:
    head = rel.split("/", 1)[0]
    ctx = "Applications"
    if head == "scalable":
        return 48, "Scalable", 1, 512, ctx
    if "x" in head:
        try:
            size = int(head.split("x", 1)[0])
            return size, "Fixed", size, size, ctx
        except ValueError:
            pass
    if head.isdigit():
        size = int(head)
        typ = "Threshold" if size in {16, 22, 24} else "Fixed"
        return size, typ, size, size, ctx
    return 48, "Scalable", 1, 512, ctx


def pick_default_files(
    icon_name: str,
    themes: dict[str, IconTheme],
    status: StrategyStatus,
) -> tuple[str, list] | None:
    """Exact-name sources for a default (non-manual) icon. Tela first, Colloid for Feishu."""
    if not icon_name:
        return None
    if status.tela and status.tela in themes:
        hits = collect_all_sizes(icon_name, themes[status.tela])
        if hits:
            return status.tela, hits
    if status.colloid and is_feishu_related(icon_name) and status.colloid in themes:
        hits = collect_all_sizes(icon_name, themes[status.colloid])
        if hits:
            return status.colloid, hits
        # exact only — do not try alias names here
    return None



def format_source_error(*, desktop_id: str = "", module: str = "", source_theme: str = "", status) -> str:
    bits = []
    if desktop_id:
        bits.append(f"应用 {desktop_id}")
    if module:
        bits.append(f"模块 {module}")
    if source_theme:
        bits.append(f"来源 {source_theme}")
    where = "，".join(bits) if bits else "源图标"
    return f"{where}：{status.display}"


def preflight_manual_selections(
    selections: dict[str, dict] | None,
) -> list[str]:
    """Hard errors for saved hand picks that no longer point at a readable file."""
    errors: list[str] = []
    for desktop_id, sel in (selections or {}).items():
        path = (sel or {}).get("source_path") or ""
        theme = (sel or {}).get("source_theme") or ""
        if not path:
            # Name-only picks are resolved later from the theme inventory.
            continue
        status = describe_source(path)
        if not status.ok:
            errors.append(
                format_source_error(desktop_id=desktop_id, source_theme=theme, status=status)
                + "。手选仍然保留，请重新选用一个存在的图标，或撤销这次选用。"
            )
    return errors



def _readable_sources(app_files):
    """Split icon candidates into usable SVGs/rasters and unreadable entries."""
    svgs: list[Path] = []
    rasters: dict[int, Path] = {}
    unread: list[tuple[object, object]] = []
    any_svg_name = False
    for cand in app_files:
        path = Path(cand.path)
        if path.suffix.lower() in {".svg", ".svgz"}:
            any_svg_name = True
        status = describe_source(path)
        if not status.ok:
            unread.append((cand, status))
            continue
        if path.suffix.lower() in {".svg", ".svgz"}:
            svgs.append(path)
        else:
            size = int(getattr(cand, "size", 0) or 0)
            if size:
                rasters[size] = path
    return svgs, rasters, unread, any_svg_name


def _unreadable_export_message(*, desktop_id: str, source_theme: str, icon_name: str, unread, manual: bool) -> str:
    details = "；".join(status.display for _cand, status in unread)
    where = format_source_error(
        desktop_id=desktop_id,
        module="apps",
        source_theme=source_theme,
        status=unread[0][1],
    ) if unread else f"{icon_name} 无法使用"
    if manual:
        return (
            f"导出已中止，原主题目录未改动。手选图标无法使用（{icon_name}）：{details or where}。"
            "选择仍保留，请重新选用一个存在的图标，或撤销这次选用。"
        )
    return f"模块图标无法使用（{icon_name}）：{details or where}"

def export_theme(
    dest_dir: Path,
    *,
    apps: Iterable[DesktopApp],
    themes: dict[str, IconTheme],
    status: StrategyStatus,
    selections: dict[str, dict],
    overwrite_owned: bool = True,
    theme_internal_name: str = THEME_INTERNAL_NAME,
    mask: MaskSettings | None = None,
    modules: dict[str, str] | None = None,
    hooks: ExportHooks | None = None,
) -> dict[str, Any]:
    """Write a standard icon theme into dest_dir. dest_dir's basename should be the internal name."""
    if dest_dir.name != theme_internal_name:
        # Allow tests to use a dest folder whose name is the internal name.
        pass
    mask_settings = mask or MaskSettings()
    hooks = hooks or ExportHooks()
    hooks.emit(stage="validate", done=0, total=0, message="正在检查设置…")
    # Validate before touching dest, so a bad custom mask never writes a broken theme.
    if mask_settings.enabled:
        try:
            validate_settings(mask_settings)
        except MaskError as exc:
            raise ExportError(f"遮罩无效，已中止导出（未写入主题）：{exc}") from exc

    final_dest = Path(dest_dir)
    try:
        assert_can_replace(final_dest, overwrite_owned=overwrite_owned)
    except SafeIOError as exc:
        raise ExportError(str(exc)) from exc

    hard = preflight_manual_selections(selections)
    if hard:
        raise ExportError(
            "导出已中止，没有改动目标主题目录。" + " ".join(hard)
        )

    staging = create_owned_staging(final_dest)
    reset_svg_log_dedup()
    begin_render_session()
    try:
        if has_explicit_module_sources(modules):
            provenance = _export_composed(
                staging,
                apps=apps,
                themes=themes,
                status=status,
                selections=selections,
                mask_settings=mask_settings,
                modules=modules or {},
                hooks=hooks,
            )
        else:
            provenance = _export_legacy(
                staging,
                apps=apps,
                themes=themes,
                status=status,
                selections=selections,
                mask_settings=mask_settings,
                hooks=hooks,
            )
        provenance["render_cache"] = cache_stats()
        provenance["svg_log"] = svg_log_stats()
        hooks.emit(stage="commit", message="正在写入生成目录…")
        hooks.check()
        replace_owned_dir(final_dest, staging)
        provenance["dest"] = str(final_dest)
        flush_svg_log_summary()
        return provenance
    except Exception:
        try:
            discard_staging(staging, final_dest)
        except SafeIOError:
            pass
        flush_svg_log_summary()
        raise
    finally:
        end_render_session()


def _export_legacy(
    dest_dir: Path,
    *,
    apps: Iterable[DesktopApp],
    themes: dict[str, IconTheme],
    status: StrategyStatus,
    selections: dict[str, dict],
    mask_settings: MaskSettings,
    hooks: ExportHooks | None = None,
) -> dict[str, Any]:
    hooks = hooks or ExportHooks()
    hooks.emit(stage="export", done=0, total=0, message="正在导出图标…")

    provenance: dict[str, Any] = {
        "tool": TOOL_ID,
        "version": TOOL_VERSION,
        "theme": THEME_INTERNAL_NAME,
        "inherits": status.inherit,
        "strategy_missing": status.missing,
        "strategy_notes": status.notes,
        "icons": [],
        "skipped": [],
        "source_issues": [],
        "source_issue_summary": "",
        "mask": provenance_mask_block(mask_settings),
    }
    source_issue_bucket: dict[str, dict | None] = {}
    if mask_settings.enabled:
        provenance["strategy_notes"] = list(status.notes) + [MASK_NOTE]
    used_dirs: dict[str, None] = {}

    def _apps_rel(rel: str) -> bool:
        return "apps" in rel.replace("\\", "/").lower().split("/")

    def place(icon_name: str, source_theme: str, files, desktop_id: str, manual: bool) -> int:
        count = 0
        app_files = [cand for cand in files if _apps_rel(getattr(cand, "rel_dir", "") or "")]
        if mask_settings.enabled:
            try:
                count = _place_masked(icon_name, app_files, desktop_id=desktop_id, source_theme=source_theme, manual=manual)
            except MaskError as exc:
                raise ExportError(
                    f"应用遮罩失败，已中止导出，原主题目录未改动：{exc}"
                ) from exc
        else:
            for cand in app_files:
                rel = cand.rel_dir
                target_dir = dest_dir / rel
                target_dir.mkdir(parents=True, exist_ok=True)
                dest = target_dir / cand.path.name
                # Same icon name may appear for many apps; reuse symlink
                if dest.exists() or dest.is_symlink():
                    used_dirs[rel] = None
                    count += 1
                    continue
                try:
                    os.symlink(cand.path.resolve(), dest)
                except OSError:
                    shutil.copy2(cand.path, dest)
                _remember_svg_source_issues(source_issue_bucket, cand.path)
                used_dirs[rel] = None
                count += 1
        if count:
            provenance["icons"].append(
                {
                    "desktop_id": desktop_id,
                    "icon_name": icon_name,
                    "source_theme": source_theme,
                    "files": count,
                    "manual": manual,
                    "masked": bool(mask_settings.enabled),
                    "rasterized": bool(mask_settings.enabled),
                }
            )
        return count

    def _place_masked(icon_name: str, app_files, *, desktop_id: str = "", source_theme: str = "", manual: bool = False) -> int:
        if not app_files:
            return 0
        svgs, raster_by_size, unread, any_svg_name = _readable_sources(app_files)
        if unread:
            for _cand, status in unread:
                provenance["skipped"].append(
                    {
                        "icon_name": icon_name,
                        "desktop_id": desktop_id,
                        "source_theme": source_theme,
                        "reason": status.display,
                    }
                )
        if not svgs and not raster_by_size:
            if manual:
                raise ExportError(
                    _unreadable_export_message(
                        desktop_id=desktop_id,
                        source_theme=source_theme,
                        icon_name=icon_name,
                        unread=unread,
                        manual=True,
                    )
                )
            return 0
        has_svg = bool(svgs) or any_svg_name
        svg_src = pick_export_svg_source(svgs, app_files)
        sizes = planned_sizes(app_files, has_svg=bool(svgs))
        written = 0
        for size in sizes:
            if svg_src is not None:
                src = svg_src
            else:
                src = raster_by_size.get(size) or (
                    raster_by_size[max(raster_by_size)] if raster_by_size else Path(app_files[0].path)
                )
            rel = f"{size}x{size}/apps"
            dest = dest_dir / rel / f"{icon_name}.png"
            if dest.exists() or dest.is_symlink():
                used_dirs[rel] = None
                written += 1
                continue
            _remember_svg_source_issues(source_issue_bucket, src)
            apply_mask_to_file(src, dest, size, mask_settings)
            _remember_svg_source_issues(source_issue_bucket, src, report=last_raster_report())
            # Safety: never a symlink to the original theme file when masked.
            if dest.is_symlink():
                raise ExportError(f"遮罩导出不应写为符号链接：{dest}")
            used_dirs[rel] = None
            written += 1
        return written

    for app in apps:
        if not app.launchable:
            continue
        did = app.desktop_id
        sel = selections.get(did)
        if sel and sel.get("source_theme") and sel.get("icon_name"):
            tname = sel["source_theme"]
            iname = sel["icon_name"]
            theme = themes.get(tname)
            if not theme:
                provenance["skipped"].append(
                    {"desktop_id": did, "reason": f"手选主题未安装：{tname}"}
                )
                continue
            files = collect_all_sizes(iname, theme)
            if not files:
                # single path fallback
                src = Path(sel.get("source_path") or "")
                if describe_source(src).ok:
                    rel = "scalable/apps" if src.suffix.lower() in {".svg", ".svgz"} else "48x48/apps"
                    from types import SimpleNamespace

                    files = [SimpleNamespace(rel_dir=rel, path=src)]
                else:
                    provenance["skipped"].append(
                        {"desktop_id": did, "reason": f"手选图标在主题中找不到：{tname}/{iname}"}
                    )
                    continue
            place(iname, tname, files, did, True)
            hooks.emit(stage="render" if mask_settings.enabled else "export", current=iname, message="正在导出应用图标…")
            continue

        # Defaults: only exact desktop Icon= name, and only if it is a name (not path)
        icon = app.icon
        if not icon or app.icon_kind == "absolute":
            provenance["skipped"].append(
                {
                    "desktop_id": did,
                    "reason": "绝对路径或空 Icon，默认不写入主题（可手选后导出）",
                }
            )
            continue
        picked = pick_default_files(icon, themes, status)
        if not picked:
            provenance["skipped"].append(
                {"desktop_id": did, "reason": "无默认拷贝（将依赖 Inherits 回退）"}
            )
            continue
        src_theme, files = picked
        place(icon, src_theme, files, did, False)
        hooks.emit(stage="render" if mask_settings.enabled else "export", current=icon, message="正在导出应用图标…")

    directories = sorted(used_dirs.keys(), key=lambda r: (r != "scalable/apps", r))
    if not directories:
        # still a valid empty overlay that only inherits
        directories = ["scalable/apps"]
        (dest_dir / "scalable" / "apps").mkdir(parents=True, exist_ok=True)
    (dest_dir / "index.theme").write_text(
        _index_theme_text(status.inherit, directories), encoding="utf-8"
    )
    _finalize_source_issues(provenance, source_issue_bucket)
    atomic_write_json(dest_dir / PROVENANCE_FILENAME, provenance)
    write_owned_marker(
        dest_dir,
        extra={"inherits": status.inherit, "icon_records": len(provenance["icons"])},
    )
    provenance["dest"] = str(dest_dir)
    provenance["directories"] = directories
    return provenance




def _is_under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False


def safe_place_file(src: Path, dest: Path, dest_root: Path) -> str:
    """Install src at dest as a symlink to the resolved file, or a copy.

    Never creates a link into dest_root (the generated theme) and never
    follows a source that itself lives inside the output. Returns one of
    symlink, copy, exists, skip, skip-output.
    """
    try:
        if not src.exists():
            return "skip"
        real = src.resolve()
    except OSError:
        return "skip"
    if not real.is_file():
        return "skip"
    if _is_under(real, dest_root):
        return "skip-output"
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() or dest.is_symlink():
        return "exists"
    try:
        os.symlink(real, dest)
        return "symlink"
    except OSError:
        try:
            shutil.copy2(real, dest)
            return "copy"
        except OSError:
            return "skip"


def _apps_rel(rel: str) -> bool:
    return "apps" in rel.replace("\\", "/").lower().split("/") or "applications" in rel.replace(
        "\\", "/"
    ).lower().split("/")


def _fixed_apps_spec(rel: str, size: int) -> ThemeDirSpec:
    return ThemeDirSpec(
        rel=rel,
        size=size,
        scale=1,
        context="Applications",
        typ="Fixed",
        min_size=size,
        max_size=size,
        threshold=2,
    )


def _export_composed(
    dest_dir: Path,
    *,
    apps: Iterable[DesktopApp],
    themes: dict[str, IconTheme],
    status: StrategyStatus,
    selections: dict[str, dict],
    mask_settings: MaskSettings,
    modules: dict[str, str],
    hooks: ExportHooks | None = None,
) -> dict[str, Any]:
    """Copy explicitly chosen module files into an independent theme."""
    mapping = normalize_module_map(modules)
    hooks = hooks or ExportHooks()
    for mid, src in mapping.items():
        if is_system_source(src):
            continue
        if src == THEME_INTERNAL_NAME:
            raise ExportError(f"不能把本工具生成的主题再当作「{module_title(mid)}」的来源，避免循环引用。")
        theme = themes.get(src)
        if theme is None or not theme.valid:
            raise ExportError(f"「{module_title(mid)}」选了未安装的图标包：{src}")

    follows_color = False
    for mid, src in mapping.items():
        if is_system_source(src):
            continue
        theme = themes.get(src)
        if theme is not None and theme.follows_color_scheme and mid != MODULE_APPS:
            follows_color = True

    provenance: dict[str, Any] = {
        "tool": TOOL_ID,
        "version": TOOL_VERSION,
        "theme": THEME_INTERNAL_NAME,
        "mode": "composed",
        "inherits": status.inherit,
        "strategy_missing": status.missing,
        "strategy_notes": list(status.notes),
        "modules": {mid: mapping[mid] for mid in MODULE_IDS},
        "icons": [],
        "module_files": [],
        "skipped": [],
        "conflicts": [],
        "empty_modules": [],
        "source_issues": [],
        "source_issue_summary": "",
        "mask": provenance_mask_block(mask_settings),
    }
    source_issue_bucket: dict[str, dict | None] = {}
    if mask_settings.enabled:
        provenance["strategy_notes"] = list(status.notes) + [MASK_NOTE]

    used_dirs: dict[str, ThemeDirSpec | None] = {}
    dest_root = dest_dir
    progress_state = {"done": 0, "total": 0}

    def _bump(n: int = 1, *, stage: str = "export", current: str = "", message: str = "") -> None:
        hooks.check()
        progress_state["done"] += n
        hooks.emit(
            stage=stage,
            done=progress_state["done"],
            total=progress_state["total"],
            current=current,
            message=message or "正在导出图标…",
        )

    def remember_dir(rel: str, spec: ThemeDirSpec | None = None) -> None:
        if rel not in used_dirs or used_dirs[rel] is None:
            used_dirs[rel] = spec

    def occupy_conflict(dest: Path, *, icon_name: str, source_theme: str, module: str, rel: str) -> bool:
        if dest.exists() or dest.is_symlink():
            provenance["conflicts"].append(
                {
                    "icon_name": icon_name,
                    "source_theme": source_theme,
                    "module": module,
                    "rel": rel,
                    "reason": "同路径已有更高优先级的文件，已跳过",
                }
            )
            remember_dir(rel)
            return True
        return False

    def place_masked_files(icon_name: str, file_list: list, source_theme: str, desktop_id: str, manual: bool) -> int:
        app_files = [cand for cand in file_list if _apps_rel(getattr(cand, "rel_dir", "") or "")]
        if not app_files:
            return 0
        svgs, raster_by_size, unread, any_svg_name = _readable_sources(app_files)
        if unread:
            for _cand, status in unread:
                provenance["skipped"].append(
                    {
                        "icon_name": icon_name,
                        "desktop_id": desktop_id,
                        "source_theme": source_theme,
                        "module": MODULE_APPS,
                        "reason": status.display,
                    }
                )
        if not svgs and not raster_by_size:
            if manual:
                raise ExportError(
                    _unreadable_export_message(
                        desktop_id=desktop_id,
                        source_theme=source_theme,
                        icon_name=icon_name,
                        unread=unread,
                        manual=True,
                    )
                )
            return 0
        svg_src = pick_export_svg_source(svgs, app_files)
        sizes = planned_sizes(app_files, has_svg=bool(svgs))
        written = 0
        try:
            for size in sizes:
                if svg_src is not None:
                    src = svg_src
                else:
                    src = raster_by_size.get(size) or (
                        raster_by_size[max(raster_by_size)] if raster_by_size else Path(app_files[0].path)
                    )
                rel = f"{size}x{size}/apps"
                dest = dest_dir / rel / f"{icon_name}.png"
                if occupy_conflict(dest, icon_name=icon_name, source_theme=source_theme, module=MODULE_APPS, rel=rel):
                    written += 1
                    continue
                _remember_svg_source_issues(source_issue_bucket, src)
                apply_mask_to_file(src, dest, size, mask_settings)
                _remember_svg_source_issues(source_issue_bucket, src, report=last_raster_report())
                if dest.is_symlink():
                    raise ExportError(f"遮罩导出不应写为符号链接：{dest}")
                remember_dir(rel, _fixed_apps_spec(rel, size))
                written += 1
        except MaskError as exc:
            raise ExportError(
                f"应用遮罩失败，已中止导出，原主题目录未改动：{exc}"
            ) from exc
        if written:
            provenance["icons"].append(
                {
                    "desktop_id": desktop_id,
                    "icon_name": icon_name,
                    "source_theme": source_theme,
                    "files": written,
                    "manual": manual,
                    "masked": True,
                    "rasterized": True,
                    "module": MODULE_APPS,
                }
            )
        return written

    def place_app_files(icon_name: str, file_list: list, source_theme: str, desktop_id: str, manual: bool) -> int:
        if mask_settings.enabled:
            return place_masked_files(icon_name, file_list, source_theme, desktop_id, manual)
        count = 0
        for cand in file_list:
            rel = getattr(cand, "rel_dir", "") or ""
            if not _apps_rel(rel):
                continue
            dest = dest_dir / rel / Path(cand.path).name
            result = safe_place_file(Path(cand.path), dest, dest_root)
            if result in {"symlink", "copy", "exists"}:
                _remember_svg_source_issues(source_issue_bucket, Path(cand.path))
            if result == "exists":
                occupy_conflict(dest, icon_name=icon_name, source_theme=source_theme, module=MODULE_APPS, rel=rel)
                count += 1
                continue
            if result in {"skip", "skip-output"}:
                src_status = describe_source(Path(cand.path))
                provenance["skipped"].append(
                    {
                        "icon_name": icon_name,
                        "desktop_id": desktop_id,
                        "source_theme": source_theme,
                        "module": MODULE_APPS,
                        "reason": src_status.display if not src_status.ok else "跳过不安全的符号链接或失效来源",
                    }
                )
                continue
            spec = None
            theme = themes.get(source_theme)
            if theme is not None:
                spec = specs_by_rel(theme, MODULE_APPS).get(rel)
            remember_dir(rel, spec)
            count += 1
        if count:
            provenance["icons"].append(
                {
                    "desktop_id": desktop_id,
                    "icon_name": icon_name,
                    "source_theme": source_theme,
                    "files": count,
                    "manual": manual,
                    "masked": False,
                    "rasterized": False,
                    "module": MODULE_APPS,
                }
            )
        return count

    placed_app_names: set[str] = set()

    # Count work units so the UI can show N / total without inventing a percentage early.
    manual_names: set[str] = set()
    for app in apps:
        if not getattr(app, "launchable", True):
            continue
        sel = (selections or {}).get(app.desktop_id) or {}
        if sel.get("source_theme") and sel.get("icon_name"):
            manual_names.add(str(sel["icon_name"]))
    total_units = len(manual_names)
    apps_source_for_total = mapping.get(MODULE_APPS, MODULE_SOURCE_SYSTEM)
    if not is_system_source(apps_source_for_total):
        grouped_for_total = files_by_name(themes[apps_source_for_total], MODULE_APPS)
        total_units += sum(1 for name in grouped_for_total if name not in manual_names)
    for mid in MODULE_IDS:
        if mid == MODULE_APPS:
            continue
        src_name = mapping.get(mid, MODULE_SOURCE_SYSTEM)
        if is_system_source(src_name):
            continue
        total_units += len(list_module_files(themes[src_name], mid))
    progress_state["total"] = total_units
    hooks.emit(
        stage="export",
        done=0,
        total=total_units,
        message=f"正在导出图标（共 {total_units} 项）…" if total_units else "正在导出图标…",
    )

    for app in apps:
        if not app.launchable:
            continue
        did = app.desktop_id
        sel = selections.get(did)
        if not (sel and sel.get("source_theme") and sel.get("icon_name")):
            continue
        tname = sel["source_theme"]
        iname = sel["icon_name"]
        theme = themes.get(tname)
        if not theme:
            provenance["skipped"].append({"desktop_id": did, "reason": f"手选主题未安装：{tname}"})
            continue
        files = collect_all_sizes(iname, theme)
        if not files:
            src = Path(sel.get("source_path") or "")
            if describe_source(src).ok:
                from types import SimpleNamespace

                rel = "scalable/apps" if src.suffix.lower() in {".svg", ".svgz"} else "48x48/apps"
                files = [SimpleNamespace(rel_dir=rel, path=src, size=48)]
            else:
                provenance["skipped"].append(
                    {"desktop_id": did, "reason": f"手选图标在主题中找不到：{tname}/{iname}"}
                )
                continue
        if place_app_files(iname, files, tname, did, True):
            placed_app_names.add(iname)
        _bump(1, stage="render" if mask_settings.enabled else "export", current=iname, message="正在导出应用图标…")

    apps_source = mapping.get(MODULE_APPS, MODULE_SOURCE_SYSTEM)
    if not is_system_source(apps_source):
        theme = themes[apps_source]
        grouped = files_by_name(theme, MODULE_APPS)
        if not grouped:
            provenance["empty_modules"].append({"module": MODULE_APPS, "source_theme": apps_source})
        else:
            written_names = 0
            for name, group in grouped.items():
                if name in placed_app_names:
                    continue
                candidates = [
                    Candidate(
                        theme=apps_source,
                        path=item.path,
                        rel_dir=item.rel_dir,
                        size=item.size,
                        display_name=theme.display_name,
                        typ=getattr(item, "typ", "") or "",
                    )
                    for item in group
                ]
                if place_app_files(name, candidates, apps_source, "", False):
                    placed_app_names.add(name)
                    written_names += 1
                _bump(1, stage="render" if mask_settings.enabled else "export", current=name, message="正在导出应用图标…")
            provenance["module_files"].append(
                {
                    "module": MODULE_APPS,
                    "source_theme": apps_source,
                    "icons": written_names + len([n for n in placed_app_names if n in grouped]),
                    "masked": bool(mask_settings.enabled),
                }
            )

    for mid in MODULE_IDS:
        if mid == MODULE_APPS:
            continue
        src_name = mapping.get(mid, MODULE_SOURCE_SYSTEM)
        if is_system_source(src_name):
            continue
        theme = themes[src_name]
        files = list_module_files(theme, mid)
        if not files:
            provenance["empty_modules"].append({"module": mid, "source_theme": src_name})
            continue
        copied = 0
        skipped = 0
        conflicts = 0
        specs = specs_by_rel(theme, mid)
        seen_dirs: set[str] = set()
        for item in files:
            hooks.check()
            dest = dest_dir / item.rel_dir / item.path.name
            if dest.exists() or dest.is_symlink():
                occupy_conflict(
                    dest,
                    icon_name=item.name,
                    source_theme=src_name,
                    module=mid,
                    rel=item.rel_dir,
                )
                conflicts += 1
                _bump(1, stage="export", current=item.name, message="正在复制模块图标…")
                continue
            result = safe_place_file(item.path, dest, dest_root)
            if result in {"symlink", "copy", "exists"}:
                _remember_svg_source_issues(source_issue_bucket, item.path)
            if result in {"skip", "skip-output"}:
                skipped += 1
                src_status = describe_source(item.path)
                provenance["skipped"].append(
                    {
                        "module": mid,
                        "icon_name": item.name,
                        "source_theme": src_name,
                        "reason": src_status.display if not src_status.ok else "跳过不安全的符号链接或失效来源",
                    }
                )
                _bump(1, stage="export", current=item.name, message="正在复制模块图标…")
                continue
            remember_dir(item.rel_dir, specs.get(item.rel_dir))
            seen_dirs.add(item.rel_dir)
            copied += 1
            _bump(1, stage="export", current=item.name, message="正在复制模块图标…")
        provenance["module_files"].append(
            {
                "module": mid,
                "source_theme": src_name,
                "files": copied,
                "skipped": skipped,
                "conflicts": conflicts,
                "directories": sorted(seen_dirs),
            }
        )

    directories = sorted(used_dirs.keys(), key=lambda r: (r.startswith("scalable") is False, r))
    if not directories:
        directories = ["scalable/apps"]
        (dest_dir / "scalable" / "apps").mkdir(parents=True, exist_ok=True)
        used_dirs["scalable/apps"] = ThemeDirSpec(
            rel="scalable/apps",
            size=48,
            scale=1,
            context="Applications",
            typ="Scalable",
            min_size=1,
            max_size=512,
        )
    dir_specs = {rel: spec for rel, spec in used_dirs.items() if spec is not None}
    (dest_dir / "index.theme").write_text(
        _index_theme_text(
            status.inherit,
            directories,
            dir_specs,
            composed=True,
            follows_color_scheme=follows_color,
        ),
        encoding="utf-8",
    )
    _finalize_source_issues(provenance, source_issue_bucket)
    atomic_write_json(dest_dir / PROVENANCE_FILENAME, provenance)
    write_owned_marker(
        dest_dir,
        extra={"inherits": status.inherit, "mode": "composed", "icon_records": len(provenance["icons"])},
    )
    provenance["dest"] = str(dest_dir)
    provenance["directories"] = directories
    return provenance


def uninstall_owned_theme(theme_dir: Path) -> str:
    if not theme_dir.exists():
        return "主题目录不存在，无需删除"
    if not is_tool_owned(theme_dir):
        raise ExportError(f"拒绝删除：不是本工具所有的目录：{theme_dir}")
    shutil.rmtree(theme_dir)
    return f"已删除本工具主题：{theme_dir}"
