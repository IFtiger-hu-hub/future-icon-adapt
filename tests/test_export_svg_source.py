"""Prefer scalable SVG over a smaller Fixed copy when exporting masked apps."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from PyQt6.QtGui import QImage

from iconadapt.constants import MODULE_APPS, THEME_INTERNAL_NAME, TOOL_VERSION
from iconadapt.desktop import scan_applications
from iconadapt.export import export_theme
from iconadapt.mask import MaskSettings
from iconadapt.modules import files_by_name, pick_export_svg_source
from iconadapt.strategy import resolve_strategy
from iconadapt.theme import enumerate_themes
from tests.fixtures import make_desktop, make_theme, write
from tests.qtutil import ensure_qt_app

RED_SVG = """<svg xmlns="http://www.w3.org/2000/svg" width="22" height="22" viewBox="0 0 22 22">
  <rect width="22" height="22" fill="#ff0000"/>
</svg>
"""
BLUE_SVG = """<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64" viewBox="0 0 64 64">
  <rect width="64" height="64" fill="#0000ff"/>
</svg>
"""
GREEN_SVG = """<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 16 16">
  <rect width="16" height="16" fill="#00aa00"/>
</svg>
"""


def _center_rgba(path: Path) -> tuple[int, int, int, int]:
    img = QImage(str(path))
    if img.isNull():
        raise AssertionError(f"unreadable image: {path}")
    c = img.pixelColor(img.width() // 2, img.height() // 2)
    return c.red(), c.green(), c.blue(), c.alpha()


def _is_blue(rgb: tuple[int, int, int, int]) -> bool:
    r, g, b, a = rgb
    return a > 200 and b > 200 and r < 40 and g < 40


def _is_red(rgb: tuple[int, int, int, int]) -> bool:
    r, g, b, a = rgb
    return a > 200 and r > 200 and g < 40 and b < 40


def _is_green(rgb: tuple[int, int, int, int]) -> bool:
    r, g, b, a = rgb
    return a > 200 and g > 150 and r < 40 and b < 40


def _write_mcmojave_like_pack(root: Path, name: str = "Pack") -> Path:
    """apps/22 listed first, like McMojave-circle, with a distinct scalable original."""
    tdir = root / name
    index = "\n".join(
        [
            "[Icon Theme]",
            f"Name={name}",
            "Comment=test",
            "Inherits=hicolor",
            "Directories=apps/22,apps/32,apps/scalable,apps/symbolic",
            "",
            "[apps/22]",
            "Size=22",
            "Context=Applications",
            "Type=Fixed",
            "",
            "[apps/32]",
            "Size=32",
            "Context=Applications",
            "Type=Fixed",
            "",
            "[apps/scalable]",
            "Size=64",
            "Context=Applications",
            "Type=Scalable",
            "MinSize=16",
            "MaxSize=512",
            "",
            "[apps/symbolic]",
            "Size=16",
            "Context=Applications",
            "Type=Scalable",
            "MinSize=16",
            "MaxSize=512",
            "",
            "",
        ]
    )
    write(tdir / "index.theme", index)
    (tdir / "apps/22").mkdir(parents=True, exist_ok=True)
    (tdir / "apps/scalable").mkdir(parents=True, exist_ok=True)
    (tdir / "apps/symbolic").mkdir(parents=True, exist_ok=True)
    (tdir / "apps/22/microsoft-edge.svg").write_text(RED_SVG, encoding="utf-8")
    (tdir / "apps/22/kget.svg").write_text(RED_SVG, encoding="utf-8")
    os.symlink("microsoft-edge.svg", tdir / "apps/22/microsoft-edge-symbolic.svg")
    os.symlink("kget.svg", tdir / "apps/22/kget-symbolic.svg")
    (tdir / "apps/scalable/microsoft-edge.svg").write_text(BLUE_SVG, encoding="utf-8")
    (tdir / "apps/scalable/kget.svg").write_text(BLUE_SVG, encoding="utf-8")
    os.symlink("microsoft-edge.svg", tdir / "apps/scalable/com.microsoft.Edge.svg")
    (tdir / "apps/symbolic/microsoft-edge-symbolic.svg").write_text(GREEN_SVG, encoding="utf-8")
    (tdir / "apps/symbolic/kget-symbolic.svg").write_text(GREEN_SVG, encoding="utf-8")
    return tdir


class PickExportSvgSourceTests(unittest.TestCase):
    def test_prefers_scalable_over_earlier_fixed_22(self):
        small = Path("/tmp/apps/22/icon.svg")
        large = Path("/tmp/apps/scalable/icon.svg")
        files = [
            SimpleNamespace(path=small, typ="Fixed", rel_dir="apps/22", size=22),
            SimpleNamespace(path=large, typ="Scalable", rel_dir="apps/scalable", size=64),
        ]
        # Inventory order is 22 first, as McMojave lists Directories.
        chosen = pick_export_svg_source([small, large], files)
        self.assertEqual(chosen, large)

    def test_prefers_symbolic_scalable_over_22px_symlink_source(self):
        small = Path("/tmp/apps/22/icon-symbolic.svg")
        symbolic = Path("/tmp/apps/symbolic/icon-symbolic.svg")
        files = [
            SimpleNamespace(path=small, typ="Fixed", rel_dir="apps/22", size=22),
            SimpleNamespace(path=symbolic, typ="Scalable", rel_dir="apps/symbolic", size=16),
        ]
        chosen = pick_export_svg_source([small, symbolic], files)
        self.assertEqual(chosen, symbolic)

    def test_falls_back_to_larger_fixed_when_no_scalable(self):
        s22 = Path("/tmp/apps/22/icon.svg")
        s32 = Path("/tmp/apps/32/icon.svg")
        files = [
            SimpleNamespace(path=s22, typ="Fixed", rel_dir="apps/22", size=22),
            SimpleNamespace(path=s32, typ="Fixed", rel_dir="apps/32", size=32),
        ]
        chosen = pick_export_svg_source([s22, s32], files)
        self.assertEqual(chosen, s32)

    def test_empty(self):
        self.assertIsNone(pick_export_svg_source([], []))


class MaskedExportPrefersScalableTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._app = ensure_qt_app()

    def test_microsoft_edge_and_kget_use_scalable_not_apps22(self):
        self.assertTrue(TOOL_VERSION.startswith("0.2."), TOOL_VERSION)
        with tempfile.TemporaryDirectory(prefix="iconadapt-test-") as tmp:
            root = Path(tmp)
            icons = root / "icons"
            apps_dir = root / "apps"
            apps_dir.mkdir()
            pack = _write_mcmojave_like_pack(icons, "Pack")
            before_blue = (pack / "apps/scalable/microsoft-edge.svg").read_bytes()
            before_red = (pack / "apps/22/microsoft-edge.svg").read_bytes()
            make_theme(icons, "hicolor", "", {"folder": ["48x48/places"]})
            make_desktop(apps_dir, "microsoft-edge.desktop", name="Edge", icon="microsoft-edge")
            make_desktop(apps_dir, "kget.desktop", name="KGet", icon="kget")
            themes = enumerate_themes([icons])
            # Inventory still lists the 22px file first — that is the bug trigger.
            grouped = files_by_name(themes["Pack"], MODULE_APPS)
            edge_rels = [item.rel_dir for item in grouped["microsoft-edge"]]
            self.assertEqual(edge_rels[0], "apps/22")
            self.assertIn("apps/scalable", edge_rels)

            dest = root / "out" / THEME_INTERNAL_NAME
            settings = MaskSettings(enabled=True, shape="circle", background="#ffffffff", padding=0)
            export_theme(
                dest,
                apps=scan_applications([apps_dir], current_desktop="KDE"),
                themes=themes,
                status=resolve_strategy(themes, current_theme="Pack"),
                selections={},
                overwrite_owned=True,
                mask=settings,
                modules={MODULE_APPS: "Pack"},
            )
            self.assertEqual((pack / "apps/scalable/microsoft-edge.svg").read_bytes(), before_blue)
            self.assertEqual((pack / "apps/22/microsoft-edge.svg").read_bytes(), before_red)

            for name in ("microsoft-edge", "kget"):
                pictorial = dest / "48x48/apps" / f"{name}.png"
                symbolic = dest / "48x48/apps" / f"{name}-symbolic.png"
                self.assertTrue(pictorial.is_file(), name)
                self.assertTrue(symbolic.is_file(), name)
                pic = _center_rgba(pictorial)
                sym = _center_rgba(symbolic)
                self.assertTrue(_is_blue(pic), f"{name} should come from scalable, got {pic}")
                self.assertFalse(_is_red(pic), name)
                self.assertTrue(_is_green(sym), f"{name}-symbolic should come from apps/symbolic, got {sym}")
                self.assertFalse(_is_red(sym), name)
                self.assertNotEqual(pictorial.read_bytes(), symbolic.read_bytes())

            alias = dest / "48x48/apps/com.microsoft.Edge.png"
            self.assertTrue(alias.is_file())
            self.assertTrue(_is_blue(_center_rgba(alias)))


if __name__ == "__main__":
    unittest.main()
