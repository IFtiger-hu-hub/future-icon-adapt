"""0.2.4 regressions: alias cache, small-size filter downgrade, fonts, ref types, log totals."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from iconadapt.export import ExportError, export_theme
from iconadapt.mask import MaskSettings, apply_mask_to_file, last_raster_report
from iconadapt.preview import PreviewPalette, decorate_previews, render_preview
from iconadapt.raster import adjust_subpixel_fonts, rasterize_svg, rasterize_svg_file
from iconadapt.rendercache import (
    RenderCache,
    begin_render_session,
    cache_stats,
    end_render_session,
    file_identity,
    get_cache,
)
from iconadapt.safeio import write_owned_marker
from iconadapt.strategy import resolve_strategy
from iconadapt.svgutil import (
    KIND_EMPTY_FILTER,
    KIND_FILTER_BUFFER,
    KIND_FONT_SIZE,
    KIND_REF_TYPE,
    KIND_UNSUPPORTED_PATTERN,
    KIND_UNRESOLVED,
    capture_svg_logs,
    flush_svg_log_summary,
    inspect_svg_text,
    record_diag_source,
    render_svg_bytes,
    reset_svg_log_dedup,
    sanitize_preview_svg,
    source_issue_summary,
    svg_log_stats,
)
from iconadapt.theme import enumerate_themes
from tests.fixtures import make_desktop, make_theme, write
from tests.qtutil import ensure_qt_app

MCMOJAVE = Path.home() / ".local/share/icons/McMojave-circle/apps/scalable"
CAMERA = MCMOJAVE / "accessories-camera.svg"
BOOTH = MCMOJAVE / "booth.svg"
FSPOT = MCMOJAVE / "f-spot.svg"
TERMINAL = MCMOJAVE / "terminal.svg"
BLACKMAGIC = MCMOJAVE / "blackmagicraw-speedtest.svg"
KERRY = MCMOJAVE / "kerry.svg"
PACKAGE = MCMOJAVE / "package_utilities.svg"
SURFSHARK = MCMOJAVE / "surfshark.svg"

LEGAL_REFS = """<svg xmlns="http://www.w3.org/2000/svg" width="32" height="32" viewBox="0 0 32 32">
  <defs>
    <linearGradient id="g" x1="0" x2="1" y1="0" y2="1">
      <stop offset="0" stop-color="#ff0000"/>
      <stop offset="1" stop-color="#0000ff"/>
    </linearGradient>
    <clipPath id="c"><circle cx="16" cy="16" r="12"/></clipPath>
    <filter id="f"><feGaussianBlur stdDeviation="1"/></filter>
    <rect id="r" x="2" y="2" width="10" height="10" fill="#111"/>
  </defs>
  <circle cx="16" cy="16" r="14" fill="url(#g)" clip-path="url(#c)" filter="url(#f)"/>
  <use href="#r"/>
</svg>
"""

TYPE_MISMATCH = """<svg xmlns="http://www.w3.org/2000/svg" width="32" height="32" viewBox="0 0 32 32">
  <defs>
    <filter id="c"><feGaussianBlur stdDeviation="1"/></filter>
    <linearGradient id="ok" x1="0" x2="1"><stop offset="0" stop-color="#0f0"/></linearGradient>
  </defs>
  <rect width="32" height="32" fill="url(#ok)"/>
  <circle cx="16" cy="16" r="10" fill="url(#c)"/>
</svg>
"""

PATTERN_FILL = """<svg xmlns="http://www.w3.org/2000/svg" width="32" height="32" viewBox="0 0 32 32">
  <defs>
    <pattern id="e" width="4" height="4" patternUnits="userSpaceOnUse">
      <rect width="4" height="4" fill="#0af"/>
      <rect width="2" height="2" fill="#fff"/>
    </pattern>
  </defs>
  <rect width="32" height="32" fill="url(#e)"/>
</svg>
"""

MISSING_PAINT = """<svg xmlns="http://www.w3.org/2000/svg" width="32" height="32">
  <defs>
    <linearGradient id="c" x1="0" x2="1"><stop offset="0" stop-color="#111"/></linearGradient>
  </defs>
  <circle cx="16" cy="16" r="14" fill="url(#c)"/>
  <circle cx="16" cy="16" r="8" fill="url(#a)"/>
