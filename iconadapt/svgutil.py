"""SVG diagnostics and bounded rendering for previews and export checks.

Never writes into installed icon themes. Preview may use an in-memory copy
to skip proven-noop or unsafe filter work; missing paint servers are never
invented. Export copies the original bytes and only records issues.
"""

from __future__ import annotations

import re
import sys
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator

# UI thumbnails and click previews stay at or below this.
MAX_PREVIEW_PIXELS = 256
# Masked export rasters (common sizes go up to 256; cap any extra theme size).
MAX_MASK_PIXELS = 512
# userSpaceOnUse filter boxes larger than this are treated as oversized.
MAX_FILTER_USER_UNITS = 4096

KIND_UNRESOLVED = "unresolved_reference"
KIND_FILTER_BUFFER = "filter_buffer"
KIND_EMPTY_FILTER = "empty_filter_target"
KIND_OVERSIZE_FILTER = "oversized_filter_region"
KIND_DECODE = "decode"
KIND_REF_TYPE = "reference_type_mismatch"
KIND_UNSUPPORTED_PATTERN = "unsupported_pattern"
KIND_FONT_SIZE = "font_size_underflow"

_URL_REF_RE = re.compile(
    r"""url\(\s*(?:['"]\s*)?#([^)'"\s]+)(?:\s*['"])?\s*\)""",
    re.IGNORECASE,
)
_HREF_RE = re.compile(
    r"""(?:xlink:)?href\s*=\s*['"]\s*#([^'"]+)['"]""",
    re.IGNORECASE,
)
_ID_RE = re.compile(
    r"""\b(?:xml:)?id\s*=\s*['"]([^'"]+)['"]""",
    re.IGNORECASE,
)
_TAG_WITH_ID_RE = re.compile(
    r"""<([A-Za-z][\w:.-]*)([^>]*\b(?:xml:)?id\s*=\s*['"]([^'"]+)['"][^>]*)""",
    re.IGNORECASE,
)
_PAINT_ATTR_RE = re.compile(
    r"""\b(fill|stroke|stop-color)\s*=\s*['"][^'"]*url\(\s*(?:['"]\s*)?#([^)'"\s]+)""",
    re.IGNORECASE,
)
_STYLE_PAINT_RE = re.compile(
    r"""\b(fill|stroke|stop-color)\s*:\s*url\(\s*(?:['"]\s*)?#([^)'"\s]+)""",
    re.IGNORECASE,
)
_PAINT_SERVER_TAGS = {"lineargradient", "radialgradient", "solidcolor", "meshgradient"}
_PATTERN_TAGS = {"pattern"}
_NONPAINT_PAINT_TAGS = {
    "filter", "clippath", "mask", "g", "svg", "symbol", "marker",
    "path", "rect", "circle", "ellipse", "use", "text", "image",
}

_EMPTY_FILTER_SELF_RE = re.compile(
    r"""<g\b([^>]*\bfilter\s*=\s*['"][^'"]+['"][^>]*)/>""",
    re.IGNORECASE,
)
_EMPTY_FILTER_PAIR_RE = re.compile(
    r"""<g\b([^>]*\bfilter\s*=\s*['"][^'"]+['"][^>]*)>\s*</g>""",
    re.IGNORECASE,
)
_FILTER_TAG_RE = re.compile(
    r"""<filter\b([^>]*)>""",
    re.IGNORECASE,
)
_ATTR_RE = re.compile(
    r"""([:@A-Za-z_][\w:.-]*)\s*=\s*(?:'([^']*)'|"([^"]*)")""",
)
_FILTER_ATTR_RE = re.compile(
    r"""\sfilter\s*=\s*['"][^'"]+['"]""",
    re.IGNORECASE,
)

_BUF_MARKERS = (
    "the requested buffer size is too big",
    "the requested filter is too big",
    "exceeds the current allocation limit",
)
_UNRESOLVED_RE = re.compile(r"Could not resolve property:\s*(\S+)", re.IGNORECASE)

