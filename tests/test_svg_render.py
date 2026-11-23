"""SVG preview diagnostics: missing refs, filter buffers, log dedup, export honesty."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path

from iconadapt.constants import MODULE_APPS, MODULE_SOURCE_SYSTEM, THEME_INTERNAL_NAME
from iconadapt.export import export_theme
from iconadapt.preview import decorate_previews, focus_caption, render_preview
from iconadapt.svgutil import (
    KIND_EMPTY_FILTER,
    KIND_FILTER_BUFFER,
    KIND_UNRESOLVED,
    inspect_svg_text,
    reset_svg_log_dedup,
    svg_log_count,
    svg_log_stats,
)
from iconadapt.strategy import resolve_strategy
from iconadapt.theme import enumerate_themes
from tests.fixtures import make_desktop, make_theme, write
from tests.qtutil import ensure_qt_app

MCMOJAVE_TERMINAL = (
    Path.home() / ".local/share/icons/McMojave-circle/apps/scalable/terminal.svg"
)
MCMOJAVE_AMAZON = (
    Path.home() / ".local/share/icons/McMojave-circle/apps/scalable/amazon.svg"
)

NORMAL_SVG = """<svg xmlns="http://www.w3.org/2000/svg" width="32" height="32" viewBox="0 0 32 32">
  <rect width="32" height="32" fill="#2ecc71"/>
</svg>
"""

LEGAL_REFS_SVG = """<svg xmlns="http://www.w3.org/2000/svg" width="32" height="32" viewBox="0 0 32 32">
  <defs>
    <linearGradient id="g" x1="0" x2="1" y1="0" y2="1">
      <stop offset="0" stop-color="#ff0000"/>
      <stop offset="1" stop-color="#0000ff"/>
    </linearGradient>
    <clipPath id="c"><circle cx="16" cy="16" r="12"/></clipPath>
    <rect id="r" x="2" y="2" width="10" height="10" fill="#111"/>
  </defs>
  <circle cx="16" cy="16" r="14" fill="url(#g)"/>
  <use href="#r"/>
</svg>
"""

BROKEN_REF_SVG = """<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64">
  <defs>
    <linearGradient id="c" x1="0" x2="0" y1="0" y2="1">
      <stop offset="0" stop-color="#767574"/>
      <stop offset="1" stop-color="#d1e7ee"/>
    </linearGradient>
  </defs>
  <circle cx="32" cy="32" r="30" fill="url(#c)"/>
  <circle cx="32" cy="32" r="30" fill="url(#a)"/>
  <circle cx="32" cy="32" r="20" fill="#1a1a1a"/>
</svg>
"""

EMPTY_FILTER_SVG = """<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64">
  <defs>
    <filter id="c" width="1.118" height="1.122" x="-.059" y="-.061">
      <feGaussianBlur stdDeviation="10.562"/>
    </filter>
  </defs>
  <rect width="40" height="40" x="12" y="12" fill="#f90"/>
  <g filter="url(#c)" transform="translate(12.684 12.68)scale(.08965)"/>
</svg>
"""

HUGE_FILTER_SVG = """<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64" viewBox="0 0 64 64">
  <defs>
    <filter id="c" filterUnits="userSpaceOnUse" x="-100000" y="-100000" width="200000" height="200000">
      <feGaussianBlur stdDeviation="1"/>
    </filter>
  </defs>
  <rect width="40" height="40" x="12" y="12" fill="#0a7"/>
  <rect width="40" height="40" x="12" y="12" fill="#000" filter="url(#c)" opacity=".2"/>