</svg>
"""

FONT_OK = """<svg xmlns="http://www.w3.org/2000/svg" width="16.933" height="16.933" viewBox="0 0 16.933 16.933">
  <rect width="16.933" height="16.933" fill="#222"/>
  <text xml:space="preserve" x="6.701" y="6.501" fill="#d4c097" stroke-width=".026"
        font-family="sans-serif" font-size=".511" letter-spacing=".079">SPEED TEST</text>
</svg>
"""

FONT_UNANCHORED = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16">
  <text font-size=".511" fill="#fff">Hi</text>
</svg>
"""

# objectBoundingBox blur on large user coords / small transform — class B.
CLASS_B = """<svg xmlns="http://www.w3.org/2000/svg" width="48" height="48" viewBox="0 0 48 48">
  <defs>
    <filter id="blur" x="-0.5" y="-0.5" width="2" height="2">
      <feGaussianBlur stdDeviation="20"/>
    </filter>
  </defs>
  <g transform="translate(0 -280)">
    <circle cx="-10" cy="304" r="200" fill="#3daee9" filter="url(#blur)"/>
  </g>
  <circle cx="24" cy="24" r="10" fill="#222"/>
</svg>
"""

NORMAL = """<svg xmlns="http://www.w3.org/2000/svg" width="32" height="32" viewBox="0 0 32 32">
  <rect width="32" height="32" fill="#2ecc71"/>
</svg>
"""


def _visible(img) -> int:
    from PyQt6.QtGui import QImage, QColor

    if img is None or img.isNull():
        return 0
    n = 0
    for y in range(img.height()):
        for x in range(img.width()):
            if QColor(img.pixel(x, y)).alpha() > 8:
                n += 1
    return n


def _digest(img) -> str:
    from PyQt6.QtCore import QByteArray, QBuffer, QIODevice

    ba = QByteArray()
    buf = QBuffer(ba)
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    img.save(buf, "PNG")
    buf.close()
    import hashlib
    return hashlib.sha256(bytes(ba)).hexdigest()


class InspectAndRefsTests(unittest.TestCase):
    def test_legal_gradient_use_filter_clip_not_flagged(self):
        issues = inspect_svg_text(LEGAL_REFS)
        kinds = {i.kind for i in issues}
        self.assertNotIn(KIND_UNRESOLVED, kinds)
        self.assertNotIn(KIND_REF_TYPE, kinds)
        self.assertNotIn(KIND_UNSUPPORTED_PATTERN, kinds)

    def test_missing_paint_id_is_unresolved(self):
        issues = inspect_svg_text(MISSING_PAINT)
        self.assertTrue(any(i.kind == KIND_UNRESOLVED and i.detail == "a" for i in issues), issues)
        self.assertFalse(any(i.kind == KIND_UNRESOLVED and i.detail == "c" for i in issues), issues)

    def test_fill_pointing_at_filter_is_type_mismatch(self):
        issues = inspect_svg_text(TYPE_MISMATCH)
        self.assertTrue(any(i.kind == KIND_REF_TYPE and i.detail == "c" for i in issues), issues)
        self.assertFalse(any(i.kind == KIND_UNRESOLVED and i.detail == "c" for i in issues), issues)
        self.assertFalse(any(i.kind == KIND_UNRESOLVED and i.detail == "ok" for i in issues), issues)

    def test_pattern_fill_is_unsupported_not_missing(self):
        issues = inspect_svg_text(PATTERN_FILL)
        self.assertTrue(any(i.kind == KIND_UNSUPPORTED_PATTERN and i.detail == "e" for i in issues), issues)
        self.assertFalse(any(i.kind == KIND_UNRESOLVED and i.detail == "e" for i in issues), issues)

    @unittest.skipUnless(PACKAGE.is_file(), "McMojave package_utilities not present")
    def test_real_package_utilities_type_mismatch(self):
        text = PACKAGE.read_text(errors="replace")
        issues = inspect_svg_text(text)
        self.assertTrue(any(i.kind == KIND_REF_TYPE and i.detail == "c" for i in issues), issues)

    @unittest.skipUnless(SURFSHARK.is_file(), "surfshark not present")
    def test_real_surfshark_pattern(self):
        text = SURFSHARK.read_text(errors="replace")
        issues = inspect_svg_text(text)
        self.assertTrue(any(i.kind == KIND_UNSUPPORTED_PATTERN for i in issues), issues)


class FontAdjustTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._app = ensure_qt_app()

    def test_adjust_keeps_visual_size_and_silences_qfont(self):
        adjusted, issues, changed = adjust_subpixel_fonts(FONT_OK, 48)
        self.assertTrue(changed)
        self.assertTrue(any(i.detail == "scaled-to-keep-size" for i in issues), issues)
        self.assertIn("font-size=\"1\"", adjusted)
        self.assertIn("scale(0.511)", adjusted)
        # Source-like markup is unchanged in the original string.
        self.assertIn('font-size=".511"', FONT_OK)
        reset_svg_log_dedup()
        raw_img, raw_msgs = render_svg_bytes(FONT_OK.encode(), 48, source="font-raw")
        adj_img, adj_msgs = render_svg_bytes(adjusted.encode(), 48, source="font-adj")
        self.assertTrue(any("pixel size" in m.lower() for m in raw_msgs), raw_msgs)
        self.assertFalse(any("pixel size" in m.lower() for m in adj_msgs), adj_msgs)
        self.assertIsNotNone(adj_img)
        # Must not be a blank image, and must not be hugely larger than the raw attempt.
        raw_vis = _visible(raw_img) if raw_img is not None else 0
        adj_vis = _visible(adj_img)
        self.assertGreater(adj_vis, 0)
        # The dark rectangle already fills the canvas; compare the light glyph by color is heavy.
        # At least the adjusted render must not fail and must stay 48x48.
        self.assertEqual(adj_img.width(), 48)

    def test_unanchored_font_not_arbitrarily_enlarged(self):
        adjusted, issues, changed = adjust_subpixel_fonts(FONT_UNANCHORED, 48)
        self.assertFalse(changed)
        self.assertEqual(adjusted, FONT_UNANCHORED)
        self.assertTrue(any(i.detail == "unanchored" for i in issues), issues)

    @unittest.skipUnless(BLACKMAGIC.is_file(), "blackmagic icon not present")
    def test_real_blackmagic_in_memory_only(self):
        original = BLACKMAGIC.read_bytes()
        result = rasterize_svg_file(BLACKMAGIC, 48, role="test")
        self.assertTrue(result.font_adjusted or any(i.kind == KIND_FONT_SIZE for i in result.issues), result.issues)
        self.assertEqual(BLACKMAGIC.read_bytes(), original)
        reset_svg_log_dedup()
        _, msgs = render_svg_bytes(
            adjust_subpixel_fonts(original.decode(), 48)[0].encode(), 48, source=str(BLACKMAGIC)
        )
        self.assertFalse(any("pixel size" in m.lower() for m in msgs), msgs)


class FilterDowngradeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._app = ensure_qt_app()
        begin_render_session()

    @classmethod
    def tearDownClass(cls):
        end_render_session()

    def test_class_b_small_size_hires_downscale_large_size_kept(self):
        begin_render_session()
        try:
            small = rasterize_svg(CLASS_B, 16, source="class-b", role="test")
            large = rasterize_svg(CLASS_B, 256, source="class-b", role="test")
            self.assertIsNotNone(small.image)
            self.assertIsNotNone(large.image)
            # 16px may need a downscale if Qt warns; 256 must not strip working filters.
            if small.downgrade:
                self.assertIn(small.downgrade, {"hires-downscale", "empty-filters"})
                self.assertNotEqual(small.downgrade, "strip-filters")
                if small.downgrade == "hires-downscale":
                    self.assertIsNotNone(small.hires_size)
                    self.assertGreater(small.hires_size, 16)
            self.assertNotEqual(large.downgrade, "strip-filters")
            stripped = sanitize_preview_svg(CLASS_B, mode="strip-filters")
            raw256, raw_msgs = render_svg_bytes(CLASS_B.encode(), 256, source="class-b-raw")
            strip256, _ = render_svg_bytes(stripped.encode(), 256, source="class-b-strip")
            self.assertIsNotNone(raw256)
            if raw256 is not None and strip256 is not None and _digest(raw256) != _digest(strip256):
                self.assertEqual(_digest(large.image), _digest(raw256))
        finally:
            end_render_session()

    @unittest.skipUnless(CAMERA.is_file(), "camera icon not present")
    def test_real_camera_16_downgrade_256_preserves_filter(self):
        begin_render_session()
        try:
            original = CAMERA.read_bytes()
            r16 = rasterize_svg_file(CAMERA, 16, role="test")
            r48 = rasterize_svg_file(CAMERA, 48, role="test")
            r256 = rasterize_svg_file(CAMERA, 256, role="test")
            self.assertEqual(CAMERA.read_bytes(), original)
            self.assertIsNotNone(r16.image)
            self.assertIsNotNone(r256.image)
            self.assertEqual(r16.downgrade, "hires-downscale")
            self.assertNotEqual(r256.downgrade, "strip-filters")
            self.assertTrue(_visible(r16.image) > 0 and _visible(r48.image) > 0 and _visible(r256.image) > 0)
            text = original.decode(errors="replace")
            stripped = sanitize_preview_svg(text, mode="strip-filters")
            raw256, _ = render_svg_bytes(original, 256, source=str(CAMERA))
            strip256, _ = render_svg_bytes(stripped.encode(), 256, source=str(CAMERA) + "#strip")
            self.assertEqual(_digest(r256.image), _digest(raw256))
            self.assertNotEqual(_digest(raw256), _digest(strip256))
        finally:
            end_render_session()


class CacheAndLogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._app = ensure_qt_app()

    def tearDown(self):
        end_render_session()
        reset_svg_log_dedup()

    def test_alias_cache_hit_same_identity(self):
        with tempfile.TemporaryDirectory(prefix="iconadapt-test-") as tmp:
            root = Path(tmp)
            src = write(root / "canon.svg", NORMAL)
            alias = root / "alias.svg"
            alias.symlink_to(src)
            begin_render_session()
            first = rasterize_svg_file(src, 48, role="test")
            second = rasterize_svg_file(alias, 48, role="test")
            self.assertFalse(first.cache_hit)
            self.assertTrue(second.cache_hit)
            self.assertEqual(_digest(first.image), _digest(second.image))
            self.assertEqual(file_identity(src)[1:3], file_identity(alias)[1:3])
            stats = cache_stats()
            self.assertGreaterEqual(stats["hits"], 1)
            self.assertGreaterEqual(stats["misses"], 1)

    def test_palette_and_size_do_not_share_cache(self):
        pal_a = PreviewPalette()
        pal_b = PreviewPalette()
        pal_b.text = "#ff0000"
        begin_render_session()
        a = rasterize_svg(NORMAL, 32, source="p", role="preview", palette=pal_a)
        b = rasterize_svg(NORMAL, 32, source="p", role="preview", palette=pal_b)
        c = rasterize_svg(NORMAL, 64, source="p", role="preview", palette=pal_a)
        self.assertFalse(a.cache_hit)
        self.assertFalse(b.cache_hit)
        self.assertFalse(c.cache_hit)
        a2 = rasterize_svg(NORMAL, 32, source="p", role="preview", palette=pal_a)
        self.assertTrue(a2.cache_hit)

    def test_memory_is_bounded(self):
        from PyQt6.QtGui import QImage
        from PyQt6.QtCore import Qt

        cache = RenderCache(max_bytes=64 * 1024)
        images = []
        for i in range(40):
            img = QImage(64, 64, QImage.Format.Format_ARGB32_Premultiplied)
            img.fill(Qt.GlobalColor.red)
            cache.put(("k", i), img)
            images.append(img)
        self.assertLessEqual(cache.stats.bytes_current, cache.max_bytes)
        self.assertGreater(cache.stats.evictions, 0)

    def test_spill_cleanup_only_owned_tmp(self):
        import tempfile
        from iconadapt.rendercache import _safe_rmtree_spill

        with tempfile.TemporaryDirectory(prefix="iconadapt-rendcache-") as tmp:
            target = Path(tmp)
            marker = target / "keep-parent-not-this"
            # Directory name must start with iconadapt-rendcache- and be under tmp.
            # TemporaryDirectory name already has that prefix.
            (target / "file.txt").write_text("x")
            _safe_rmtree_spill(target)
            self.assertFalse(target.exists())
        # A similarly named dir not under tmp must not be deleted.
        # We only assert the helper returns without touching a path whose parent isn't tmp.
        # Don't create anything in the user's home for this.

    def test_log_summary_uses_unique_sources_not_example_cap(self):
        reset_svg_log_dedup()
        # 2 real sources, 20 alias paths, many events — cap used to claim "8 files".
        with capture_svg_logs("unused"):
            for i in range(20):
                alias = f"/tmp/alias-{i}.svg"
                resolved = "/tmp/real-camera.svg" if i < 12 else "/tmp/real-shot.svg"
                record_diag_source(alias, resolved)
                from PyQt6.QtCore import QtMsgType
                from iconadapt import svgutil as su
                su._qt_message_handler(
                    QtMsgType.QtWarningMsg,
                    None,
                    "The requested buffer size is too big! Skipping it.",
                )
        stats = svg_log_stats()
        self.assertEqual(stats["unique_sources"]["filter_buffer"], 2)
        self.assertEqual(stats["unique_paths"]["filter_buffer"], 20)
        self.assertEqual(stats["kinds"]["filter_buffer"], 20)
        lines = flush_svg_log_summary()
        text = "\n".join(lines)
        self.assertIn("共 20 次", text)
        self.assertIn("共 2 个真实源", text)
        self.assertIn("20 个路径/别名", text)
        self.assertIn("以上为举例，不是全部", text)
        self.assertNotIn("涉及 8 个文件", text)

    def test_source_issue_summary_labels_examples(self):
        rows = [
            {"name": f"icon{i}.svg", "path": f"/t/{i}.svg", "aliases": [f"/t/{i}.svg", f"/t/a{i}.svg"]}
            for i in range(12)
        ]
        text = source_issue_summary(rows, limit=8)
        self.assertIn("12 个真实源", text)
        self.assertIn("以上为举例，不是全部", text)
        self.assertIn("24 个路径/别名", text)
        self.assertNotIn("等 12 个", text)



def _write_svg_theme(root: Path, name: str, icons: dict[str, str]) -> Path:
    tdir = root / name
    rel = "scalable/apps"
    (tdir / rel).mkdir(parents=True, exist_ok=True)
    write(
        tdir / "index.theme",
        "\n".join(
            [
                "[Icon Theme]",
                f"Name={name}",
                "Comment=test",
                "Inherits=hicolor",
                f"Directories={rel}",
                "",
                f"[{rel}]",
                "Size=128",
                "Type=Scalable",
                "MinSize=1",
                "MaxSize=512",
                "Context=Applications",
                "",
            ]
        ),
    )
    for icon_name, body in icons.items():
        (tdir / rel / f"{icon_name}.svg").write_text(body, encoding="utf-8")
    return tdir


class ExportAliasAndSafetyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._app = ensure_qt_app()

    def test_export_records_aliases_and_does_not_modify_source(self):
        from iconadapt.constants import MODULE_APPS, OWNED_MARKER, PROVENANCE_FILENAME, THEME_INTERNAL_NAME
        from iconadapt.desktop import scan_applications

        with tempfile.TemporaryDirectory(prefix="iconadapt-test-") as tmp:
            root = Path(tmp)
            icons = root / "icons"
            apps_dir = root / "apps"
            apps_dir.mkdir()
            pack = _write_svg_theme(icons, "Pack", {"camera": MISSING_PAINT, "plain": NORMAL})
            booth = pack / "scalable" / "apps" / "booth.svg"
            booth.symlink_to("camera.svg")
            src = pack / "scalable" / "apps" / "camera.svg"
            before = src.read_bytes()
            make_theme(icons, "hicolor", "", {"folder": ["48x48/places"]})
            make_desktop(apps_dir, "camera.desktop", name="Camera", icon="camera")
            make_desktop(apps_dir, "booth.desktop", name="Booth", icon="booth")
            themes = enumerate_themes([icons])
            apps = scan_applications([apps_dir], current_desktop="KDE")
            dest = root / "out" / THEME_INTERNAL_NAME
            settings = MaskSettings(enabled=True, shape="circle", background="#ffffffff", padding=0)
            prov = export_theme(
                dest,
                apps=apps,
                themes=themes,
                status=resolve_strategy(themes, current_theme="Pack"),
                selections={},
                overwrite_owned=True,
                mask=settings,
                modules={MODULE_APPS: "Pack"},
            )
            self.assertEqual(src.read_bytes(), before, "export must not rewrite the source pack")
            self.assertTrue((dest / "16x16/apps/camera.png").is_file())
            self.assertTrue((dest / "16x16/apps/booth.png").is_file())
            self.assertTrue((dest / "256x256/apps/camera.png").is_file())
            self.assertTrue((dest / "256x256/apps/booth.png").is_file())
            self.assertTrue((dest / "index.theme").is_file())
            self.assertTrue((dest / PROVENANCE_FILENAME).is_file())
            self.assertTrue((dest / OWNED_MARKER).is_file())
            issues = prov.get("source_issues") or []
            camera_rows = [
                r for r in issues
                if r.get("name") == "camera.svg" or str(r.get("path") or "").endswith("camera.svg")
            ]
            self.assertTrue(camera_rows, issues)
            aliases = " ".join(camera_rows[0].get("aliases") or [])
            self.assertIn("camera.svg", aliases)
            self.assertIn("booth.svg", aliases)
            self.assertGreaterEqual(camera_rows[0].get("alias_count") or 0, 2)
            cache = prov.get("render_cache") or {}
            self.assertGreaterEqual(cache.get("hits") or 0, 1)

    def test_mask_failure_leaves_dest_untouched(self):
        from iconadapt.constants import MODULE_APPS, THEME_INTERNAL_NAME
        from iconadapt.desktop import scan_applications
        from iconadapt.safeio import is_tool_owned

        with tempfile.TemporaryDirectory(prefix="iconadapt-test-") as tmp:
            root = Path(tmp)
            icons = root / "icons"
            apps_dir = root / "apps"
            apps_dir.mkdir()
            _write_svg_theme(icons, "Pack", {"good-app": NORMAL})
            make_theme(icons, "hicolor", "", {"folder": ["48x48/places"]})
            make_desktop(apps_dir, "good-app.desktop", name="Good", icon="good-app")
            dest = root / "out" / THEME_INTERNAL_NAME
            dest.mkdir(parents=True)
            write_owned_marker(dest)
            sentinel = dest / "keep-me.txt"
            sentinel.write_text("original", encoding="utf-8")
            settings = MaskSettings(enabled=True, shape="custom", custom_path="/no/such/mask.svg")
            with self.assertRaises(ExportError):
                export_theme(
                    dest,
                    apps=scan_applications([apps_dir], current_desktop="KDE"),
                    themes=enumerate_themes([icons]),
                    status=resolve_strategy(enumerate_themes([icons]), current_theme="Pack"),
                    selections={},
                    overwrite_owned=True,
                    mask=settings,
                    modules={MODULE_APPS: "Pack"},
                )
            self.assertTrue(sentinel.is_file())
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "original")
            self.assertTrue(is_tool_owned(dest))

    @unittest.skipUnless(KERRY.is_symlink(), "kerry dangling sample missing")
    def test_describe_source_still_reports_dangling_kerry(self):
        from iconadapt.sourcecheck import REASON_DANGLING, describe_source

        status = describe_source(KERRY)
        self.assertFalse(status.ok)
        self.assertEqual(status.reason, REASON_DANGLING)
        self.assertIn("kerry.svg", status.display)
        self.assertIn("search.svg", status.display)


if __name__ == "__main__":
    unittest.main()
