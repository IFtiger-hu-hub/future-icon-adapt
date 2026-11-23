"""Application-icon masks for previews and exported copies only.

Never writes into installed icon themes or action/status directories.
When a mask is applied to export copies, results are rasterized to PNG;
source SVGs are not silently treated as still-vector.
"""

from __future__ import annotations

import base64
import json
import math
import os
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .constants import (
    MASK_COMMON_SIZES,
    MASK_SHAPES,
    SETTINGS_FILENAME,
    TOOL_ID,
    TOOL_VERSION,
)
from .svgutil import (
    MAX_MASK_PIXELS,
    clamp_pixel_size,
    install_qt_svg_log_filter,
    load_raster_bounded,
)
from .raster import RasterResult, rasterize_svg_file
from .persist import atomic_write_json
from .sourcecheck import describe_source

MASK_NOTE = (
    "启用遮罩后，写入生成主题的应用图标副本已栅格化为 PNG，"
    "源矢量不再保留。常用尺寸各自渲染，外形保持一致。"
    "已装原主题文件不会被修改。"
)


class MaskError(Exception):
    pass


@dataclass
class MaskSettings:
    enabled: bool = False
    shape: str = "circle"  # circle | rounded-square | squircle | custom
    custom_path: str = ""
    background: str = "#00000000"
    padding: float = 0.08

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)



_LAST_RASTER: dict | None = None


def last_raster_report() -> dict | None:
    """Most recent SVG raster report from load_source_rgba (export diagnostics)."""
    return dict(_LAST_RASTER) if _LAST_RASTER else None


def _store_raster_report(result: RasterResult | None) -> None:
    global _LAST_RASTER
    _LAST_RASTER = result.to_dict() if result is not None else None


def default_settings() -> MaskSettings:
    return MaskSettings()


def settings_path(state_dir: Path) -> Path:
    return state_dir / SETTINGS_FILENAME


def load_settings(state_dir: Path) -> MaskSettings:
    path = settings_path(state_dir)
    if not path.is_file():
        return default_settings()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default_settings()
    if not isinstance(data, dict):
        return default_settings()
    mask = data.get("mask") if isinstance(data.get("mask"), dict) else data
    return settings_from_dict(mask)


def settings_from_dict(data: dict[str, Any] | None) -> MaskSettings:
    data = data or {}
    padding = data.get("padding", 0.08)
    try:
        padding = float(padding)
    except (TypeError, ValueError):
        padding = 0.08
    return MaskSettings(
        enabled=bool(data.get("enabled", False)),
        shape=str(data.get("shape") or "circle"),
        custom_path=str(data.get("custom_path") or ""),
        background=str(data.get("background") or "#00000000"),
        padding=padding,
    )


def save_settings(state_dir: Path, settings: MaskSettings) -> Path:
    path = settings_path(state_dir)
    payload = {
        "version": 1,
        "tool": TOOL_ID,
        "tool_version": TOOL_VERSION,
        "mask": settings.to_dict(),
    }
    # Preserve unknown top-level keys if a file already exists.
    if path.is_file():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(existing, dict):
                existing.update(payload)
                payload = existing
        except (OSError, json.JSONDecodeError):
            pass
    atomic_write_json(path, payload)
    return path


def parse_color(value: str):
    """Accept #RGB, #RRGGBB, #AARRGGBB (Qt), #RRGGBBAA, and named colors."""
    from PyQt6.QtGui import QColor

    raw = (value or "").strip()
    if not raw:
        raise MaskError("底板颜色为空")
    if raw.startswith("#") and len(raw) == 9:
        # Ambiguous 8-digit form: try Qt #AARRGGBB first, also allow #RRGGBBAA
        qt = QColor(raw)
        if qt.isValid():
            return qt
    color = QColor(raw)
    if color.isValid():
        return color
    if raw.startswith("#") and len(raw) == 9:
        rr, gg, bb, aa = raw[1:3], raw[3:5], raw[5:7], raw[7:9]
        color = QColor(f"#{aa}{rr}{gg}{bb}")
        if color.isValid():
            return color
    raise MaskError(f"无法识别的底板颜色：{value}")


