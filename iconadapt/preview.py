"""Visible module previews, including base-theme fallback and symbolic SVGs.

Previews follow the same inherit order the exporter writes into Inherits.
The generated theme itself (icon-adapt) is never used as a source, so a
session that is already using it cannot recurse into its own output.
"""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .constants import MODULE_SOURCE_SYSTEM, THEME_INTERNAL_NAME
from .svgutil import (
    KIND_DECODE,
    MAX_PREVIEW_PIXELS,
    SvgIssue,
    clamp_pixel_size,
    flush_svg_log_summary,
    install_qt_svg_log_filter,
    issue_note,
    load_raster_bounded,
    merge_issues,
    read_svg_text,
    render_svg_bytes,
)
from .raster import rasterize_svg
from .rendercache import file_identity, palette_signature
from .sourcecheck import describe_source
from .modules import (
    REPRESENTATIVE_NAMES,
    is_system_source,
    module_file_to_dict,
    module_has_icons,
    pick_preview_file,
    preview_items,
    PREFERRED_SIZE,
    _files_for_stem,
)
from .theme import IconTheme

# Render target for the thumbnail grid.
PREVIEW_PIXELS = 48

_STYLE_RE = re.compile(
    r'(<style\b[^>]*id=["\']current-color-scheme["\'][^>]*>)(.*?)(</style>)',
    re.IGNORECASE | re.DOTALL,
)
_COLORSCHEME_HINT = re.compile(
    r"ColorScheme-|current-color-scheme|currentColor|currentcolour",
    re.IGNORECASE,
)


@dataclass
class PreviewPalette:
    text: str = "#232627"
    background: str = "#eff0f1"
    highlight: str = "#3daee9"
    neutral: str = "#ca9216"
    positive: str = "#27ae60"
    negative: str = "#da4453"
    view_text: str = "#232627"
    view_background: str = "#fcfcfc"
    button_text: str = "#232627"
    button_background: str = "#eff0f1"


@dataclass
class RenderResult:
    data_url: str = ""
    status: str = "ok"  # ok, error, broken, low_contrast, missing, incomplete
    note: str = ""
    symbolic: bool = False
    plate: str = "light"  # light, dark, checker
    visible_pixels: int = 0
    issues: list[SvgIssue] = field(default_factory=list)
    preview_simplified: bool = False


def is_cursor_theme(theme: IconTheme) -> bool:
    """A cursor pack has no icon-theme directories, only pointer assets."""
    if theme.directories:
        return False
    try:
        names = {p.name.lower() for p in theme.path.iterdir()}
    except OSError:
        names = set()
    return bool(names & {"cursors", "cursors_scalable"}) or "cursor" in theme.internal_name.lower()


def theme_kind(theme: IconTheme) -> str:
    if is_cursor_theme(theme):
        return "cursors"
    if not theme.directories:
        return "empty"
    return "icons"


def skip_source_name(name: str | None) -> bool:
    return (not name) or name == THEME_INTERNAL_NAME


def flatten_inherit(
    starts: Iterable[str],
    themes: dict[str, IconTheme],
    *,
    skip: Iterable[str] = (),
) -> tuple[list[str], list[str]]:
    """Depth-first inherit walk with cycle detection. Never yields icon-adapt."""
    warnings: list[str] = []
    chain: list[str] = []
    visiting: set[str] = set()
    done: set[str] = set()
    blocked = {THEME_INTERNAL_NAME, *skip}

    def walk(name: str) -> None:
        if not name or name in done:
            return
        if name in blocked:
            done.add(name)
            return
        if name in visiting:
            warnings.append(f"inheritance cycle involving {name}")
            return
        visiting.add(name)
        if name in themes and themes[name].valid:
            chain.append(name)
            for parent in themes[name].inherits:
                walk(parent)
        elif name not in themes:
            warnings.append(f"inherited theme not installed: {name}")
        visiting.remove(name)
        done.add(name)

    for start in starts:
        walk(start)
    return chain, warnings


def base_preview_chain(
    inherit: list[str] | None,
    themes: dict[str, IconTheme],
) -> list[str]:
    """The chain written into a generated theme's Inherits, then parents."""
    chain, _ = flatten_inherit(inherit or [], themes, skip={THEME_INTERNAL_NAME})
    return chain


