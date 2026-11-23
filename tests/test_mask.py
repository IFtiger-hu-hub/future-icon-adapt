import json
import tempfile
import unittest
from pathlib import Path

from iconadapt.constants import OWNED_MARKER, THEME_INTERNAL_NAME
from iconadapt.desktop import scan_applications
from iconadapt.export import ExportError, export_theme, is_tool_owned, write_owned_marker
from iconadapt.mask import (
    MASK_NOTE,
    MaskError,
    MaskSettings,
    apply_mask_to_file,
    apply_mask_to_image,
    load_settings,
    save_settings,
    validate_settings,
)
from iconadapt.persist import atomic_write_json
from iconadapt.strategy import resolve_strategy
from iconadapt.theme import enumerate_themes
from tests.fixtures import make_desktop, make_theme, write, write_bytes, PNG_1x1
from tests.qtutil import ensure_qt_app


def _solid_png(path: Path, size: int, r=255, g=255, b=255, a=255) -> Path:
    from PyQt6.QtGui import QColor, QImage

    ensure_qt_app()
    img = QImage(size, size, QImage.Format.Format_ARGB32)
    img.fill(QColor(r, g, b, a))
    path.parent.mkdir(parents=True, exist_ok=True)
    if not img.save(str(path), "PNG"):
        raise RuntimeError("save failed")
    return path


def _alpha(img, x, y) -> int:
    return img.pixelColor(x, y).alpha()


class MaskGeometryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = ensure_qt_app()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.src = _solid_png(self.root / "src.png", 64, 255, 255, 255, 255)

    def tearDown(self):
        self.tmp.cleanup()

    def test_circle_cuts_corners_keeps_center(self):
        img = apply_mask_to_image(self.src, 64, MaskSettings(enabled=True, shape="circle", padding=0, background="#00000000"))
        self.assertEqual(_alpha(img, 32, 32), 255)
        self.assertEqual(_alpha(img, 0, 0), 0)
        self.assertEqual(_alpha(img, 63, 0), 0)
        # Mid-top pixel sits on the antialiased tangent; a clearly outside corner is enough.

    def test_rounded_square_and_squircle_differ_from_circle(self):
        circle = apply_mask_to_image(self.src, 64, MaskSettings(enabled=True, shape="circle", padding=0))
        rounded = apply_mask_to_image(self.src, 64, MaskSettings(enabled=True, shape="rounded-square", padding=0))
        squircle = apply_mask_to_image(self.src, 64, MaskSettings(enabled=True, shape="squircle", padding=0))
        # Near the top edge but off-center: inside a rounded square's straight edge, outside the circle.
        self.assertGreater(_alpha(rounded, 16, 2), 200)
        self.assertLess(_alpha(circle, 16, 2), 32)
        # Squircle is fuller toward the corner than a circle, still empty at the extreme corner.
        self.assertEqual(_alpha(squircle, 0, 0), 0)
        self.assertGreater(_alpha(squircle, 6, 6), _alpha(circle, 6, 6))

    def test_background_and_padding_fill_the_ring(self):
        # Qt #AARRGGBB : opaque red
        img = apply_mask_to_image(
            self.src,
            64,
            MaskSettings(enabled=True, shape="circle", padding=0.25, background="#ffff0000"),
        )
        c = img.pixelColor(32, 32)
        self.assertGreater(c.alpha(), 200)
        self.assertGreater(c.red(), 200)
        self.assertGreater(c.green(), 200)  # white icon in the center
        # A point well inside the circle but in the padding ring
        ring = img.pixelColor(32, 10)
        self.assertGreater(ring.alpha(), 200)
        self.assertGreater(ring.red(), 200)
        self.assertLess(ring.green(), 40)

    def test_does_not_modify_source_file(self):
        before = self.src.read_bytes()
        dest = self.root / "out.png"
        apply_mask_to_file(self.src, dest, 64, MaskSettings(enabled=True, shape="circle"))
        self.assertEqual(self.src.read_bytes(), before)
        self.assertTrue(dest.is_file())
        self.assertFalse(dest.is_symlink())
        self.assertNotEqual(dest.read_bytes(), before)

    def test_refuses_to_overwrite_source_path(self):
        with self.assertRaises(MaskError):
            apply_mask_to_file(self.src, self.src, 64, MaskSettings(enabled=True, shape="circle"))
        # still intact
        self.assertGreater(self.src.stat().st_size, 0)

    def test_custom_png_mask_and_bad_files(self):
        mask_png = _solid_png(self.root / "mask.png", 64, 255, 255, 255, 255)
        # turn it into a circle mask by applying circle to a white square... use generated mask file
        apply_mask_to_file(
            _solid_png(self.root / "white.png", 64),
            mask_png,
            64,
            MaskSettings(enabled=True, shape="circle", padding=0, background="#00000000"),
        )
        img = apply_mask_to_image(
            self.src,
            64,
            MaskSettings(enabled=True, shape="custom", custom_path=str(mask_png), padding=0),
        )
        self.assertEqual(_alpha(img, 32, 32), 255)
        self.assertEqual(_alpha(img, 0, 0), 0)

        missing = MaskSettings(enabled=True, shape="custom", custom_path=str(self.root / "nope.png"))
        with self.assertRaises(MaskError):
            validate_settings(missing)
        empty = self.root / "empty.png"
        empty.write_bytes(b"")
        with self.assertRaises(MaskError):
            validate_settings(MaskSettings(enabled=True, shape="custom", custom_path=str(empty)))
        junk = self.root / "junk.png"
        junk.write_text("not an image", encoding="utf-8")
        with self.assertRaises(MaskError):
            validate_settings(MaskSettings(enabled=True, shape="custom", custom_path=str(junk)))
        bad_svg = self.root / "bad.svg"
        bad_svg.write_text("<not-svg", encoding="utf-8")
        with self.assertRaises(MaskError):
            validate_settings(MaskSettings(enabled=True, shape="custom", custom_path=str(bad_svg)))

    def test_settings_persist(self):
        state = self.root / "state"
        state.mkdir()
        settings = MaskSettings(enabled=True, shape="squircle", padding=0.1, background="#ff112233")
        save_settings(state, settings)
        loaded = load_settings(state)
        self.assertTrue(loaded.enabled)
        self.assertEqual(loaded.shape, "squircle")
        self.assertAlmostEqual(loaded.padding, 0.1)
        self.assertEqual(loaded.background, "#ff112233")


class MaskExportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = ensure_qt_app()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.icons = self.root / "icons"
        self.appsdir = self.root / "apps"
        self.icons.mkdir()
        self.appsdir.mkdir()
        make_theme(self.icons, "Tela", "hicolor", {"wechat": ["48x48/apps"]})
        # overwrite 1x1 fixture with a solid 48px icon so geometry is testable
        _solid_png(self.icons / "Tela" / "48x48" / "apps" / "wechat.png", 48)
        write_bytes(self.icons / "Tela" / "48x48" / "actions" / "edit-copy.png", PNG_1x1)
        make_theme(self.icons, "Papirus", "breeze,hicolor", {})
        make_theme(self.icons, "breeze", "hicolor", {})
        make_theme(self.icons, "hicolor", "", {})
        make_desktop(self.appsdir, "wechat.desktop", name="WeChat", icon="wechat")
        self.themes = enumerate_themes([self.icons])
        self.status = resolve_strategy(self.themes, current_theme="Papirus")
        self.apps = scan_applications([self.appsdir], current_desktop="KDE")
        self.src = self.icons / "Tela" / "48x48" / "apps" / "wechat.png"
        self.src_bytes = self.src.read_bytes()

    def tearDown(self):
        self.tmp.cleanup()

    def test_export_masks_app_copies_not_source_or_actions(self):
        dest = self.root / "out" / "icon-adapt"
        dest.parent.mkdir()
        mask = MaskSettings(enabled=True, shape="circle", padding=0, background="#00000000")
        prov = export_theme(
            dest,
            apps=self.apps,
            themes=self.themes,
            status=self.status,
            selections={},
            overwrite_owned=True,
            mask=mask,
        )
        self.assertTrue(is_tool_owned(dest))
        self.assertTrue(prov["mask"]["rasterized"])
        self.assertIn("栅格化", prov["mask"]["note"])
        self.assertEqual(self.src.read_bytes(), self.src_bytes)
        out = dest / "48x48" / "apps" / "wechat.png"
        self.assertTrue(out.is_file())
        self.assertFalse(out.is_symlink())
        self.assertNotEqual(out.read_bytes(), self.src_bytes)
        self.assertFalse((dest / "48x48" / "actions").exists())
        # Common sizes also written from the raster source
        self.assertTrue((dest / "32x32" / "apps" / "wechat.png").is_file())
        from PyQt6.QtGui import QImage

        img = QImage(str(out))
        self.assertFalse(img.isNull())
        self.assertEqual(img.pixelColor(0, 0).alpha(), 0)

    def test_bad_mask_does_not_write_or_clobber_theme(self):
        dest = self.root / "out" / "icon-adapt"
        # First: dest does not exist
        bad = MaskSettings(enabled=True, shape="custom", custom_path=str(self.root / "missing-mask.png"))
        with self.assertRaises(ExportError):
            export_theme(dest, apps=self.apps, themes=self.themes, status=self.status, selections={}, mask=bad)
        self.assertFalse(dest.exists())
        # Pre-existing owned theme must stay untouched when validation fails first
        dest.mkdir(parents=True)
        write_owned_marker(dest)
        sentinel = dest / "keep-me.txt"
        write(sentinel, "safe")
        with self.assertRaises(ExportError):
            export_theme(dest, apps=self.apps, themes=self.themes, status=self.status, selections={}, mask=bad)
        self.assertTrue(sentinel.is_file())
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "safe")

    def test_disabled_mask_still_symlinks_or_copies_original(self):
        dest = self.root / "out" / "icon-adapt"
        dest.parent.mkdir()
        export_theme(
            dest,
            apps=self.apps,
            themes=self.themes,
            status=self.status,
            selections={},
            mask=MaskSettings(enabled=False),
        )
        out = dest / "48x48" / "apps" / "wechat.png"
        self.assertTrue(out.exists())
        # Default path uses symlink when possible
        if out.is_symlink():
            self.assertEqual(out.resolve(), self.src.resolve())