def validate_settings(settings: MaskSettings, *, require_enabled_ok: bool = True) -> None:
    if settings.shape not in (*MASK_SHAPES, "custom"):
        raise MaskError(f"未知遮罩形状：{settings.shape}")
    if not (0.0 <= float(settings.padding) <= 0.45):
        raise MaskError(f"内边距必须在 0 到 0.45 之间（画布比例），当前为 {settings.padding}")
    parse_color(settings.background)
    if not settings.enabled and not require_enabled_ok:
        return
    if settings.enabled and settings.shape == "custom":
        _load_custom_mask_source(Path(settings.custom_path))


def ensure_gui_app() -> None:
    """QImage/QSvg rendering needs a QGuiApplication. Safe to call many times."""
    os.environ.setdefault("QT_QPA_PLATFORM", os.environ.get("QT_QPA_PLATFORM", "offscreen"))
    from PyQt6.QtCore import QCoreApplication
    from PyQt6.QtGui import QGuiApplication

    inst = QCoreApplication.instance()
    if inst is None:
        QGuiApplication(sys.argv if sys.argv else ["iconadapt"])
        return
    # QCoreApplication without GUI is not enough for some SVG loads; tests should
    # construct QGuiApplication first. We still proceed if a QGuiApplication exists.


def shape_path(size: int, shape: str):
    from PyQt6.QtCore import QRectF
    from PyQt6.QtGui import QPainterPath

    if size < 1:
        raise MaskError("遮罩尺寸无效")
    inset = 0.5
    box = QRectF(inset, inset, size - 2 * inset, size - 2 * inset)
    path = QPainterPath()
    if shape == "circle":
        path.addEllipse(box)
        return path
    if shape == "rounded-square":
        radius = max(1.0, size * 0.22)
        path.addRoundedRect(box, radius, radius)
        return path
    if shape == "squircle":
        return _superellipse_path(size, n=4.0, inset=inset)
    raise MaskError(f"未知内置形状：{shape}")


def _superellipse_path(size: int, n: float = 4.0, inset: float = 0.5):
    from PyQt6.QtGui import QPainterPath

    cx = cy = size / 2.0
    a = b = max(0.5, size / 2.0 - inset)
    path = QPainterPath()
    steps = max(64, size * 2)
    exp = 2.0 / n
    for i in range(steps + 1):
        theta = (2.0 * math.pi) * i / steps
        c, s = math.cos(theta), math.sin(theta)
        x = cx + a * math.copysign(abs(c) ** exp, c)
        y = cy + b * math.copysign(abs(s) ** exp, s)
        if i == 0:
            path.moveTo(x, y)
        else:
            path.lineTo(x, y)
    path.closeSubpath()
    return path


def _load_custom_mask_source(path: Path):
    """Raise MaskError if the custom mask file cannot be used. Returns ('svg', Path) or ('image', QImage)."""
    from PyQt6.QtGui import QImage

    if not path or str(path).strip() == "":
        raise MaskError("启用了自定义遮罩，但未选择遮罩文件")
    path = Path(path).expanduser()
    if not path.is_file():
        raise MaskError(f"自定义遮罩文件不存在：{path}")
    suffix = path.suffix.lower()
    if suffix not in {".svg", ".svgz", ".png"}:
        raise MaskError(f"自定义遮罩仅支持 SVG 或 PNG，实际为：{path.name}")
    if path.stat().st_size == 0:
        raise MaskError(f"自定义遮罩文件为空：{path}")
    ensure_gui_app()
    if suffix in {".svg", ".svgz"}:
        from PyQt6.QtSvg import QSvgRenderer

        renderer = QSvgRenderer(str(path))
        if not renderer.isValid():
            raise MaskError(f"无法解析自定义 SVG 遮罩：{path}")
        return "svg", path
    image = QImage(str(path))
    if image.isNull() or image.width() < 1 or image.height() < 1:
        raise MaskError(f"无法读取自定义 PNG 遮罩（文件损坏或不是有效图像）：{path}")
    return "image", image