def palette_from_app() -> PreviewPalette:
    pal = PreviewPalette()
    try:
        from PyQt6.QtGui import QGuiApplication, QPalette

        app = QGuiApplication.instance()
        if app is None:
            return pal
        qp = app.palette()
        def hx(role) -> str:
            return qp.color(role).name()

        pal.background = hx(QPalette.ColorRole.Window)
        pal.text = hx(QPalette.ColorRole.WindowText)
        pal.view_background = hx(QPalette.ColorRole.Base)
        pal.view_text = hx(QPalette.ColorRole.Text)
        pal.button_background = hx(QPalette.ColorRole.Button)
        pal.button_text = hx(QPalette.ColorRole.ButtonText)
        pal.highlight = hx(QPalette.ColorRole.Highlight)
        pal.negative = hx(QPalette.ColorRole.BrightText) if False else pal.negative
    except Exception:
        return pal
    return pal


def _colorscheme_css(pal: PreviewPalette) -> str:
    return (
        f".ColorScheme-Text {{ color:{pal.text}; }} "
        f".ColorScheme-Background {{ color:{pal.background}; }} "
        f".ColorScheme-Highlight {{ color:{pal.highlight}; }} "
        f".ColorScheme-HighlightedText {{ color:{pal.view_background}; }} "
        f".ColorScheme-PositiveText {{ color:{pal.positive}; }} "
        f".ColorScheme-NeutralText {{ color:{pal.neutral}; }} "
        f".ColorScheme-NegativeText {{ color:{pal.negative}; }} "
        f".ColorScheme-ViewText {{ color:{pal.view_text}; }} "
        f".ColorScheme-ViewBackground {{ color:{pal.view_background}; }} "
        f".ColorScheme-ButtonText {{ color:{pal.button_text}; }} "
        f".ColorScheme-ButtonBackground {{ color:{pal.button_background}; }} "
        f".ColorScheme-ActiveText {{ color:{pal.highlight}; }} "
    )


def looks_colorscheme_svg(text: str, path: Path) -> bool:
    if _COLORSCHEME_HINT.search(text or ""):
        return True
    stem = path.stem.lower()
    parts = {p.lower() for p in path.parts}
    return stem.endswith("-symbolic") or "symbolic" in parts


def apply_colorscheme(text: str, pal: PreviewPalette) -> str:
    css = _colorscheme_css(pal)
    if _STYLE_RE.search(text):
        text = _STYLE_RE.sub(lambda m: m.group(1) + css + m.group(3), text, count=1)
    else:
        injection = f'<defs><style type="text/css" id="current-color-scheme">{css}</style></defs>'
        if re.search(r"<svg\b[^>]*>", text, re.IGNORECASE):
            text = re.sub(r"(<svg\b[^>]*>)", r"\1" + injection, text, count=1, flags=re.IGNORECASE)
        else:
            text = injection + text
    # Make currentColor resolve even when a renderer ignores class color.
    text = re.sub(
        r"(<svg\b)([^>]*)(>)",
        lambda m: m.group(1) + m.group(2) + ("" if re.search(r"\bcolor=", m.group(2)) else f' color="{pal.text}"') + m.group(3),
        text,
        count=1,
        flags=re.IGNORECASE,
    )
    return text


def _read_svg_text(path: Path) -> str | None:
    return read_svg_text(path)


def _ensure_qt() -> bool:
    from .mask import ensure_gui_app

    try:
        ensure_gui_app()
        from PyQt6.QtGui import QGuiApplication, QImage  # noqa: F401

        install_qt_svg_log_filter()
        return QGuiApplication.instance() is not None or True
    except Exception:
        return False


def _qcolor(name: str):
    from PyQt6.QtGui import QColor

    c = QColor(name)
    if not c.isValid():
        c = QColor("#eff0f1")
    return c


