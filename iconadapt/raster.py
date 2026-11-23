"""Shared SVG rasterization for previews and masked export.

Applies in-memory-only adjustments. Never writes the source icon pack.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from .svgutil import (
    KIND_DECODE,
    KIND_EMPTY_FILTER,
    KIND_FILTER_BUFFER,
    KIND_FONT_SIZE,
    KIND_OVERSIZE_FILTER,
    KIND_REF_TYPE,
    KIND_UNSUPPORTED_PATTERN,
    KIND_UNRESOLVED,
    MAX_MASK_PIXELS,
    MAX_PREVIEW_PIXELS,
    SvgIssue,
    inspect_svg_text,
    issues_from_qt_messages,
    merge_issues,
    refine_issues,
    render_svg_bytes,
    sanitize_preview_svg,
    record_diag_source,
)
from .rendercache import (
    file_identity,
    get_cache,
    make_key,
    mask_signature,
    palette_signature,
    payload_digest,
)

_FONT_ATTR_RE = re.compile(
    r"""(font-size\s*=\s*["'])([0-9]*\.?[0-9]+)([a-zA-Z%]*)(["'])""",
    re.IGNORECASE,
)
_FONT_CSS_RE = re.compile(
    r"""(font-size\s*:\s*)([0-9]*\.?[0-9]+)([a-zA-Z%]*)""",
    re.IGNORECASE,
)
_VIEWBOX_RE = re.compile(
    r"""viewBox\s*=\s*['"]\s*([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)['"]""",
    re.IGNORECASE,
)
_SVG_WH_RE = re.compile(
    r"""<svg\b[^>]*?\b(width|height)\s*=\s*['"]([0-9.]+)[a-z%]*['"]""",
    re.IGNORECASE,
)
_TEXT_RE = re.compile(
    r"""<(text|tspan)\b([^>]*?)(/?)>""",
    re.IGNORECASE,
)


@dataclass
class RasterResult:
    image: object | None
    issues: list[SvgIssue] = field(default_factory=list)
    downgrade: str = ""  # "", "hires-downscale", "empty-filters", "strip-filters"
    hires_size: int | None = None
    font_adjusted: bool = False
    cache_hit: bool = False
    source: str = ""
    resolved: str = ""
    size: int = 0
    process: str = "raw"

    def to_dict(self) -> dict:
        return {
            "issues": [i.to_dict() for i in self.issues],
            "downgrade": self.downgrade,
            "hires_size": self.hires_size,
            "font_adjusted": self.font_adjusted,
            "cache_hit": self.cache_hit,
            "source": self.source,
            "resolved": self.resolved,
            "size": self.size,
            "process": self.process,
        }


def _viewbox_width(text: str) -> float | None:
    match = _VIEWBOX_RE.search(text or "")
    if match:
        try:
            w = float(match.group(3))
            return w if w > 0 else None
        except ValueError:
            return None
    widths = [m for m in _SVG_WH_RE.finditer(text or "") if m.group(1).lower() == "width"]
    if widths:
        try:
            w = float(widths[0].group(2))
            return w if w > 0 else None
        except ValueError:
            return None
    return None


def adjust_subpixel_fonts(text: str, output_size: int) -> tuple[str, list[SvgIssue], bool]:
    """In-memory only: keep intended visual size when Qt would int-truncate a font-size < 1.

    For values in (0, 1), set font-size to 1 (so Qt accepts it) and scale the
    element around its x/y origin by the original value. Lengths that would
    also be scaled (stroke, letter-spacing, etc.) are divided first, so the
    appearance stays the same. The source file is not written. If we cannot
    wrap the element, we leave it unchanged and mark the text incomplete.
    """
    issues: list[SvgIssue] = []
    if not text or output_size < 1:
        return text, issues, False

    values = [float(m.group(2)) for m in list(_FONT_ATTR_RE.finditer(text)) + list(_FONT_CSS_RE.finditer(text))]
    if not values or all(v >= 1 or v <= 0 for v in values):
        return text, issues, False

    changed = False
    incomplete = False

    def bump_font_only(blob: str) -> str:
        def repl_attr(match: re.Match) -> str:
            try:
                val = float(match.group(2))
            except ValueError:
                return match.group(0)
            unit = match.group(3) or ""
            if unit.lower() in {"px", "pt", "em", "%"}:
                return match.group(0)
            if 0 < val < 1:
                return f"{match.group(1)}1{unit}{match.group(4)}"
            return match.group(0)

        def repl_css(match: re.Match) -> str:
            try:
                val = float(match.group(2))
            except ValueError:
                return match.group(0)
            unit = match.group(3) or ""
            if unit.lower() in {"px", "pt", "em", "%"}:
                return match.group(0)
            if 0 < val < 1:
                return f"{match.group(1)}1{unit}"
            return match.group(0)

        return _FONT_CSS_RE.sub(repl_css, _FONT_ATTR_RE.sub(repl_attr, blob))

    def scale_length_attrs(blob: str, scale: float) -> str:
        if scale <= 0:
            return blob

        def bump_named(match: re.Match) -> str:
            try:
                val = float(match.group(2))
            except ValueError:
                return match.group(0)
            unit = match.group(3) or ""
            if unit.lower() in {"px", "pt", "em", "%"}:
                return match.group(0)
            new = val / scale
            # Keep a compact but precise decimal.
            rendered = f"{new:.6f}".rstrip("0").rstrip(".")
            return f"{match.group(1)}{rendered}{unit}{match.group(4)}"

        pattern = re.compile(
            r"""((?:letter-spacing|word-spacing|stroke-width|stroke-dashoffset|dx|dy)\s*=\s*["'])([0-9]*\.?[0-9]+)([a-zA-Z%]*)(["'])""",
            re.IGNORECASE,
        )
        return pattern.sub(bump_named, blob)

    out = []
    pos = 0
    for match in _TEXT_RE.finditer(text):
        out.append(text[pos:match.start()])
        tag, attrs, selfclose = match.group(1), match.group(2), match.group(3)
        sizes = [float(m.group(2)) for m in _FONT_ATTR_RE.finditer(attrs)]
        sizes += [float(m.group(2)) for m in _FONT_CSS_RE.finditer(attrs)]
        scale = next((v for v in sizes if 0 < v < 1), None)
        new_attrs = attrs
        if scale is not None:
            x = _attr_num(attrs, "x")
            y = _attr_num(attrs, "y")
            existing = _attr_str(attrs, "transform") or ""
            if x is not None and y is not None:
                new_attrs = scale_length_attrs(bump_font_only(attrs), scale)
                prefix = f"translate({x} {y}) scale({scale}) translate({-x} {-y})"
                composed = f"{prefix} {existing}".strip()
                if _attr_str(attrs, "transform") is None:
                    new_attrs += f' transform="{composed}"'
                else:
                    new_attrs = re.sub(
                        r"""transform\s*=\s*['"][^'"]*['"]""",
                        f'transform="{composed}"',
                        new_attrs,
                        count=1,
                        flags=re.IGNORECASE,
                    )
                changed = True
            else:
                incomplete = True
        out.append(f"<{tag}{new_attrs}{selfclose}>")
        pos = match.end()
    out.append(text[pos:])
    new_text = "".join(out)
    if changed:
        issues.append(
            SvgIssue(
                kind=KIND_FONT_SIZE,
                message="源图标字号小于 1，已在内存中按原比例缩放后再绘制（未改源文件）",
                detail="scaled-to-keep-size",
            )
        )
    if incomplete:
        issues.append(
            SvgIssue(
                kind=KIND_FONT_SIZE,
                message="源图标字号过小，无法在不改位置的情况下安全调整，文字可能画不出来",
                detail="unanchored",
            )
        )
    return new_text, issues, changed


def _attr_num(blob: str, name: str) -> float | None:
    match = re.search(rf"""\b{name}\s*=\s*['"]([-\d.]+)['"]""", blob, re.IGNORECASE)
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


def _attr_str(blob: str, name: str) -> str | None:
    match = re.search(rf"""\b{name}\s*=\s*['"]([^'"]*)['"]""", blob, re.IGNORECASE)
    return match.group(1) if match else None


def _has_buffer(messages: Iterable[str], issues: Iterable[SvgIssue] | None = None) -> bool:
    if any(i.kind == KIND_FILTER_BUFFER for i in (issues or [])):
        return True
    return any(
        "buffer size is too big" in str(m).lower() or "filter is too big" in str(m).lower()
        for m in (messages or [])
    )


def _has_font_warn(messages: Iterable[str]) -> bool:
    return any("pixel size" in str(m).lower() and "<=" in str(m) for m in (messages or []))


def _downscale(img, size: int):
    from PyQt6.QtCore import Qt
    from PyQt6.QtGui import QImage, QPainter

    if img is None or img.isNull():
        return None
    if img.width() == size and img.height() == size:
        return img
    canvas = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    canvas.fill(Qt.GlobalColor.transparent)
    scaled = img.scaled(
        size,
        size,
        Qt.AspectRatioMode.KeepAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )
    painter = QPainter(canvas)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.drawImage((size - scaled.width()) // 2, (size - scaled.height()) // 2, scaled)
    painter.end()
    return canvas


def _hires_ladder(size: int, cap: int) -> list[int]:
    cap = max(size + 1, min(cap, MAX_MASK_PIXELS))
    candidates = []
    for value in (48, 64, 96, 128, 256, min(cap, max(48, size * 3)), min(cap, max(64, size * 4))):
        value = int(value)
        if size < value <= cap and value not in candidates:
            candidates.append(value)
    return candidates


def rasterize_svg(
    text: str,
    size: int,
    *,
    source: str = "",
    cap: int = MAX_PREVIEW_PIXELS,
    role: str = "preview",
    palette=None,
    identity: tuple = (),
) -> RasterResult:
    """Rasterize one SVG with cache, font repair, and size-local filter fallback.

    Does not invent paint servers. Does not strip filters at sizes that
    already render them successfully.
    """
    from PyQt6.QtGui import QImage

    size = max(1, min(int(size), cap))
    source = source or ""
    resolved = identity[0] if identity else source
    record_diag_source(source, resolved)
    static = inspect_svg_text(text)
    work, font_issues, font_changed = adjust_subpixel_fonts(text, size)
    if font_changed:
        static = merge_issues(static, font_issues)
    else:
        static = merge_issues(static, font_issues)

    process = "font-adjusted" if font_changed else "raw"
    digest = payload_digest(work)
    key = make_key(
        digest=digest,
        size=size,
        role=role,
        identity=identity,
        palette=palette_signature(palette),
        process=process,
    )
    cache = get_cache()
    cached = cache.get_entry(key)
    if cached is not None:
        image, meta = cached
        cached_issues = []
        for item in meta.get("issues") or []:
            if isinstance(item, SvgIssue):
                cached_issues.append(item)
            elif isinstance(item, dict):
                cached_issues.append(
                    SvgIssue(
                        kind=str(item.get("kind") or ""),
                        message=str(item.get("message") or ""),
                        detail=str(item.get("detail") or ""),
                    )
                )
        issues = merge_issues(static, cached_issues)
        return RasterResult(
            image=image,
            issues=issues,
            downgrade=str(meta.get("downgrade") or ""),
            hires_size=meta.get("hires_size"),
            font_adjusted=bool(meta.get("font_adjusted", font_changed)),
            cache_hit=True,
            source=source,
            resolved=resolved,
            size=size,
            process=str(meta.get("process") or process),
        )

    issues = list(static)
    img = None
    messages: list[str] = []
    downgrade = ""
    hires_size = None

    empty_only = any(i.kind == KIND_EMPTY_FILTER for i in static) and not any(
        i.kind == KIND_OVERSIZE_FILTER for i in static
    )
    payload = work
    if empty_only:
        cleaned = sanitize_preview_svg(work, mode="empty-filters")
        if cleaned != work:
            payload = cleaned
            downgrade = "empty-filters"
            issues = merge_issues(
                issues,
                [SvgIssue(kind=KIND_EMPTY_FILTER, message="已忽略空的滤镜元素", detail="raster")],
            )

    img, messages = render_svg_bytes(payload.encode("utf-8"), size, source=source or "svg", cap=cap)
    issues = refine_issues(merge_issues(issues, issues_from_qt_messages(messages), static), payload)
    if _has_font_warn(messages) and not font_changed:
        issues = merge_issues(
            issues,
            [SvgIssue(kind=KIND_FONT_SIZE, message="源图标文字字号过小，未能完整绘制", detail="qt-zero")],
        )

    if img is not None and _has_buffer(messages):
        # Keep the raw image as a fallback. Try a higher resolution that
        # often succeeds for objectBoundingBox filters on large user coords,
        # then downscale. Do not strip first — that would drop working
        # filters at 48/256 as well.
        recovered = False
        for hi in _hires_ladder(size, cap):
            hi_img, hi_msgs = render_svg_bytes(payload.encode("utf-8"), hi, source=source or "svg", cap=cap)
            if hi_img is not None and not _has_buffer(hi_msgs):
                img = _downscale(hi_img, size)
                hires_size = hi
                downgrade = "hires-downscale"
                recovered = True
                issues = merge_issues(
                    issues,
                    [
                        SvgIssue(
                            kind=KIND_FILTER_BUFFER,
                            message=f"{size}px 滤镜无法按原尺寸绘制，已先按 {hi}px 渲染再缩小（未改源文件）",
                            detail=f"{size}->{hi}",
                        )
                    ],
                )
                break
        if not recovered:
            stripped = sanitize_preview_svg(payload, mode="strip-filters")
            if stripped != payload:
                img2, msg2 = render_svg_bytes(stripped.encode("utf-8"), size, source=source or "svg", cap=cap)
                if img2 is not None:
                    img = img2
                    downgrade = "strip-filters"
                    issues = merge_issues(
                        issues,
                        issues_from_qt_messages(msg2),
                        [
                            SvgIssue(
                                kind=KIND_FILTER_BUFFER,
                                message=f"{size}px 滤镜无法安全绘制，已忽略滤镜，外观可能不完整",
                                detail="strip",
                            )
                        ],
                    )

    if img is None or img.isNull():
        # Last attempt: empty-filters then strip, still no invented paint.
        for mode, label in (("empty-filters", "empty-filters"), ("strip-filters", "strip-filters")):
            cleaned = sanitize_preview_svg(work, mode=mode)
            if cleaned == work:
                continue
            img2, msg2 = render_svg_bytes(cleaned.encode("utf-8"), size, source=source or "svg", cap=cap)
            if img2 is not None and not img2.isNull():
                img = img2
                downgrade = label
                issues = merge_issues(issues, issues_from_qt_messages(msg2))
                break

    if img is not None and not img.isNull():
        process_final = process + (f":{downgrade}" if downgrade else "")
        cache.put(
            key,
            img,
            {
                "downgrade": downgrade,
                "hires_size": hires_size,
                "font_adjusted": font_changed,
                "process": process_final,
                "issues": [i.to_dict() for i in issues],
            },
        )

    return RasterResult(
        image=img if img is not None and not img.isNull() else None,
        issues=issues,
        downgrade=downgrade,
        hires_size=hires_size,
        font_adjusted=font_changed,
        cache_hit=False,
        source=source,
        resolved=resolved,
        size=size,
        process=process + (f":{downgrade}" if downgrade else ""),
    )


def rasterize_svg_file(path: Path | str, size: int, *, cap: int = MAX_MASK_PIXELS, role: str = "mask") -> RasterResult:
    from .svgutil import read_svg_text

    src = Path(path)
    text = read_svg_text(src)
    if text is None:
        return RasterResult(
            image=None,
            issues=[SvgIssue(kind=KIND_DECODE, message=f"无法读取这个 SVG 文件")],
            source=str(src),
            size=size,
        )
    ident = file_identity(src)
    return rasterize_svg(text, size, source=str(src), cap=cap, role=role, identity=ident)


__all__ = [
    "RasterResult",
    "adjust_subpixel_fonts",
    "rasterize_svg",
    "rasterize_svg_file",
]