def load_source_rgba(path: Path, size: int):
    """Load an icon file into a size×size ARGB image, preserving aspect ratio."""
    from PyQt6.QtCore import Qt
    from PyQt6.QtGui import QImage, QPainter

    if size < 1:
        raise MaskError("渲染尺寸无效")
    size = clamp_pixel_size(size, cap=MAX_MASK_PIXELS)
    path = Path(path)
    status = describe_source(path)
    if not status.ok:
        raise MaskError(status.display)
    ensure_gui_app()
    install_qt_svg_log_filter()
    suffix = path.suffix.lower()
    canvas = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    canvas.fill(Qt.GlobalColor.transparent)
    if suffix in {".svg", ".svgz"}:
        result = rasterize_svg_file(path, size, cap=MAX_MASK_PIXELS, role="mask")
        _store_raster_report(result)
        img = result.image
        if img is None or img.isNull():
            raise MaskError(f"无法解析源 SVG：{path}")
        if img.width() == size and img.height() == size:
            return img
        painter = QPainter(canvas)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.drawImage((size - img.width()) // 2, (size - img.height()) // 2, img)
        painter.end()
        return canvas
    _store_raster_report(None)
    src = load_raster_bounded(path, size, cap=MAX_MASK_PIXELS)
    if src is None:
        raise MaskError(f"无法读取源图标：{path}")
    from PyQt6.QtCore import Qt as _Qt
    src = src.convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)
    scaled = src.scaled(
        size,
        size,
        _Qt.AspectRatioMode.KeepAspectRatio,
        _Qt.TransformationMode.SmoothTransformation,
    )
    painter = QPainter(canvas)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    x = (size - scaled.width()) // 2
    y = (size - scaled.height()) // 2
    painter.drawImage(x, y, scaled)
    painter.end()
    return canvas


def _custom_alpha_mask(size: int, settings: MaskSettings):
    from PyQt6.QtCore import Qt
    from PyQt6.QtGui import QImage, QPainter

    kind, payload = _load_custom_mask_source(Path(settings.custom_path))
    canvas = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    canvas.fill(Qt.GlobalColor.transparent)
    painter = QPainter(canvas)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    if kind == "svg":
        from PyQt6.QtSvg import QSvgRenderer

        QSvgRenderer(str(payload)).render(painter)
    else:
        img = payload
        scaled = img.scaled(
            size,
            size,
            Qt.AspectRatioMode.IgnoreAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        if not scaled.hasAlphaChannel():
            # Build alpha from luminance (white = keep).
            scaled = scaled.convertToFormat(QImage.Format.Format_ARGB32)
            for y in range(scaled.height()):
                for x in range(scaled.width()):
                    c = scaled.pixelColor(x, y)
                    lum = int(round(0.299 * c.red() + 0.587 * c.green() + 0.114 * c.blue()))
                    c.setAlpha(lum)
                    scaled.setPixelColor(x, y, c)
            scaled = scaled.convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)
        painter.drawImage(0, 0, scaled)
    painter.end()
    return canvas