# Per-process log filter state. Re-install is required because QML/Kirigami may replace the hook.
_lock = threading.Lock()
_installed = False
_prev_handler = None
_seen: dict[tuple[str, str], int] = {}
_emitted: dict[tuple[str, str], int] = {}
_kind_count: dict[str, int] = {}
_kind_files: dict[str, list[str]] = {}
_kind_resolved: dict[str, dict[str, int]] = {}
_kind_paths: dict[str, dict[str, int]] = {}
_summary_emitted_upto: dict[str, int] = {}
_active_sources: list[str] = []
_active_resolved: list[str] = []
_source_resolved: dict[str, str] = {}
_current_diag_source = ""
_current_diag_resolved = ""
_capturers: list[list[str]] = []


@dataclass
class SvgIssue:
    kind: str
    message: str
    detail: str = ""

    def to_dict(self) -> dict:
        return {"kind": self.kind, "message": self.message, "detail": self.detail}


def clamp_pixel_size(size: int, *, cap: int = MAX_PREVIEW_PIXELS) -> int:
    try:
        value = int(size)
    except (TypeError, ValueError):
        value = 48
    return max(16, min(value, cap))


def issue_note(issues: Iterable[SvgIssue]) -> str:
    """Short, user-facing explanation. Never claims the icon is complete."""
    issues = list(issues)
    if not issues:
        return ""
    bits: list[str] = []
    missing = sorted({i.detail for i in issues if i.kind == KIND_UNRESOLVED and i.detail})
    if missing:
        names = "、".join(f"#{m}" for m in missing)
        bits.append(f"源图标引用了不存在的图形（{names}），预览可能不完整")
    if any(i.kind == KIND_REF_TYPE for i in issues):
        names = "、".join(f"#{i.detail}" for i in issues if i.kind == KIND_REF_TYPE and i.detail)
        bits.append(
            "源图标把不是颜色的对象当作填充或描边来用"
            + (f"（{names}）" if names else "")
            + "，相应部分可能画不出来"
        )
    if any(i.kind == KIND_UNSUPPORTED_PATTERN for i in issues):
        names = "、".join(f"#{i.detail}" for i in issues if i.kind == KIND_UNSUPPORTED_PATTERN and i.detail)
        bits.append(
            "源图标使用了图案填充"
            + (f"（{names}）" if names else "")
            + "，当前 Qt SVG 引擎无法绘制该图案"
        )
    if any(i.kind in {KIND_FILTER_BUFFER, KIND_EMPTY_FILTER, KIND_OVERSIZE_FILTER} for i in issues):
        if any("先按" in i.message and "再缩小" in i.message for i in issues):
            bits.append("小尺寸下滤镜无法按原尺寸绘制，已先按更大尺寸渲染再缩小，细部可能与桌面不完全一致")
        elif any("已忽略空的滤镜元素" in i.message or i.kind == KIND_EMPTY_FILTER for i in issues):
            bits.append("源图标含无效滤镜（空元素），预览已忽略该滤镜，外观可能与桌面不完全一致")
        elif any("已简化" in i.message or "已忽略滤镜" in i.message for i in issues):
            bits.append("源图标滤镜过大或无法安全绘制，已忽略该滤镜，可能与桌面不完全一致")
        else:
            bits.append("源图标的滤镜无法按原样绘制，预览可能不完整")
    if any(i.kind == KIND_FONT_SIZE for i in issues):
        if any(i.detail == "scaled-to-keep-size" for i in issues if i.kind == KIND_FONT_SIZE):
            bits.append("源图标字号过小，已在内存中按原比例处理后绘制，未改源文件")
        else:
            bits.append("源图标文字字号过小，文字可能画不出来")
    if any(i.kind == KIND_DECODE for i in issues):
        bits.append("SVG 无法完整解码")
    # Keep unique, stable order.
    out: list[str] = []
    for bit in bits:
        if bit not in out:
            out.append(bit)
    return "。".join(out) + ("。" if out else "")