</svg>
"""


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_svg_theme(root: Path, name: str, icons: dict[str, str]) -> Path:
    make_theme(root, name, "hicolor", {stem: ["scalable/apps"] for stem in icons})
    for stem, body in icons.items():
        path = root / name / "scalable" / "apps" / f"{stem}.png"
        if path.exists():
            path.unlink()
        write(root / name / "scalable" / "apps" / f"{stem}.svg", body)
    return root / name


class SvgInspectAndRenderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._app = ensure_qt_app()

    def setUp(self):
        reset_svg_log_dedup()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _svg(self, name: str, body: str) -> Path:
        path = self.root / name
        path.write_text(body, encoding="utf-8")
        return path

    def test_normal_svg_is_ok_and_visible(self):
        path = self._svg("ok.svg", NORMAL_SVG)
        before = _sha256(path)
        result = render_preview(path, 48)
        self.assertEqual(result.status, "ok", result)
        self.assertTrue(result.data_url.startswith("data:image/png"))
        self.assertGreater(result.visible_pixels, 8)
        self.assertFalse(result.issues)
        self.assertEqual(_sha256(path), before)

    def test_legal_gradient_and_use_are_not_rejected(self):
        issues = inspect_svg_text(LEGAL_REFS_SVG)
        self.assertFalse([i for i in issues if i.kind == KIND_UNRESOLVED], issues)
        path = self._svg("legal.svg", LEGAL_REFS_SVG)
        result = render_preview(path, 48)
        self.assertIn(result.status, {"ok", "low_contrast"})
        self.assertTrue(result.data_url)
        self.assertFalse(any(i.kind == KIND_UNRESOLVED for i in result.issues), result)

    def test_broken_reference_is_incomplete_and_visible(self):
        path = self._svg("broken.svg", BROKEN_REF_SVG)
        before = _sha256(path)
        result = render_preview(path, 48)
        self.assertEqual(result.status, "incomplete", result)
        self.assertTrue(result.data_url, "partial paint should still be shown")
        self.assertGreater(result.visible_pixels, 8)
        self.assertTrue(any(i.kind == KIND_UNRESOLVED and i.detail == "a" for i in result.issues), result.issues)
        self.assertIn("#a", result.note)
        self.assertIn("不完整", focus_caption({"name": "broken", "render_status": result.status, "render_note": result.note}))
        self.assertEqual(path.read_text(encoding="utf-8"), BROKEN_REF_SVG)
        self.assertEqual(_sha256(path), before)

    def test_empty_filter_group_reproduces_buffer_warning_and_is_labeled(self):
        path = self._svg("empty-filter.svg", EMPTY_FILTER_SVG)
        before = _sha256(path)
        result = render_preview(path, 48)
        self.assertEqual(_sha256(path), before)
        self.assertEqual(path.read_text(encoding="utf-8"), EMPTY_FILTER_SVG)
        self.assertEqual(result.status, "incomplete", result)
        self.assertTrue(result.data_url)
        self.assertGreater(result.visible_pixels, 8)
        kinds = {i.kind for i in result.issues}
        self.assertTrue(kinds & {KIND_FILTER_BUFFER, KIND_EMPTY_FILTER}, result.issues)
        self.assertTrue(result.preview_simplified or KIND_EMPTY_FILTER in kinds)
        self.assertIn("滤镜", result.note)

        # Contrast: the same drawing without the empty group is clean.
        clean = self._svg("clean-filter.svg", EMPTY_FILTER_SVG.replace('<g filter="url(#c)" transform="translate(12.684 12.68)scale(.08965)"/>', ""))
        reset_svg_log_dedup()
        ok = render_preview(clean, 48)
        self.assertEqual(ok.status, "ok", ok)
        self.assertFalse(any(i.kind in {KIND_FILTER_BUFFER, KIND_EMPTY_FILTER} for i in ok.issues), ok)

    def test_huge_filter_region_is_bounded_and_not_silent_ok(self):
        path = self._svg("huge-filter.svg", HUGE_FILTER_SVG)
        result = render_preview(path, 48)
        self.assertNotEqual(result.status, "ok", result)
        # Process is still alive if we got here; image may be recovered after preview clamp.
        if result.data_url:
            self.assertIn(result.status, {"incomplete", "low_contrast", "error"})
        self.assertTrue(result.issues or result.status == "error")

    def test_repeated_preview_does_not_flood_same_warning(self):
        path = self._svg("broken.svg", BROKEN_REF_SVG)
        reset_svg_log_dedup()
        first = render_preview(path, 48)
        second = render_preview(path, 48)
        third = render_preview(path, 32)  # different size, still same source+problem
        self.assertEqual(first.status, "incomplete")
        self.assertEqual(second.status, "incomplete")
        self.assertEqual(third.status, "incomplete")
        seen, emitted = svg_log_count(str(path), "unresolved")
        self.assertGreaterEqual(seen, 2)
        self.assertEqual(emitted, 1, svg_log_stats())
        # Other categories still have a path: a decode error is not swallowed by this key.
        self.assertEqual(svg_log_count(str(path), "filter_buffer")[0], 0)

    @unittest.skipUnless(MCMOJAVE_TERMINAL.is_file(), "McMojave-circle terminal.svg not installed")
    def test_real_mcmojave_terminal_missing_ref_source_hash_unchanged(self):
        before = _sha256(MCMOJAVE_TERMINAL)
        result = render_preview(MCMOJAVE_TERMINAL, 48)
        after = _sha256(MCMOJAVE_TERMINAL)
        self.assertEqual(before, after)
        self.assertEqual(before, "312a844ae10a694b7fe0bb1cb0196c617a139ca57d5533156a56d375a4b2de1a")
        self.assertEqual(result.status, "incomplete", result)
        self.assertTrue(result.data_url)
        self.assertGreater(result.visible_pixels, 8)
        self.assertTrue(any(i.kind == KIND_UNRESOLVED and i.detail == "a" for i in result.issues), result.issues)
        self.assertIn("#a", result.note)

    @unittest.skipUnless(MCMOJAVE_AMAZON.is_file(), "McMojave-circle amazon.svg not installed")
    def test_real_mcmojave_amazon_empty_filter_source_unchanged(self):
        before = _sha256(MCMOJAVE_AMAZON)
        result = render_preview(MCMOJAVE_AMAZON, 48)
        self.assertEqual(_sha256(MCMOJAVE_AMAZON), before)
        self.assertEqual(result.status, "incomplete", result)
        self.assertTrue(result.data_url)
        self.assertGreater(result.visible_pixels, 8)
        kinds = {i.kind for i in result.issues}
        self.assertTrue(kinds & {KIND_FILTER_BUFFER, KIND_EMPTY_FILTER}, result.issues)


class SvgExportAndGuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._app = ensure_qt_app()

    def setUp(self):
        reset_svg_log_dedup()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.icons = self.root / "icons"
        self.apps_dir = self.root / "apps"
        self.apps_dir.mkdir()
        _write_svg_theme(
            self.icons,
            "Pack",
            {
                "good-app": NORMAL_SVG,
                "broken-app": BROKEN_REF_SVG,
                "filter-app": EMPTY_FILTER_SVG,
            },
        )
        make_theme(self.icons, "hicolor", "", {"folder": ["48x48/places"]})
        make_desktop(self.apps_dir, "broken-app.desktop", name="Broken", icon="broken-app")
        self.themes = enumerate_themes([self.icons])
        self.status = resolve_strategy(self.themes, current_theme="Pack")
        from iconadapt.desktop import scan_applications

        self.apps = scan_applications([self.apps_dir], current_desktop="KDE")

    def tearDown(self):
        self.tmp.cleanup()

    def test_export_copies_broken_source_verbatim_and_reports(self):
        src = self.icons / "Pack" / "scalable" / "apps" / "broken-app.svg"
        before = _sha256(src)
        dest = self.root / "out" / THEME_INTERNAL_NAME
        prov = export_theme(
            dest,
            apps=self.apps,
            themes=self.themes,
            status=self.status,
            selections={},
            overwrite_owned=True,
            modules={MODULE_APPS: "Pack"},
        )
        self.assertEqual(_sha256(src), before, "export must not rewrite the original pack")
        copied = dest / "scalable" / "apps" / "broken-app.svg"
        self.assertTrue(copied.exists() or copied.is_symlink(), copied)
        self.assertEqual(_sha256(copied.resolve()), before)
        issues = prov.get("source_issues") or []
        names = {row["name"] for row in issues}
        self.assertIn("broken-app.svg", names)
        self.assertIn("filter-app.svg", names)
        self.assertNotIn("good-app.svg", names)
        self.assertTrue(prov.get("source_issue_summary"))
        self.assertIn("按原样复制", prov["source_issue_summary"])
        self.assertIn("没有改你的选择", prov["source_issue_summary"])

    def test_offscreen_gui_module_switch_and_candidate_preview(self):
        from PyQt6.QtCore import QUrl
        from PyQt6.QtQml import QQmlApplicationEngine

        from iconadapt.backend import IconAdaptBackend
        from iconadapt.gui import qml_dir

        iso = {
            "data_home": str(self.root / "data"),
            "config_home": str(self.root / "config"),
            "icon_dirs": [str(self.icons)],
            "app_dirs": [str(self.apps_dir)],
            "current_theme": "Pack",
            "current_desktop": "KDE",
        }
        be = IconAdaptBackend(isolated=iso)
        reset_svg_log_dedup()
        be.setModuleSource(MODULE_APPS, "Pack")
        payload = json.loads(be.modulePreviewJson("apps", "Pack", 8))
        items = payload.get("previews") or []
        self.assertTrue(items, payload)
        statuses = {item.get("name"): item.get("render_status") for item in items}
        notes = {item.get("name"): item.get("render_note") for item in items}
        self.assertEqual(statuses.get("good-app"), "ok", statuses)
        self.assertEqual(statuses.get("broken-app"), "incomplete", (statuses, notes))
        self.assertEqual(statuses.get("filter-app"), "incomplete", (statuses, notes))
        broken = next(item for item in items if item["name"] == "broken-app")
        self.assertTrue(broken.get("preview_url", "").startswith("data:image/png"))
        self.assertIn("#a", broken.get("render_note") or "")

        # Switch away and back; second pass must not flood the same warning.
        be.setModuleSource(MODULE_APPS, MODULE_SOURCE_SYSTEM)
        be.setModuleSource(MODULE_APPS, "Pack")
        json.loads(be.modulePreviewJson("apps", "Pack", 8))
        broken_path = str(self.icons / "Pack" / "scalable" / "apps" / "broken-app.svg")
        seen, emitted = svg_log_count(broken_path, "unresolved")
        self.assertGreaterEqual(seen, 1)
        self.assertEqual(emitted, 1, (seen, emitted, svg_log_stats()))

        # Candidates in app detail also go through bounded previews.
        detail = json.loads(be.appDetailJson("broken-app"))
        cands = detail.get("candidates") or []
        self.assertTrue(cands)
        self.assertTrue(any(c.get("render_status") == "incomplete" for c in cands), cands)
        self.assertTrue(any((c.get("preview_url") or "").startswith("data:image/png") for c in cands))
        url1 = be.iconPreviewUrl(broken_path)
        url2 = be.iconPreviewUrl(broken_path)
        self.assertEqual(url1, url2)
        self.assertTrue(url1.startswith("data:image/png"))
        seen2, emitted2 = svg_log_count(broken_path, "unresolved")
        self.assertEqual(emitted2, 1, (seen2, emitted2))

        engine = QQmlApplicationEngine()
        engine.rootContext().setContextProperty("iconAdapt", be)
        engine.load(QUrl.fromLocalFile(str(qml_dir() / "Main.qml")))
        self.assertTrue(engine.rootObjects(), "QML failed to load during SVG offscreen switch test")
        del engine


if __name__ == "__main__":
    unittest.main()