def apply_mask_to_image(source, size: int, settings: MaskSettings):
    """Return a new size×size QImage. Does not touch any file."""
    from PyQt6.QtCore import Qt
    from PyQt6.QtGui import QColor, QImage, QPainter, QPainterPath

    ensure_gui_app()
    validate_settings(settings)
    bg = parse_color(settings.background)
    padding = max(0.0, min(0.45, float(settings.padding)))
    if isinstance(source, Path) or isinstance(source, str):
        src = load_source_rgba(Path(source), size)
    else:
        src = source
        if src.isNull():
            raise MaskError("源图像无效")
        if src.width() != size or src.height() != size:
            src = load_source_from_qimage(src, size)

    out = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    out.fill(Qt.GlobalColor.transparent)
    painter = QPainter(out)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)

    if settings.shape == "custom":
        clip_img = _custom_alpha_mask(size, settings)
        # Draw background then icon, then punch with custom alpha.
        if bg.alpha() > 0:
            painter.fillRect(0, 0, size, size, bg)
        pad_px = int(round(size * padding))
        inner = max(1, size - 2 * pad_px)
        icon = src.scaled(
            inner,
            inner,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        x = pad_px + (inner - icon.width()) // 2
        y = pad_px + (inner - icon.height()) // 2
        painter.drawImage(x, y, icon)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationIn)
        painter.drawImage(0, 0, clip_img)
        painter.end()
        return out

    path: QPainterPath = shape_path(size, settings.shape)
    if bg.alpha() > 0:
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(bg)
        painter.drawPath(path)
    pad_px = int(round(size * padding))
    inner = max(1, size - 2 * pad_px)
    icon = src.scaled(
        inner,
        inner,
        Qt.AspectRatioMode.KeepAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )
    x = pad_px + (inner - icon.width()) // 2
    y = pad_px + (inner - icon.height()) // 2
    painter.drawImage(x, y, icon)
    painter.end()

    mask = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    mask.fill(Qt.GlobalColor.transparent)
    mp = QPainter(mask)
    mp.setRenderHint(QPainter.RenderHint.Antialiasing)
    mp.setPen(Qt.PenStyle.NoPen)
    mp.setBrush(QColor(255, 255, 255, 255))
    mp.drawPath(path)
    mp.end()

    painter = QPainter(out)
    painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationIn)
    painter.drawImage(0, 0, mask)
    painter.end()
    return out


def load_source_from_qimage(src, size: int):
    from PyQt6.QtCore import Qt
    from PyQt6.QtGui import QImage, QPainter

    canvas = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    canvas.fill(Qt.GlobalColor.transparent)
    scaled = src.scaled(
        size,
        size,
        Qt.AspectRatioMode.KeepAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )
    painter = QPainter(canvas)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    painter.drawImage((size - scaled.width()) // 2, (size - scaled.height()) // 2, scaled)
    painter.end()
    return canvas


def apply_mask_to_file(source: Path, dest: Path, size: int, settings: MaskSettings) -> Path:
    """Write a masked PNG copy to dest. Never overwrites `source` unless dest is a different path check."""
    status = describe_source(source)
    if not status.ok:
        raise MaskError(status.display)
    source = Path(source)
    dest = Path(dest)
    try:
        if dest.exists() and source.exists() and dest.resolve() == source.resolve():
            raise MaskError("拒绝把遮罩结果写回源图标文件")
    except OSError:
        pass
    img = apply_mask_to_image(source, size, settings)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not img.save(str(dest), "PNG"):
        raise MaskError(f"无法写出遮罩后的 PNG：{dest}")
    return dest


def preview_data_url(source: Path, size: int, settings: MaskSettings) -> str:
    from PyQt6.QtCore import QByteArray, QBuffer, QIODevice

    img = apply_mask_to_image(Path(source), size, settings)
    buf = QBuffer()
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    if not img.save(buf, "PNG"):
        raise MaskError("无法生成遮罩预览")
    data = bytes(buf.data())
    return "data:image/png;base64," + base64.b64encode(data).decode("ascii")


def planned_sizes(source_files, *, has_svg: bool = False) -> list[int]:
    """Sizes to emit for a masked icon. Always includes common launcher sizes."""
    sizes = set(MASK_COMMON_SIZES)
    for cand in source_files:
        size = int(getattr(cand, "size", 0) or 0)
        if size:
            sizes.add(size)
    return sorted(s for s in sizes if 0 < s <= MAX_MASK_PIXELS)


def provenance_mask_block(settings: MaskSettings) -> dict[str, Any]:
    return {
        "enabled": bool(settings.enabled),
        "shape": settings.shape,
        "custom_path": settings.custom_path if settings.shape == "custom" else "",
        "background": settings.background,
        "padding": settings.padding,
        "rasterized": bool(settings.enabled),
        "note": MASK_NOTE if settings.enabled else "未启用遮罩；导出仍可使用符号链接或原样复制。",
    }