def _fill_checker(img, light="#f4f4f4", dark="#cfcfcf", cell=4) -> None:
    from PyQt6.QtGui import QColor

    w, h = img.width(), img.height()
    a, b = QColor(light), QColor(dark)
    for y in range(h):
        for x in range(w):
            img.setPixelColor(x, y, a if ((x // cell) + (y // cell)) % 2 == 0 else b)


def _composite_on(plate, overlay):
    from PyQt6.QtGui import QPainter
    from PyQt6.QtCore import Qt

    out = plate.copy()
    p = QPainter(out)
    p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
    p.drawImage(0, 0, overlay)
    p.end()
    return out


def _visible_contrast(img, bg, *, min_delta: int = 40) -> int:
    """Count pixels that both have alpha and differ from the plate color."""
    br, bgc, bb = bg.red(), bg.green(), bg.blue()
    count = 0
    for y in range(img.height()):
        for x in range(img.width()):
            c = img.pixelColor(x, y)
            if c.alpha() < 24:
                continue
            if max(abs(c.red() - br), abs(c.green() - bgc), abs(c.blue() - bb)) >= min_delta:
                count += 1
    return count


def _render_svg_bytes(data: bytes, size: int, *, source: str = ""):
    img, _messages = render_svg_bytes(data, size, source=source, cap=MAX_PREVIEW_PIXELS)
    return img


def _load_raster(path: Path, size: int):
    return load_raster_bounded(path, size, cap=MAX_PREVIEW_PIXELS)


def _render_svg_for_preview(
    text: str,
    size: int,
    *,
    source: str = "",
    palette=None,
    identity: tuple = (),
) -> tuple:
    """Render one SVG via the shared rasterizer. Never writes the source."""
    result = rasterize_svg(
        text,
        size,
        source=source or "preview-svg",
        cap=MAX_PREVIEW_PIXELS,
        role="preview",
        palette=palette,
        identity=identity,
    )
    simplified = bool(result.downgrade)
    return result.image, result.issues, simplified


def _to_data_url(img) -> str:
    from PyQt6.QtCore import QByteArray, QBuffer, QIODevice

    ba = QByteArray()
    buf = QBuffer(ba)
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    img.save(buf, "PNG")
    buf.close()
    return "data:image/png;base64," + base64.b64encode(bytes(ba)).decode("ascii")


_RENDER_CACHE: dict[tuple, RenderResult] = {}


def clear_preview_cache() -> None:
    _RENDER_CACHE.clear()


def render_preview(
    path: Path | str,
    size: int = PREVIEW_PIXELS,
    *,
    pal: PreviewPalette | None = None,
) -> RenderResult:
    """Rasterize one icon for the UI. Never writes the source file."""
    src = Path(path)
    pal = pal or palette_from_app()
    size = clamp_pixel_size(size, cap=MAX_PREVIEW_PIXELS)
    ident = file_identity(src)
    key = (ident or str(src), size, palette_signature(pal))
    cached = _RENDER_CACHE.get(key)
    if cached is not None:
        return cached

    result = _render_preview_uncached(src, size, pal)
    _RENDER_CACHE[key] = result
    return result


def _render_preview_uncached(src: Path, size: int, pal: PreviewPalette) -> RenderResult:
    status = describe_source(src)
    if not status.ok:
        return RenderResult(status="broken", note=status.display)
    if not _ensure_qt():
        return RenderResult(status="error", note="当前环境无法渲染预览")

    from PyQt6.QtGui import QImage, QColor
    from PyQt6.QtCore import Qt

    size = clamp_pixel_size(size, cap=MAX_PREVIEW_PIXELS)
    suffix = src.suffix.lower()
    symbolic = False
    overlay = None
    load_error = ""
    svg_issues: list[SvgIssue] = []
    simplified = False

    if suffix in {".svg", ".svgz"}:
        text = _read_svg_text(src)
        if text is None:
            return RenderResult(
                status="broken",
                note="无法读取这个 SVG 文件",
                issues=[SvgIssue(kind=KIND_DECODE, message="无法读取这个 SVG 文件")],
            )
        symbolic = looks_colorscheme_svg(text, src)
        payload = apply_colorscheme(text, pal) if symbolic else text
        overlay, svg_issues, simplified = _render_svg_for_preview(
            payload, size, source=str(src), palette=pal, identity=file_identity(src)
        )
        if overlay is None:
            load_error = "SVG 无法解码"
            svg_issues = merge_issues(
                svg_issues,
                [SvgIssue(kind=KIND_DECODE, message="SVG 无法解码")],
            )
    elif suffix in {".png", ".xpm", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}:
        overlay = _load_raster(src, size)
        if overlay is None:
            load_error = "图片无法解码"
    else:
        overlay = _load_raster(src, size)
        if overlay is None:
            load_error = f"不支持的图标格式：{suffix or '无扩展名'}"

    def _finish(result: RenderResult) -> RenderResult:
        if svg_issues:
            result.issues = merge_issues(result.issues, svg_issues)
            result.preview_simplified = result.preview_simplified or simplified
            note = issue_note(result.issues)
            if result.status in {"ok", "low_contrast"}:
                result.status = "incomplete"
            if note:
                # Keep contrast / symbolic hints after the integrity note.
                extra = result.note.strip()
                result.note = note if not extra or extra in note else (note + extra)
        return result

    if overlay is None:
        return _finish(RenderResult(status="error", note=load_error or "无法加载图标", symbolic=symbolic))

    light_bg = _qcolor(pal.background)
    dark_bg = _qcolor("#2a2e32")

    def plate_image(color) -> QImage:
        img = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
        img.fill(color)
        # Center the (possibly non-square) overlay.
        composed = _composite_on(img, _centered(overlay, size))
        return composed

    light = plate_image(light_bg)
    vis_light = _visible_contrast(light, light_bg)
    if vis_light >= 8:
        return _finish(RenderResult(
            data_url=_to_data_url(light),
            status="ok",
            note="符号图标已按预览配色渲染。" if symbolic else "",
            symbolic=symbolic,
            plate="light",
            visible_pixels=vis_light,
        ))

    dark = plate_image(dark_bg)
    vis_dark = _visible_contrast(dark, dark_bg)
    if vis_dark >= 8:
        return _finish(RenderResult(
            data_url=_to_data_url(dark),
            status="ok",
            note="图标颜色较浅，已放在深色底上预览，避免看不清。" if _is_light_glyph(overlay) else "图标在浅底上对比不足，已换深色底预览。",
            symbolic=symbolic,
            plate="dark",
            visible_pixels=vis_dark,
        ))

    # Last attempt: checkerboard, still no recolor of pictorial glyphs.
    from PyQt6.QtGui import QImage as _QI

    checker = _QI(size, size, _QI.Format.Format_ARGB32_Premultiplied)
    _fill_checker(checker)
    checked = _composite_on(checker, _centered(overlay, size))
    # Use mid-gray as the reference so both light and dark glyphs count.
    vis_chk = _visible_contrast(checked, _qcolor("#e0e0e0"), min_delta=28)
    if vis_chk >= 8:
        return _finish(RenderResult(
            data_url=_to_data_url(checked),
            status="ok",
            note="已用棋盘格底预览，便于看出透明边缘。",
            symbolic=symbolic,
            plate="checker",
            visible_pixels=vis_chk,
        ))

    if vis_light == 0 and vis_dark == 0:
        return _finish(RenderResult(
            status="error",
            note="图标文件是空的，或渲染结果完全透明。",
            symbolic=symbolic,
            visible_pixels=0,
        ))
    # Something is there but still low contrast; return the better plate with a warning.
    better = dark if vis_dark >= vis_light else light
    plate = "dark" if vis_dark >= vis_light else "light"
    return _finish(RenderResult(
        data_url=_to_data_url(better),
        status="low_contrast",
        note="图标与背景对比仍然偏低，桌面上的实际颜色可能不同。",
        symbolic=symbolic,
        plate=plate,
        visible_pixels=max(vis_light, vis_dark),
    ))


def _centered(overlay, size: int):
    from PyQt6.QtCore import Qt
    from PyQt6.QtGui import QImage

    if overlay.width() == size and overlay.height() == size:
        return overlay
    canvas = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    canvas.fill(Qt.GlobalColor.transparent)
    from PyQt6.QtGui import QPainter

    x = max(0, (size - overlay.width()) // 2)
    y = max(0, (size - overlay.height()) // 2)
    p = QPainter(canvas)
    p.drawImage(x, y, overlay)
    p.end()
    return canvas



def _is_light_glyph(overlay) -> bool:
    total = 0
    lum = 0
    for y in range(overlay.height()):
        for x in range(overlay.width()):
            c = overlay.pixelColor(x, y)
            if c.alpha() < 24:
                continue
            lum += c.red() + c.green() + c.blue()
            total += 1
    if not total:
        return False
    return (lum / total) > 200


def pack_flags_for_module(
    themes: dict[str, IconTheme], module_id: str
) -> dict[str, dict[str, Any]]:
    flags: dict[str, dict[str, Any]] = {}
    for name, theme in themes.items():
        if skip_source_name(name) or not theme.valid:
            continue
        kind = theme_kind(theme)
        empty = (kind != "icons") or (not module_has_icons(theme, module_id))
        flags[name] = {"empty": empty, "kind": kind}
    return flags


def _item_from_file(item, *, origin: str, fallback: bool) -> dict[str, Any]:
    data = module_file_to_dict(item)
    data["origin"] = origin  # own, base, fallback
    data["fallback"] = bool(fallback)
    data["render_status"] = "ok"
    data["render_note"] = ""
    data["preview_url"] = ""
    data["symbolic"] = False
    data["plate"] = ""
    return data


def previews_from_chain(
    chain: list[str],
    module_id: str,
    themes: dict[str, IconTheme],
    *,
    limit: int,
    origin: str,
) -> list[dict[str, Any]]:
    """Resolve representative names through the chain (first match wins)."""
    limit = max(1, limit)
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    usable = [n for n in chain if n in themes and themes[n].valid and not skip_source_name(n)]
    wanted = list(REPRESENTATIVE_NAMES.get(module_id, ()))
    for name in wanted:
        for tname in usable:
            files = _files_for_stem(themes[tname], module_id, name)
            if not files:
                continue
            chosen = pick_preview_file(files, PREFERRED_SIZE.get(module_id, 48))
            if not chosen:
                continue
            items.append(_item_from_file(chosen, origin=origin, fallback=origin == "fallback"))
            seen.add(name)
            break
        if len(items) >= limit:
            return items
    # Fill from the first theme that actually has this module.
    for tname in usable:
        if len(items) >= limit:
            break
        extra = preview_items(themes[tname], module_id, limit=limit)
        for row in extra:
            if row["name"] in seen:
                continue
            row = dict(row)
            row["origin"] = origin
            row["fallback"] = origin == "fallback"
            row["theme"] = tname
            row["render_status"] = "ok"
            row["render_note"] = ""
            row["preview_url"] = ""
            row["symbolic"] = False
            row["plate"] = ""
            items.append(row)
            seen.add(row["name"])
            if len(items) >= limit:
                break
    return items[:limit]


def compose_module_previews(
    source: str,
    module_id: str,
    themes: dict[str, IconTheme],
    *,
    inherit: list[str] | None = None,
    limit: int = 8,
) -> dict[str, Any]:
    """Build one module's preview payload, including base/fallback sources."""
    base_chain = base_preview_chain(inherit, themes)
    result: dict[str, Any] = {
        "source": source,
        "module": module_id,
        "empty": False,
        "has_content": False,
        "missing_theme": False,
        "kind": "icons",
        "fallback": False,
        "previews": [],
        "preview_caption": "",
        "status_line": "",
        "chain": [],
        "warnings": [],
    }

    if is_system_source(source):
        items = previews_from_chain(base_chain, module_id, themes, limit=limit, origin="base")
        sources = list(dict.fromkeys(i["theme"] for i in items if i.get("theme")))
        result["has_content"] = bool(items)
        result["previews"] = items
        result["chain"] = base_chain
        if items:
            shown = "、".join(sources) if sources else "基础主题"
            chain_txt = " → ".join(base_chain) if base_chain else shown
            result["preview_caption"] = f"预览来自基础主题「{shown}」，与安装后未覆盖时的回退一致。"
            result["status_line"] = f"这一类保持系统原样，会跟随基础主题（{chain_txt}）。"
        elif base_chain:
            result["status_line"] = (
                f"这一类保持系统原样，会跟随基础主题（{' → '.join(base_chain)}），"
                "但这些主题里还没有找到这类图标。"
            )
            result["empty"] = True
        else:
            result["status_line"] = "这一类保持系统原样，但还没有可用的基础主题可预览。"
            result["empty"] = True
        return result

    theme = themes.get(source)
    if theme is None or not theme.valid or skip_source_name(source):
        result["missing_theme"] = True
        result["empty"] = True
        result["status_line"] = "选中的图标包已经找不到，或不能再当作来源。"
        return result

    result["kind"] = theme_kind(theme)
    if result["kind"] == "cursors":
        result["empty"] = True
        result["status_line"] = "这是鼠标指针主题，没有这类图标。"
        # Still try inherits, in case a pack points at a real icon theme.
        chain, warnings = flatten_inherit(theme.inherits, themes)
        result["warnings"] = warnings
        result["chain"] = chain
        items = previews_from_chain(chain, module_id, themes, limit=limit, origin="fallback")
        if items:
            result["fallback"] = True
            result["previews"] = items
            shown = "、".join(dict.fromkeys(i["theme"] for i in items))
            result["preview_caption"] = f"指针主题本身没有这类图标。下面是会回退到的「{shown}」。"
        return result

    if module_has_icons(theme, module_id):
        items = []
        for row in preview_items(theme, module_id, limit=limit):
            row = dict(row)
            row["origin"] = "own"
            row["fallback"] = False
            row["render_status"] = "ok"
            row["render_note"] = ""
            row["preview_url"] = ""
            row["symbolic"] = False
            row["plate"] = ""
            items.append(row)
        result["has_content"] = True
        result["previews"] = items
        if not items:
            result["status_line"] = "这类目录在索引里有，但还没有读到可用的图标文件。"
        return result

    result["empty"] = True
    result["has_content"] = False
    chain, warnings = flatten_inherit(theme.inherits, themes)
    result["warnings"] = warnings
    result["chain"] = chain
    items = previews_from_chain(chain, module_id, themes, limit=limit, origin="fallback")
    if items:
        result["fallback"] = True
        result["previews"] = items
        shown = "、".join(dict.fromkeys(i["theme"] for i in items))
        result["preview_caption"] = f"这个图标包本身没有这类图标。下面是会回退到的「{shown}」。"
        result["status_line"] = result["preview_caption"]
    else:
        result["status_line"] = "这个图标包本身没有这类图标，它的继承链里也没有找到可用预览。"
    return result


def decorate_previews(
    items: list[dict[str, Any]],
    *,
    size: int = PREVIEW_PIXELS,
    pal: PreviewPalette | None = None,
) -> list[dict[str, Any]]:
    pal = pal or palette_from_app()
    out: list[dict[str, Any]] = []
    for row in items:
        row = dict(row)
        path = row.get("path") or ""
        rendered = render_preview(path, size, pal=pal) if path else RenderResult(
            status="missing", note="没有可预览的文件"
        )
        row["preview_url"] = rendered.data_url
        row["render_status"] = rendered.status
        row["render_note"] = rendered.note
        row["symbolic"] = rendered.symbolic or bool(row.get("symbolic"))
        row["plate"] = rendered.plate
        row["visible_pixels"] = rendered.visible_pixels
        row["preview_simplified"] = rendered.preview_simplified
        row["render_issues"] = [i.to_dict() for i in rendered.issues]
        out.append(row)
    flush_svg_log_summary()
    return out


def focus_caption(item: dict[str, Any] | None) -> str:
    """Text shown after the user clicks a thumbnail. Never claims success on error."""
    if not item:
        return ""
    name = item.get("name") or "这个图标"
    theme = item.get("theme") or ""
    status = item.get("render_status") or "ok"
    if status in {"error", "broken", "missing"}:
        reason = item.get("render_note") or "无法加载"
        return f"无法预览「{name}」：{reason}"
    if status == "incomplete":
        reason = item.get("render_note") or "源图标不完整"
        bits = [f"预览「{name}」可能不完整：{reason}"]
        theme = item.get("theme") or ""
        if theme:
            origin = item.get("origin")
            if origin == "fallback":
                bits.append(f"来自 {theme}（回退，不是该图标包本体）")
            elif origin == "base":
                bits.append(f"来自 {theme}（基础主题）")
            else:
                bits.append(f"来自 {theme}")
        if item.get("size"):
            bits.append(f"{item['size']}px")
        if item.get("maybe_unreliable_tray"):
            bits.append("可能不生效：托盘程序若自己画图标，不会跟着换")
        return "，".join(bits)
    bits = [f"预览：{name}"]
    if theme:
        origin = item.get("origin")
        if origin == "fallback":
            bits.append(f"来自 {theme}（回退，不是该图标包本体）")
        elif origin == "base":
            bits.append(f"来自 {theme}（基础主题）")
        else:
            bits.append(f"来自 {theme}")
    if item.get("size"):
        bits.append(f"{item['size']}px")
    line = "，".join(bits[:1]) + ("，" + "，".join(bits[1:]) if len(bits) > 1 else "")
    # Slightly nicer: first clause already has 预览：name
    line = bits[0]
    if len(bits) > 1:
        line += "，" + "，".join(bits[1:])
    if status == "low_contrast":
        line += "。" + (item.get("render_note") or "对比偏低。")
    elif item.get("render_note"):
        line += "。" + item["render_note"]
    if item.get("maybe_unreliable_tray"):
        line += "（可能不生效：托盘程序若自己画图标，不会跟着换）"
    return line