def source_issue_summary(issues_by_file: list[dict], *, limit: int = 8) -> str:
    if not issues_by_file:
        return ""
    n = len(issues_by_file)
    sample = "、".join(
        (row.get("name") or Path(row.get("path", "")).name)
        for row in issues_by_file[:limit]
    )
    alias_n = 0
    for row in issues_by_file:
        alias_n += len(row.get("aliases") or [])
    more = f"；以上为举例，不是全部，共 {n} 个真实源文件" if n > limit else ""
    alias_txt = f"，覆盖 {alias_n} 个路径/别名" if alias_n > n else ""
    return (
        f"有 {n} 个真实源文件不完整（缺引用、引用类型不对、图案无法绘制或滤镜/文字需降级等，例如 {sample}{more}{alias_txt}）。"
        "能画的部分已画出，源文件按原样复制保留，没有改你的选择，也没有改原图标包，也没有补造缺失图形。"
    )



def _local_name(tag: str) -> str:
    name = (tag or "").strip()
    if ":" in name:
        name = name.split(":")[-1]
    return name.lower()


def _id_types(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for match in _TAG_WITH_ID_RE.finditer(text or ""):
        ident = match.group(3)
        if ident and ident not in out:
            out[ident] = _local_name(match.group(1))
    return out


def _paint_reference_issues(text: str) -> list[SvgIssue]:
    """Classify fill/stroke/stop-color url(#id) when the target exists but is not a paint server."""
    if not text:
        return []
    types = _id_types(text)
    issues: list[SvgIssue] = []
    seen: set[tuple[str, str]] = set()
    refs: list[tuple[str, str]] = []
    for match in _PAINT_ATTR_RE.finditer(text):
        refs.append((match.group(1).lower(), match.group(2)))
    for match in _STYLE_PAINT_RE.finditer(text):
        refs.append((match.group(1).lower(), match.group(2)))
    for attr, ident in refs:
        if not ident or ident not in types:
            continue
        tag = types[ident]
        kind = ""
        if tag in _PATTERN_TAGS:
            kind = KIND_UNSUPPORTED_PATTERN
            message = f"源图标用图案 #{ident} 作为{attr}，当前 Qt SVG 引擎无法绘制该图案"
        elif tag in _PAINT_SERVER_TAGS:
            continue
        elif tag in _NONPAINT_PAINT_TAGS or tag:
            kind = KIND_REF_TYPE
            message = f"源图标把 #{ident}（{tag}）当作{attr}来用，该对象不是颜色或渐变，相应部分可能画不出来"
        if not kind:
            continue
        key = (kind, ident)
        if key in seen:
            continue
        seen.add(key)
        issues.append(SvgIssue(kind=kind, message=message, detail=ident))
    return issues


def refine_issues(issues: Iterable[SvgIssue], context_text: str = "") -> list[SvgIssue]:
    """Reclassify Qt 'could not resolve' using the SVG text, when the id actually exists."""
    types = _id_types(context_text) if context_text else {}
    paint = {(i.kind, i.detail): i for i in _paint_reference_issues(context_text)} if context_text else {}
    out: list[SvgIssue] = []
    seen: set[tuple[str, str]] = set()
    for issue in issues:
        kind, detail = issue.kind, issue.detail
        if kind == KIND_UNRESOLVED and detail and detail in types:
            tag = types[detail]
            replacement = None
            if (KIND_UNSUPPORTED_PATTERN, detail) in paint:
                replacement = paint[(KIND_UNSUPPORTED_PATTERN, detail)]
            elif (KIND_REF_TYPE, detail) in paint:
                replacement = paint[(KIND_REF_TYPE, detail)]
            elif tag in _PATTERN_TAGS:
                replacement = SvgIssue(
                    kind=KIND_UNSUPPORTED_PATTERN,
                    message=f"源图标用图案 #{detail} 作为填充或描边，当前 Qt SVG 引擎无法绘制该图案",
                    detail=detail,
                )
            elif tag in _PAINT_SERVER_TAGS:
                # Legal paint server that Qt still failed to resolve: keep unresolved.
                replacement = issue
            else:
                replacement = SvgIssue(
                    kind=KIND_REF_TYPE,
                    message=f"源图标把 #{detail}（{tag}）当作填充或描边来用，该对象不是颜色或渐变",
                    detail=detail,
                )
            issue = replacement
            kind, detail = issue.kind, issue.detail
        key = (kind, detail)
        if key in seen:
            continue
        seen.add(key)
        out.append(issue)
    # Static type/pattern issues must survive even when Qt did not mention them.
    for key, issue in paint.items():
        if key not in seen:
            seen.add(key)
            out.append(issue)
    return out


def record_diag_source(source: str = "", resolved: str = "") -> None:
    """Remember the path currently being rendered and its resolved real file."""
    global _current_diag_source, _current_diag_resolved
    src = (source or "").strip()
    res = (resolved or src).strip()
    if not src and not res:
        return
    with _lock:
        _current_diag_source = src or res
        _current_diag_resolved = res or src
        if src:
            _source_resolved[src] = res or src
        if res and res not in _source_resolved:
            _source_resolved[res] = res


def inspect_svg_text(text: str) -> list[SvgIssue]:
    """Static checks. Legal url(#id) / use / clipPath / gradient references pass."""
    if not text:
        return []
    issues: list[SvgIssue] = []
    ids = set(_ID_RE.findall(text))
    refs: list[str] = []
    for match in _URL_REF_RE.finditer(text):
        refs.append(match.group(1))
    for match in _HREF_RE.finditer(text):
        refs.append(match.group(1))
    missing = []
    for ref in refs:
        # Ignore empty or apparently external / fragment-less values already excluded.
        if not ref or ref.startswith(("http:", "https:", "file:", "/")):
            continue
        if ref not in ids and ref not in missing:
            missing.append(ref)
    for ref in missing:
        issues.append(
            SvgIssue(
                kind=KIND_UNRESOLVED,
                message=f"引用了不存在的图形：#{ref}",
                detail=ref,
            )
        )
    issues.extend(_paint_reference_issues(text))
    empties = list(_EMPTY_FILTER_SELF_RE.finditer(text)) + list(_EMPTY_FILTER_PAIR_RE.finditer(text))
    if empties:
        issues.append(
            SvgIssue(
                kind=KIND_EMPTY_FILTER,
                message=f"有 {len(empties)} 个空的滤镜元素（不绘制任何内容，但会让 Qt 申请无效缓冲区）",
                detail=str(len(empties)),
            )
        )
    for match in _FILTER_TAG_RE.finditer(text):
        attrs = _parse_attrs(match.group(1))
        units = (attrs.get("filterUnits") or attrs.get("filterunits") or "").strip()
        if units.lower() != "userspaceonuse":
            continue
        box = []
        for key in ("x", "y", "width", "height"):
            box.append(_num(attrs.get(key)))
        if any(v is not None and abs(v) > MAX_FILTER_USER_UNITS for v in box):
            issues.append(
                SvgIssue(
                    kind=KIND_OVERSIZE_FILTER,
                    message="滤镜区域过大，按原尺寸绘制可能失败或占用过多内存",
                    detail=",".join("" if v is None else str(v) for v in box),
                )
            )
            break
    return issues


def read_svg_text(path: Path | str) -> str | None:
    import gzip

    src = Path(path)
    try:
        raw = src.read_bytes()
    except OSError:
        return None
    if src.suffix.lower() == ".svgz":
        try:
            raw = gzip.decompress(raw)
        except OSError:
            return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("utf-8", errors="replace")


def inspect_svg_file(path: Path | str) -> list[SvgIssue]:
    text = read_svg_text(path)
    if text is None:
        return [SvgIssue(kind=KIND_DECODE, message="无法读取这个 SVG 文件")]
    return inspect_svg_text(text)


def sanitize_preview_svg(text: str, *, mode: str = "empty-filters") -> str:
    """In-memory preview copy only. Never invents paint servers or new shapes."""
    if mode == "empty-filters":
        out = _EMPTY_FILTER_SELF_RE.sub("", text)
        out = _EMPTY_FILTER_PAIR_RE.sub("", out)
        return out
    if mode == "strip-filters":
        out = _EMPTY_FILTER_SELF_RE.sub("", text)
        out = _EMPTY_FILTER_PAIR_RE.sub("", out)
        out = _FILTER_ATTR_RE.sub("", out)
        return out
    return text


def issues_from_qt_messages(messages: Iterable[str], context_text: str = "") -> list[SvgIssue]:
    issues: list[SvgIssue] = []
    seen: set[tuple[str, str]] = set()
    for raw in messages:
        msg = str(raw)
        low = msg.lower()
        unresolved = _UNRESOLVED_RE.search(msg)
        if unresolved:
            detail = unresolved.group(1).rstrip(".,;:")
            key = (KIND_UNRESOLVED, detail)
            if key not in seen:
                seen.add(key)
                issues.append(
                    SvgIssue(
                        kind=KIND_UNRESOLVED,
                        message=f"引用了不存在的图形：#{detail}",
                        detail=detail,
                    )
                )
            continue
        if any(marker in low for marker in _BUF_MARKERS):
            key = (KIND_FILTER_BUFFER, "")
            if key not in seen:
                seen.add(key)
                issues.append(
                    SvgIssue(
                        kind=KIND_FILTER_BUFFER,
                        message="源图标滤镜无法按原样绘制（内部缓冲区无效或过大）",
                    )
                )
            continue
        if "pixel size" in low and "<=" in low:
            key = (KIND_FONT_SIZE, "")
            if key not in seen:
                seen.add(key)
                issues.append(
                    SvgIssue(
                        kind=KIND_FONT_SIZE,
                        message="源图标文字字号过小，引擎无法按该字号绘制",
                        detail="qt-zero",
                    )
                )
    return refine_issues(issues, context_text=context_text) if context_text else issues


def merge_issues(*groups: Iterable[SvgIssue]) -> list[SvgIssue]:
    out: list[SvgIssue] = []
    seen: set[tuple[str, str]] = set()
    for group in groups:
        for issue in group:
            key = (issue.kind, issue.detail)
            if key in seen:
                continue
            seen.add(key)
            out.append(issue)
    return out


def install_qt_svg_log_filter() -> None:
    """(Re)install our Qt message hook. Safe if QML/Kirigami later replaced it."""
    global _installed, _prev_handler
    with _lock:
        from PyQt6.QtCore import qInstallMessageHandler

        previous = qInstallMessageHandler(_qt_message_handler)
        # Never chain to ourselves (that would recurse if we re-install).
        if previous is not _qt_message_handler:
            _prev_handler = previous
        _installed = True


def reset_svg_log_dedup() -> None:
    global _current_diag_source, _current_diag_resolved
    with _lock:
        _seen.clear()
        _emitted.clear()
        _kind_count.clear()
        _kind_files.clear()
        _kind_resolved.clear()
        _kind_paths.clear()
        _source_resolved.clear()
        _current_diag_source = ""
        _current_diag_resolved = ""
        _summary_emitted_upto.clear()


def svg_log_stats() -> dict:
    with _lock:
        return {
            "seen": int(sum(_seen.values())),
            "emitted": int(sum(_emitted.values())),
            "unique": len(_seen),
            "kinds": {k: int(v) for k, v in _kind_count.items()},
            "unique_sources": {k: len(v) for k, v in _kind_resolved.items()},
            "unique_paths": {k: len(v) for k, v in _kind_paths.items()},
        }


def svg_log_count(source: str, kind: str) -> tuple[int, int]:
    """Return (seen, emitted) for keys that match this source and kind token."""
    with _lock:
        seen = sum(v for (src, key), v in _seen.items() if src == source and kind in key)
        emitted = sum(v for (src, key), v in _emitted.items() if src == source and kind in key)
        return seen, emitted


@contextmanager
def capture_svg_logs(source: str = "") -> Iterator[list[str]]:
    install_qt_svg_log_filter()
    bucket: list[str] = []
    with _lock:
        _capturers.append(bucket)
        if source:
            _active_sources.append(source)
    try:
        yield bucket
    finally:
        with _lock:
            if _capturers and _capturers[-1] is bucket:
                _capturers.pop()
            elif bucket in _capturers:
                _capturers.remove(bucket)
            if source and _active_sources and _active_sources[-1] == source:
                _active_sources.pop()
            elif source and source in _active_sources:
                _active_sources.remove(source)


def render_svg_bytes(data: bytes, size: int, *, source: str = "", cap: int = MAX_PREVIEW_PIXELS):
    """Rasterize SVG into a bounded QImage. Returns (image or None, qt messages)."""
    from PyQt6.QtCore import QByteArray, Qt
    from PyQt6.QtGui import QImage, QPainter
    from PyQt6.QtSvg import QSvgRenderer

    size = clamp_pixel_size(size, cap=cap)
    with capture_svg_logs(source) as messages:
        renderer = QSvgRenderer(QByteArray(data))
        if not renderer.isValid():
            return None, list(messages)
        img = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
        if img.isNull():
            return None, list(messages)
        img.fill(Qt.GlobalColor.transparent)
        painter = QPainter(img)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        renderer.render(painter)
        painter.end()
    return img, list(messages)


def load_raster_bounded(path: Path, size: int, *, cap: int = MAX_PREVIEW_PIXELS):
    """Load a bitmap without allocating the original pixel grid when it is huge."""
    from PyQt6.QtCore import QSize, Qt
    from PyQt6.QtGui import QImageReader

    size = clamp_pixel_size(size, cap=cap)
    reader = QImageReader(str(path))
    if not reader.canRead():
        return None
    orig = reader.size()
    if orig.isValid() and orig.width() > 0 and orig.height() > 0:
        scaled = orig.scaled(size, size, Qt.AspectRatioMode.KeepAspectRatio)
        # Never ask the reader for a target larger than the cap.
        if scaled.width() > cap or scaled.height() > cap:
            scaled = orig.scaled(cap, cap, Qt.AspectRatioMode.KeepAspectRatio)
        reader.setScaledSize(QSize(max(1, scaled.width()), max(1, scaled.height())))
    else:
        reader.setScaledSize(QSize(size, size))
    image = reader.read()
    if image.isNull():
        return None
    return image


def _parse_attrs(blob: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for match in _ATTR_RE.finditer(blob or ""):
        out[match.group(1)] = match.group(2) if match.group(2) is not None else (match.group(3) or "")
    return out


def _num(raw: str | None) -> float | None:
    if raw is None or raw == "":
        return None
    token = re.split(r"[\s,]+", raw.strip())[0]
    token = re.sub(r"[a-zA-Z%]+$", "", token)
    try:
        return float(token)
    except ValueError:
        return None


def _normalize_svg_message(message: str) -> str:
    text = str(message)
    unresolved = _UNRESOLVED_RE.search(text)
    if unresolved:
        return f"unresolved:{unresolved.group(1).rstrip('.,;:')}"
    low = text.lower()
    if any(marker in low for marker in _BUF_MARKERS):
        return "filter_buffer"
    if "pixel size" in low and "<=" in low:
        return KIND_FONT_SIZE
    # Keep path-specific Qt messages stable enough to dedup per file.
    # Drop varying addresses; keep the rest.
    return re.sub(r"0x[0-9a-fA-F]+", "0x?", text).strip()


def _path_from_message(message: str) -> str:
    match = re.search(r"(/[^\s:]+\.svgz?):", message)
    return match.group(1) if match else ""


def _rewrite_diag_message(message: str, source: str) -> str:
    """Replace Qt's <input> (QByteArray loads) with the real source path."""
    text = str(message)
    src = (source or "").strip()
    if not src:
        return text
    if "<input>" in text:
        return text.replace("<input>", src)
    if _path_from_message(text):
        return text
    return f"{src}: {text}"


def flush_svg_log_summary() -> list[str]:
    """Print a count of repeated Qt SVG warning kinds, with example paths."""
    out: list[str] = []
    with _lock:
        for kind, count in sorted(_kind_count.items()):
            already = _summary_emitted_upto.get(kind, 0)
            if count <= 1 or count <= already:
                continue
            paths = [f for f in _kind_paths.get(kind, {}) if f]
            resolved = [f for f in _kind_resolved.get(kind, {}) if f]
            examples = paths[:3] or resolved[:3] or [f for f in _kind_files.get(kind, []) if f][:3]
            shown = "、".join(examples)
            n_src = len(resolved) or len({_source_resolved.get(f, f) for f in paths if f})
            n_path = len(paths) or len([f for f in _kind_files.get(kind, []) if f])
            if not examples:
                extra = ""
            elif n_src > 3 or n_path > 3 or len(examples) < n_path:
                extra = (
                    f"，例如 {shown}；以上为举例，不是全部，"
                    f"共 {n_src} 个真实源、{n_path} 个路径/别名"
                )
            else:
                extra = f"（{shown}；{n_src} 个真实源、{n_path} 个路径）"
            out.append(f"[icon-adapt] 同类 Qt SVG 警告「{kind}」共 {count} 次{extra}")
            _summary_emitted_upto[kind] = count
    for line in out:
        try:
            sys.stderr.write(line + chr(10))
        except Exception:
            pass
    return out


def _is_svg_diag(message: str) -> bool:
    low = message.lower()
    if any(marker in low for marker in _BUF_MARKERS):
        return True
    if "could not resolve property" in low:
        return True
    if "skipping unknown element" in low:
        return True
    if "too long to rasterize" in low:
        return True
    if "pixel size" in low and "<=" in low:
        return True
    return False


def _qt_message_handler(mode, context, message) -> None:
    text = str(message)
    with _lock:
        source = _current_diag_source or (
            _active_sources[-1] if _active_sources else _path_from_message(text)
        )
        is_diag = _is_svg_diag(text)
        rewritten = _rewrite_diag_message(text, source) if is_diag else text
        for bucket in _capturers:
            bucket.append(rewritten)
        emit = True
        if is_diag:
            kind = _normalize_svg_message(text)
            key = (source, kind)
            _seen[key] = _seen.get(key, 0) + 1
            _kind_count[kind] = _kind_count.get(kind, 0) + 1
            resolved = _current_diag_resolved or _source_resolved.get(source) or (
                _active_resolved[-1] if _active_resolved else source
            )
            if source:
                files = _kind_files.setdefault(kind, [])
                if source not in files and len(files) < 8:
                    files.append(source)
                _kind_paths.setdefault(kind, {})
                _kind_paths[kind][source] = _kind_paths[kind].get(source, 0) + 1
            if resolved:
                _kind_resolved.setdefault(kind, {})
                _kind_resolved[kind][resolved] = _kind_resolved[kind].get(resolved, 0) + 1
            if _kind_count[kind] == 1:
                _emitted[key] = _emitted.get(key, 0) + 1
                emit = True
            else:
                emit = False
        prev = _prev_handler
    if not emit:
        return
    if prev is not None:
        try:
            prev(mode, context, rewritten)
        except TypeError:
            prev(mode, context, message)
        return
    try:
        sys.stderr.write(rewritten + chr(10))
    except Exception:
        pass


__all__ = [
    "MAX_MASK_PIXELS",
    "MAX_PREVIEW_PIXELS",
    "KIND_DECODE",
    "KIND_EMPTY_FILTER",
    "KIND_FILTER_BUFFER",
    "KIND_OVERSIZE_FILTER",
    "KIND_UNRESOLVED",
    "KIND_REF_TYPE",
    "KIND_UNSUPPORTED_PATTERN",
    "KIND_FONT_SIZE",
    "SvgIssue",
    "record_diag_source",
    "refine_issues",
    "capture_svg_logs",
    "clamp_pixel_size",
    "inspect_svg_file",
    "inspect_svg_text",
    "flush_svg_log_summary",
    "install_qt_svg_log_filter",
    "issue_note",
    "issues_from_qt_messages",
    "load_raster_bounded",
    "merge_issues",
    "render_svg_bytes",
    "reset_svg_log_dedup",
    "sanitize_preview_svg",
    "source_issue_summary",
    "svg_log_count",
    "svg_log_stats",
]
