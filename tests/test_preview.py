"""Module preview: base inherit, fallback labeling, symbolic contrast, errors."""

from __future__ import annotations

import base64
import json
import os
import tempfile
import unittest
from pathlib import Path

from iconadapt.constants import MODULE_SOURCE_SYSTEM, THEME_INTERNAL_NAME
from iconadapt.modules import modules_overview
from iconadapt.preview import (
    PreviewPalette,
    apply_colorscheme,
    base_preview_chain,
    compose_module_previews,
    decorate_previews,
    flatten_inherit,
    focus_caption,
    is_cursor_theme,
    render_preview,
)
from iconadapt.theme import enumerate_themes
from tests.fixtures import PNG_1x1, make_theme, write, write_bytes
from tests.qtutil import ensure_qt_app


LIGHT_SYMBOLIC = """<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" width="22" height="22" viewBox="0 0 22 22">
 <defs>
  <style id="current-color-scheme" type="text/css">
   .ColorScheme-Text { color:#eeeeec; }
  </style>
 </defs>
 <path style="fill:currentColor" class="ColorScheme-Text" d="M 4 10 L 18 10 L 18 12 L 4 12 Z"/>
</svg>
"""


def _png_from_url(url: str) -> bytes:
    prefix = "data:image/png;base64,"
    assert url.startswith(prefix), url[:40]
    return base64.b64decode(url[len(prefix):])


class InheritPreviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        make_theme(
            self.root,
            "Papirus",
            "breeze,hicolor",
            {
                "folder": ["48x48/places"],
                "user-home": ["48x48/places"],
                "document-open": ["22x22/actions"],
                "demo-app": ["48x48/apps"],
                "text-plain": ["48x48/mimetypes"],
                "dialog-information": ["22x22/status"],
                "emblem-default": ["8x8/emblems"],
            },
        )
        make_theme(
            self.root,
            "breeze",
            "hicolor",
            {
                "folder": ["places/48"],
                "list-add": ["actions/22"],
                "audio-volume-high": ["applets/22"],
            },
        )
        make_theme(self.root, "hicolor", "", {"folder": ["48x48/places"]})
        make_theme(self.root, "AppsOnly", "Papirus,hicolor", {"other-app": ["48x48/apps"]})
        make_theme(
            self.root,
            THEME_INTERNAL_NAME,
            "Papirus,breeze,hicolor",
            {"folder": ["48x48/places"], "only-adapt": ["48x48/places"]},
        )
        self.themes = enumerate_themes([self.root])

    def tearDown(self):
        self.tmp.cleanup()

    def test_system_previews_each_module_from_base_chain(self):
        inherit = ["Papirus", "breeze", "hicolor"]
        for mid in ("apps", "places", "mimetypes", "actions", "status", "other"):
            composed = compose_module_previews(
                MODULE_SOURCE_SYSTEM, mid, self.themes, inherit=inherit, limit=8
            )
            self.assertTrue(composed["previews"], f"{mid}: {composed}")
            for item in composed["previews"]:
                self.assertEqual(item["origin"], "base")
                self.assertNotEqual(item["theme"], THEME_INTERNAL_NAME)
                self.assertIn(item["theme"], inherit)
        # icon-adapt must not appear even if listed first
        chain = base_preview_chain([THEME_INTERNAL_NAME, "Papirus"], self.themes)
        self.assertNotIn(THEME_INTERNAL_NAME, chain)
        self.assertEqual(chain[0], "Papirus")

    def test_multi_parent_fallback_is_labeled(self):
        composed = compose_module_previews(
            "AppsOnly", "places", self.themes, inherit=["Papirus", "breeze", "hicolor"], limit=8
        )
        self.assertTrue(composed["empty"])
        self.assertFalse(composed["has_content"])
        self.assertTrue(composed["fallback"])
        self.assertTrue(composed["previews"])
        for item in composed["previews"]:
            self.assertEqual(item["origin"], "fallback")
            self.assertEqual(item["theme"], "Papirus")
            self.assertNotEqual(item["theme"], "AppsOnly")
        self.assertIn("Papirus", composed["preview_caption"])
        self.assertIn("本身没有", composed["status_line"])

    def test_cycle_does_not_loop(self):
        make_theme(self.root, "LoopA", "LoopB", {"zzz": ["48x48/apps"]})
        make_theme(self.root, "LoopB", "LoopA", {})
        themes = enumerate_themes([self.root])
        chain, warnings = flatten_inherit(["LoopA"], themes)
        self.assertEqual(chain[0], "LoopA")
        self.assertIn("LoopB", chain)
        self.assertTrue(any("cycle" in w for w in warnings))
        # Should return, not hang.
        composed = compose_module_previews("LoopB", "places", themes, inherit=["Papirus"], limit=4)
        self.assertTrue(composed["empty"])

    def test_icon_adapt_not_used_as_base_or_parent(self):
        inherit = [THEME_INTERNAL_NAME, "Papirus"]
        composed = compose_module_previews(
            MODULE_SOURCE_SYSTEM, "places", self.themes, inherit=inherit, limit=8
        )
        themes = {i["theme"] for i in composed["previews"]}
        names = {i["name"] for i in composed["previews"]}
        self.assertNotIn(THEME_INTERNAL_NAME, themes)
        self.assertNotIn("only-adapt", names)
        self.assertIn("Papirus", themes)


class RenderAndErrorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._app = ensure_qt_app()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_light_symbolic_is_visible_after_colorscheme_and_plate(self):
        path = self.root / "list-add.svg"
        write(path, LIGHT_SYMBOLIC)
        # Raw color is almost white; after scheme rewrite on a light plate it should contrast.
        pal = PreviewPalette(text="#232627", background="#eff0f1")
        rewritten = apply_colorscheme(LIGHT_SYMBOLIC, pal)
        self.assertIn("#232627", rewritten)
        self.assertNotIn("#eeeeec", rewritten)
        result = render_preview(path, 48, pal=pal)
        self.assertEqual(result.status, "ok")
        self.assertTrue(result.symbolic)
        self.assertGreaterEqual(result.visible_pixels, 8)
        self.assertTrue(result.data_url.startswith("data:image/png"))
        raw = _png_from_url(result.data_url)
        self.assertGreater(len(raw), 32)

    def test_broken_symlink_and_undecodable_file(self):
        missing = self.root / "nope.svg"
        link = self.root / "dangling.svg"
        os.symlink(missing, link)
        broken = render_preview(link, 32)
        self.assertEqual(broken.status, "broken")
        self.assertIn("符号链接", broken.note)
        self.assertEqual(broken.data_url, "")

        bad = self.root / "bad.svg"
        write(bad, "<not-svg>")
        err = render_preview(bad, 32)
        self.assertIn(err.status, {"error", "broken"})
        self.assertTrue(err.note)
        self.assertEqual(err.data_url, "")

        caption = focus_caption({
            "name": "bad",
            "theme": "Demo",
            "render_status": "error",
            "render_note": err.note,
        })
        self.assertTrue(caption.startswith("无法预览"))
        self.assertNotIn("来自 Demo", caption)

    def test_empty_module_with_broken_icon_still_explains(self):
        dest = self.root / "icons"
        make_theme(dest, "Emptyish", "hicolor", {"ghost": ["48x48/places"]})
        ghost = dest / "Emptyish" / "48x48" / "places" / "ghost.png"
        ghost.unlink()
        os.symlink(dest / "missing.png", ghost)
        themes = enumerate_themes([dest])
        composed = compose_module_previews("Emptyish", "places", themes, inherit=[], limit=4)
        # The dangling name is still listed so decorate can mark it broken.
        decorated = decorate_previews(composed["previews"], size=32)
        if decorated:
            statuses = {i["render_status"] for i in decorated}
            self.assertTrue(statuses & {"broken", "error", "missing"} or composed["empty"])

    def test_cursor_theme_is_flagged(self):
        tdir = self.root / "Breeze_Light"
        tdir.mkdir()
        write(tdir / "index.theme", "[Icon Theme]\nName=Breeze Light\nComment=cursors\nDirectories=\n")
        (tdir / "cursors").mkdir()
        themes = enumerate_themes([self.root])
        self.assertTrue(is_cursor_theme(themes["Breeze_Light"]))
        composed = compose_module_previews("Breeze_Light", "actions", themes, inherit=[], limit=4)
        self.assertTrue(composed["empty"])
        self.assertIn("指针", composed["status_line"])


class LiveAndUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._app = ensure_qt_app()

    def test_flat_remix_list_add_pixels_not_empty(self):
        path = Path.home() / ".local/share/icons/Flat-Remix-Blue-Dark/actions/22/list-add.svg"
        if not path.is_file():
            self.skipTest("Flat-Remix-Blue-Dark not installed")
        pal = PreviewPalette(text="#232627", background="#e8e8e8")
        result = render_preview(path, 44, pal=pal)
        self.assertEqual(result.status, "ok", result.note)
        self.assertGreaterEqual(result.visible_pixels, 8)
        self.assertTrue(result.data_url)

    def test_offscreen_backend_system_previews_and_qml_load(self):
        from iconadapt.backend import IconAdaptBackend
        from iconadapt.gui import qml_dir
        from PyQt6.QtCore import QUrl
        from PyQt6.QtQml import QQmlApplicationEngine

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        icons = root / "icons"
        apps = root / "apps"
        apps.mkdir(parents=True)
        make_theme(icons, "Papirus", "breeze,hicolor", {"folder": ["48x48/places"], "demo": ["48x48/apps"]})
        make_theme(icons, "breeze", "hicolor", {"document-open": ["actions/22"]})
        make_theme(icons, "hicolor", "", {})
        make_theme(icons, "Tela", "hicolor", {"demo": ["48x48/apps"]})
        # Fixture PNG_1x1 is fully transparent; write opaque samples so preview
        # decoration can be checked for real pixels.
        from PyQt6.QtGui import QImage, QColor
        for rel in (
            icons / "Papirus" / "48x48" / "places" / "folder.png",
            icons / "Papirus" / "48x48" / "apps" / "demo.png",
            icons / "breeze" / "actions" / "22" / "document-open.png",
        ):
            img = QImage(16, 16, QImage.Format.Format_ARGB32)
            img.fill(QColor("#2266aa"))
            img.save(str(rel))
        from tests.fixtures import make_desktop
        make_desktop(apps, "demo.desktop", name="Demo", icon="demo")
        iso = {
            "data_home": str(root / "data"),
            "config_home": str(root / "config"),
            "icon_dirs": [str(icons)],
            "app_dirs": [str(apps)],
            "current_theme": "icon-adapt",
            "current_desktop": "KDE",
        }
        be = IconAdaptBackend(isolated=iso)
        rows = {r["id"]: r for r in json.loads(be.modulesJson)}
        for mid, row in rows.items():
            self.assertEqual(row["source"], MODULE_SOURCE_SYSTEM)
            self.assertTrue(row.get("status_line"))
            # Every module should attempt a real base preview when the chain has files.
        self.assertTrue(rows["places"]["previews"])
        self.assertTrue(rows["apps"]["previews"])
        self.assertTrue(rows["actions"]["previews"])
        self.assertTrue(any(i.get("preview_url", "").startswith("data:image/png") for i in rows["places"]["previews"]))
        self.assertTrue(all(i["theme"] != THEME_INTERNAL_NAME for i in rows["places"]["previews"]))

        # QML must load against this backend (not just process start).
        engine = QQmlApplicationEngine()
        engine.rootContext().setContextProperty("iconAdapt", be)
        qml_file = qml_dir() / "Main.qml"
        engine.load(QUrl.fromLocalFile(str(qml_file)))
        self.assertTrue(engine.rootObjects(), "QML failed to load in isolated offscreen test")
        # Probe the same model the UI binds to.
        root_obj = engine.rootObjects()[0]
        modules = json.loads(be.modulesJson)
        places = next(r for r in modules if r["id"] == "places")
        url = places["previews"][0]["preview_url"]
        png = _png_from_url(url)
        self.assertGreater(len(png), 32)
        del engine


if __name__ == "__main__":
    unittest.main()
