"""CLI and GUI entry: python3 -m iconadapt <command>"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import TOOL_ID
from .constants import THEME_INTERNAL_NAME, TOOL_VERSION
from .desktop import scan_applications
from .export import ExportError, export_theme, uninstall_owned_theme
from .override import apply_overrides, plan_override, rollback_overrides
from .persist import load_choices, remove_selection, save_choices, upsert_selection
from .report import report_apps, summarize
from .strategy import resolve_strategy
from .theme import enumerate_themes
from . import xdgpaths
from .lookup import collect_exact_candidates
from .mask import MaskSettings, load_settings, provenance_mask_block
from .modules import load_module_map, normalize_module_map
from .search import filter_items, matches_app, matches_report


def _themes(icon_dirs=None):
    return enumerate_themes(icon_dirs or xdgpaths.icon_search_dirs())


def cmd_scan(args) -> int:
    apps = scan_applications(xdgpaths.applications_dirs(), current_desktop=xdgpaths.current_desktop())
    apps = filter_items(apps, getattr(args, "query", None), matches_app)
    for a in apps:
        print(f"{a.desktop_id}\t{a.display_name}\tIcon={a.icon}\t{a.path}")
    print(f"# {len(apps)} launchable apps", file=sys.stderr)
    return 0


def cmd_themes(args) -> int:
    themes = _themes()
    st = resolve_strategy(themes, current_theme=xdgpaths.kde_icon_theme_name())
    print(json.dumps({"strategy": st.to_dict(), "themes": [
        {"internal_name": t.internal_name, "display": t.display_name, "inherits": t.inherits, "path": str(t.path)}
        for t in sorted(themes.values(), key=lambda x: x.internal_name.lower())
    ]}, ensure_ascii=False, indent=2))
    return 0


def cmd_report(args) -> int:
    themes = _themes()
    current = args.theme or xdgpaths.kde_icon_theme_name() or "breeze"
    apps = scan_applications(xdgpaths.applications_dirs(), current_desktop=xdgpaths.current_desktop())
    rows = report_apps(apps, current, themes, include_candidates=args.verbose, include_hints=True)
    if args.status:
        rows = [r for r in rows if r.status == args.status]
    rows = [r for r in rows if matches_report(r, getattr(args, "query", None))]
    if args.json:
        print(json.dumps({"current_theme": current, "summary": summarize(rows), "apps": [r.to_dict() for r in rows]}, ensure_ascii=False, indent=2))
    else:
        print(f"current_theme={current} summary={summarize(rows)}")
        for r in rows:
            extra = f" -> {r.lookup_theme}:{r.lookup_path}" if r.lookup_path else ""
            hint_n = len(r.hints)
            print(f"{r.status:16} {r.desktop_id:40} Icon={r.icon}{extra}" + (f" hints={hint_n}" if hint_n else ""))
    return 0


def cmd_candidates(args) -> int:
    themes = _themes()
    cands = collect_exact_candidates(args.icon, themes)
    print(json.dumps([{"theme": c.theme, "path": str(c.path), "size": c.size} for c in cands], ensure_ascii=False, indent=2))
    return 0


def cmd_export(args) -> int:
    themes = _themes()
    current = xdgpaths.kde_icon_theme_name() or "breeze"
    status = resolve_strategy(themes, current_theme=current)
    apps = scan_applications(xdgpaths.applications_dirs(), current_desktop=xdgpaths.current_desktop())
    state = Path(args.state_dir) if args.state_dir else xdgpaths.tool_state_dir()
    store = load_choices(state) if state.exists() else {"selections": {}}
    mask = load_settings(state) if state.exists() else MaskSettings()
    modules = load_module_map(state) if state.exists() else {}
    for item in getattr(args, "module", None) or []:
        if "=" not in item:
            print(f"无效的 --module 参数（需要 模块=图标包）：{item}", file=sys.stderr)
            return 2
        mid, _, src = item.partition("=")
        modules[mid.strip()] = src.strip()
    modules = normalize_module_map(modules)
    dest = Path(args.dest) if args.dest else (state / "build" / THEME_INTERNAL_NAME)
    if args.dry_run:
        print(json.dumps({
            "dest": str(dest),
            "inherit": status.inherit,
            "missing_themes": status.missing,
            "notes": status.notes,
            "manual_selections": len(store.get("selections") or {}),
            "apps": len(apps),
            "mask": provenance_mask_block(mask),
            "modules": modules,
        }, ensure_ascii=False, indent=2))
        return 0
    try:
        prov = export_theme(
            dest,
            apps=apps,
            themes=themes,
            status=status,
            selections=store.get("selections") or {},
            overwrite_owned=True,
            mask=mask,
            modules=modules,
        )
        print(json.dumps(prov, ensure_ascii=False, indent=2))
        return 0
    except ExportError as exc:
        print(str(exc), file=sys.stderr)
        return 1


def cmd_uninstall(args) -> int:
    dest = Path(args.dest) if args.dest else xdgpaths.user_icons_dir() / THEME_INTERNAL_NAME
    try:
        print(uninstall_owned_theme(dest))
        return 0
    except ExportError as exc:
        print(str(exc), file=sys.stderr)
        return 1


def cmd_gui(args) -> int:
    from .gui import run_gui
    return run_gui(quit_after_load=args.quit_after_load)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="iconadapt", description="本机桌面图标适配工具（不自动改系统主题）")
    p.add_argument("--version", action="version", version=f"{TOOL_ID} {TOOL_VERSION}")
    sub = p.add_subparsers(dest="cmd", required=True)
    ps = sub.add_parser("scan", help="列出可启动应用")
    ps.add_argument("--query", help="按应用名、desktop id、Icon、GenericName、Comment 过滤（忽略大小写）")
    sub.add_parser("themes", help="枚举已装主题与默认策略状态")
    pr = sub.add_parser("report", help="图标覆盖报告")
    pr.add_argument("--json", action="store_true")
    pr.add_argument("--verbose", action="store_true")
    pr.add_argument("--theme", help="指定当前主题内部名")
    pr.add_argument("--status", help="只显示某类状态")
    pr.add_argument("--query", help="按应用名、desktop id、Icon、GenericName、Comment 过滤（忽略大小写）")
    pc = sub.add_parser("candidates", help="按精确图标名列出各主题中的文件")
    pc.add_argument("icon")
    pe = sub.add_parser("export", help="生成用户级主题到工具目录或指定目录（不切换主题）")
    pe.add_argument("--dest")
    pe.add_argument("--state-dir")
    pe.add_argument("--dry-run", action="store_true")
    pe.add_argument("--module", action="append", metavar="模块=图标包",
                    help="为模块指定来源，例如 --module places=Papirus。可重复。未指定则用已保存配置或系统原样。")
    pu = sub.add_parser("uninstall-theme", help="仅删除本工具拥有的主题目录")
    pu.add_argument("--dest")
    pg = sub.add_parser("gui", help="打开图形界面")
    pg.add_argument("--quit-after-load", action="store_true", help="加载后退出（用于离屏启动验证）")
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    cmds = {
        "scan": cmd_scan,
        "themes": cmd_themes,
        "report": cmd_report,
        "candidates": cmd_candidates,
        "export": cmd_export,
        "uninstall-theme": cmd_uninstall,
        "gui": cmd_gui,
    }
    return cmds[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
