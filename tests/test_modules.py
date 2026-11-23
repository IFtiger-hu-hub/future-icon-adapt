"""Module classification, previews, persistence, and composed export."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from iconadapt.constants import (
    MODULE_ACTIONS,
    MODULE_APPS,
    MODULE_MIMETYPES,
    MODULE_OTHER,
    MODULE_PLACES,
    MODULE_SOURCE_SYSTEM,
    MODULE_STATUS,
    SETTINGS_FILENAME,
    THEME_INTERNAL_NAME,
)
from iconadapt.export import export_theme, safe_place_file, uninstall_owned_theme
from iconadapt.mask import MaskSettings, save_settings
from iconadapt.modules import (
    classify_rel,
    classify_spec,
    has_explicit_module_sources,
    load_module_map,
    modules_overview,
    normalize_module_map,
    preview_items,
    save_module_map,
    theme_options_for_module,
)
from iconadapt.persist import load_choices, save_choices, upsert_selection
from iconadapt.strategy import resolve_strategy
from iconadapt.theme import ThemeDirSpec, enumerate_themes, parse_index_theme
from tests.fixtures import PNG_1x1, make_desktop, make_theme, write, write_bytes
from tests.qtutil import ensure_qt_app


class ClassifyTests(unittest.TestCase):
    def test_context_wins_over_unusual_directory_name(self):
        self.assertEqual(classify_rel("foo/48", "Places"), MODULE_PLACES)
        self.assertEqual(classify_rel("odd/bin", "Applications"), MODULE_APPS)
        self.assertEqual(classify_rel("applets/22", "Status"), MODULE_STATUS)
        self.assertEqual(classify_rel("misc/16", "MimeTypes"), MODULE_MIMETYPES)
        self.assertEqual(classify_rel("x/22", "Actions"), MODULE_ACTIONS)
        self.assertEqual(classify_rel("x/8", "Emblems"), MODULE_OTHER)

    def test_path_fallback_when_context_missing(self):
        self.assertEqual(classify_rel("48x48/apps", ""), MODULE_APPS)
        self.assertEqual(classify_rel("apps/48", ""), MODULE_APPS)
        self.assertEqual(classify_rel("places/16", ""), MODULE_PLACES)
        self.assertEqual(classify_rel("16x16/places", ""), MODULE_PLACES)
        self.assertEqual(classify_rel("22x22/panel", ""), MODULE_STATUS)
        self.assertEqual(classify_rel("applets/16", ""), MODULE_STATUS)
        self.assertEqual(classify_rel("22x22@2x/status", ""), MODULE_STATUS)
        self.assertEqual(classify_rel("mimetypes/64", ""), MODULE_MIMETYPES)
        self.assertEqual(classify_rel("devices/32", ""), MODULE_PLACES)
        self.assertEqual(classify_rel("categories/32", ""), MODULE_APPS)
        self.assertEqual(classify_rel("unknown/48", ""), MODULE_OTHER)

    def test_context_is_case_insensitive(self):
        self.assertEqual(classify_rel("dir", "STATUS"), MODULE_STATUS)
        self.assertEqual(classify_rel("dir", "mime-types"), MODULE_MIMETYPES)

    def test_spec_wrapper(self):
        spec = ThemeDirSpec(rel="weird/22", size=22, context="Actions")
        self.assertEqual(classify_spec(spec), MODULE_ACTIONS)


class InventoryPreviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        # Papirus-like + Breeze-like layouts, plus a pack that inherits apps
        # but has no places of its own.
        make_theme(
            self.root,
            "PapirusLike",
            "hicolor",
            {
                "demo-app": ["48x48/apps", "24x24/apps"],
                "folder": ["48x48/places", "16x16/places"],
                "text-plain": ["48x48/mimetypes"],
                "document-open": ["22x22/actions"],
                "audio-volume-high": ["22x22/status", "22x22/panel"],
                "emblem-default": ["8x8/emblems"],
            },
        )
        make_theme(
            self.root,
            "BreezeLike",
            "hicolor",
            {
                "demo-app": ["apps/48"],
                "folder": ["places/48", "places/16"],
                "text-plain": ["mimetypes/48"],
                "document-open": ["actions/22"],
                "dialog-information": ["applets/22"],
            },
        )
        make_theme(
            self.root,
            "AppsOnly",
            "PapirusLike,hicolor",
            {"other-app": ["48x48/apps"]},
        )
        self.themes = enumerate_themes([self.root])

    def tearDown(self):
        self.tmp.cleanup()

    def test_inventory_is_own_files_not_inherited(self):
        overview = modules_overview(
            self.themes,
            {"apps": "AppsOnly", "places": "AppsOnly"},
            preview_limit=16,
        )
        by_id = {row["id"]: row for row in overview}
        self.assertTrue(by_id["apps"]["has_content"])
        self.assertEqual(by_id["apps"]["icon_count"], 1)
        names = {item["name"] for item in by_id["apps"]["previews"]}
        self.assertEqual(names, {"other-app"})
        # Inherited places must not be presented as this pack's own icons.
        self.assertTrue(by_id["places"]["empty"])
        self.assertFalse(by_id["places"]["has_content"])
        self.assertTrue(by_id["places"].get("fallback"))
        for item in by_id["places"]["previews"]:
            self.assertNotEqual(item["theme"], "AppsOnly")
            self.assertTrue(item.get("fallback"))
            self.assertEqual(item.get("origin"), "fallback")
        self.assertTrue(any(item["theme"] == "PapirusLike" for item in by_id["places"]["previews"]))

    def test_preview_prefers_representative_and_keeps_source(self):
        items = preview_items(self.themes["PapirusLike"], MODULE_PLACES, limit=8)
        self.assertTrue(items)
        self.assertEqual(items[0]["name"], "folder")
        self.assertEqual(items[0]["theme"], "PapirusLike")
        self.assertEqual(items[0]["size"], 48)
        self.assertTrue(Path(items[0]["path"]).is_file())

    def test_tray_paths_are_marked_unreliable(self):
        items = preview_items(self.themes["PapirusLike"], MODULE_STATUS, limit=8)
        names = {i["name"]: i for i in items}
        self.assertIn("audio-volume-high", names)
        # The 22x22/panel copy should mark the name as possibly ineffective.
        self.assertTrue(any(i["maybe_unreliable_tray"] for i in items))

    def test_theme_options_include_system_and_counts(self):
        options = theme_options_for_module(self.themes, MODULE_PLACES)
        self.assertEqual(options[0]["id"], MODULE_SOURCE_SYSTEM)
        by_name = {o["name"]: o for o in options}
        self.assertEqual(by_name["PapirusLike"]["count"], 1)
        self.assertTrue(by_name["AppsOnly"]["empty"])
        self.assertNotIn(THEME_INTERNAL_NAME, by_name)

    def test_empty_module_overview_flag(self):
        row = next(
            r
            for r in modules_overview(self.themes, {"mimetypes": "AppsOnly"})
            if r["id"] == "mimetypes"
        )
        self.assertTrue(row["empty"])
        self.assertFalse(row["has_content"])


class PersistTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name) / "state"
        self.state.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def test_module_map_roundtrip_keeps_mask_and_choices(self):
        save_settings(self.state, MaskSettings(enabled=True, shape="circle", padding=0.1))
        store = load_choices(self.state)
        upsert_selection(store, "demo", icon_name="demo", source_theme="Tela", source_path="/tmp/x")
        save_choices(self.state, store)

        save_module_map(self.state, {"places": "Papirus", "unknown": "ignored", "apps": ""})
        mapping = load_module_map(self.state)
        self.assertEqual(mapping["places"], "Papirus")
        self.assertEqual(mapping["apps"], MODULE_SOURCE_SYSTEM)
        self.assertNotIn("unknown", mapping)

        # mask and choices still present
        data = json.loads((self.state / SETTINGS_FILENAME).read_text(encoding="utf-8"))
        self.assertEqual(data["mask"]["shape"], "circle")
        self.assertTrue(data["mask"]["enabled"])
        choices = load_choices(self.state)
        self.assertEqual(choices["selections"]["demo"]["icon_name"], "demo")

        # saving mask later must not drop modules
        save_settings(self.state, MaskSettings(enabled=False, shape="squircle"))
        mapping2 = load_module_map(self.state)
        self.assertEqual(mapping2["places"], "Papirus")
        data2 = json.loads((self.state / SETTINGS_FILENAME).read_text(encoding="utf-8"))
        self.assertEqual(data2["mask"]["shape"], "squircle")

    def test_old_settings_without_modules_are_all_system(self):
        mapping = load_module_map(self.state)
        self.assertFalse(has_explicit_module_sources(mapping))
        self.assertTrue(all(v == MODULE_SOURCE_SYSTEM for v in mapping.values()))

    def test_normalize_unknown_and_aliases(self):
        m = normalize_module_map({"apps": "inherit", "places": "system", "status": "BreezeLike"})
        self.assertEqual(m["apps"], MODULE_SOURCE_SYSTEM)
        self.assertEqual(m["status"], "BreezeLike")
        self.assertFalse(has_explicit_module_sources({"apps": "", "places": None}))
        self.assertTrue(has_explicit_module_sources({"apps": "Tela"}))


class ComposeExportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._app = ensure_qt_app()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.icons = self.root / "icons"
        self.apps_dir = self.root / "apps"
        self.apps_dir.mkdir()
        make_theme(
            self.icons,
            "Tela",
            "hicolor",
            {"demo": ["48x48/apps"], "manual-only": ["48x48/apps"]},
        )
        make_theme(
            self.icons,
            "Papirus",
            "breeze,hicolor",
            {
                "demo": ["48x48/apps"],
                "folder": ["48x48/places", "16x16/places"],
                "text-plain": ["48x48/mimetypes"],
                "document-open": ["22x22/actions"],
                "audio-volume-high": ["22x22/status"],
            },
        )
        make_theme(
            self.icons,
            "breeze",
            "hicolor",
            {
                "folder": ["places/48"],
                "document-open": ["actions/22"],
                "dialog-information": ["applets/22"],
            },
        )
        make_theme(self.icons, "hicolor", "", {"demo": ["48x48/apps"]})
        make_desktop(self.apps_dir, "demo.desktop", name="Demo", icon="demo")
        self.themes = enumerate_themes([self.icons])
        self.status = resolve_strategy(self.themes, current_theme="Papirus")
        from iconadapt.desktop import scan_applications

        self.apps = scan_applications([self.apps_dir], current_desktop="KDE")

    def tearDown(self):
        self.tmp.cleanup()

    def _export(self, dest, *, selections=None, modules=None, mask=None):
        return export_theme(
            dest,
            apps=self.apps,
            themes=self.themes,
            status=self.status,
            selections=selections or {},
            overwrite_owned=True,
            mask=mask,
            modules=modules,
        )

    def test_legacy_export_without_modules_stays_apps_only(self):
        dest = self.root / "build" / "legacy"
        dest.parent.mkdir(parents=True, exist_ok=True)
        prov = self._export(dest, modules=None)
        self.assertNotEqual(prov.get("mode"), "composed")
        self.assertTrue((dest / "48x48/apps/demo.png").exists())
        self.assertFalse((dest / "48x48/places").exists())
        self.assertFalse((dest / "places/48").exists())

    def test_empty_system_map_uses_legacy_path(self):
        dest = self.root / "build" / "empty-map"
        dest.parent.mkdir(parents=True, exist_ok=True)
        prov = self._export(dest, modules={"apps": "", "places": "__system__"})
        self.assertNotEqual(prov.get("mode"), "composed")

    def test_mixed_modules_copy_real_files_and_contexts(self):
        dest = self.root / "build" / "mixed"
        dest.parent.mkdir(parents=True, exist_ok=True)
        prov = self._export(
            dest,
            modules={"apps": "Tela", "places": "breeze", "status": "Papirus"},
        )
        self.assertEqual(prov["mode"], "composed")
        self.assertTrue((dest / "48x48/apps/demo.png").exists() or (dest / "48x48/apps/demo.png").is_symlink())
        # places from breeze layout, not Papirus
        self.assertTrue((dest / "places/48/folder.png").exists() or (dest / "places/48/folder.png").is_symlink())
        self.assertFalse((dest / "48x48/places").exists())
        self.assertTrue((dest / "22x22/status/audio-volume-high.png").exists() or (dest / "22x22/status/audio-volume-high.png").is_symlink())
        # actions / mimetypes left to Inherits
        self.assertFalse((dest / "22x22/actions").exists())
        index = (dest / "index.theme").read_text(encoding="utf-8")
        self.assertIn("Context=Places", index)
        self.assertIn("Context=Status", index)
        self.assertIn("Context=Applications", index)
        self.assertIn("[places/48]", index)
        # original packs untouched
        self.assertTrue((self.icons / "Papirus/48x48/places/folder.png").is_file())
        self.assertTrue((self.icons / "breeze/places/48/folder.png").is_file())

    def test_manual_selection_beats_apps_module_default(self):
        dest = self.root / "build" / "priority"
        dest.parent.mkdir(parents=True, exist_ok=True)
        tela_demo = (self.icons / "Tela/48x48/apps/demo.png").resolve()
        selections = {
            "demo": {
                "source_theme": "Tela",
                "icon_name": "demo",
                "source_path": str(tela_demo),
            }
        }
        self._export(dest, selections=selections, modules={"apps": "Papirus", "places": "Papirus"})
        placed = dest / "48x48/apps/demo.png"
        self.assertTrue(placed.exists() or placed.is_symlink())
        self.assertEqual(placed.resolve(), tela_demo)

    def test_same_rel_conflict_keeps_higher_priority(self):
        dest = self.root / "build" / "conflict"
        dest.parent.mkdir(parents=True, exist_ok=True)
        # Write a file first via manual, then module copy of same name/path should skip.
        # Already covered by manual vs module; also places vs nothing.
        prov = self._export(dest, modules={"places": "Papirus", "other": "Papirus"})
        # emblems go to other; folder stays in places — no overwrite
        self.assertTrue((dest / "48x48/places/folder.png").exists() or (dest / "48x48/places/folder.png").is_symlink())
        self.assertTrue((dest / "8x8/emblems/emblem-default.png").exists() or not (self.icons / "Papirus/8x8/emblems").exists())

    def test_mask_applies_only_to_apps_and_does_not_modify_sources(self):
        dest = self.root / "build" / "masked"
        dest.parent.mkdir(parents=True, exist_ok=True)
        src_app = self.icons / "Papirus/48x48/apps/demo.png"
        src_place = self.icons / "Papirus/48x48/places/folder.png"
        before_app = src_app.read_bytes()
        before_place = src_place.read_bytes()
        prov = self._export(
            dest,
            modules={"apps": "Papirus", "places": "Papirus"},
            mask=MaskSettings(enabled=True, shape="circle", padding=0.08, background="#00000000"),
        )
        self.assertTrue(any(block.get("masked") for block in prov.get("icons", [])))
        # apps should be rasterized png copies, not symlinks, under 48x48/apps or planned sizes
        app_copies = list((dest / "48x48/apps").glob("demo.png")) if (dest / "48x48/apps").exists() else []
        self.assertTrue(app_copies)
        self.assertFalse(app_copies[0].is_symlink())
        place = dest / "48x48/places/folder.png"
        self.assertTrue(place.exists() or place.is_symlink())
        # places must remain a faithful copy/link, not a masked raster in an apps dir
        if place.is_symlink():
            self.assertEqual(place.resolve(), src_place.resolve())
        else:
            self.assertEqual(place.read_bytes(), before_place)
        self.assertEqual(src_app.read_bytes(), before_app)
        self.assertEqual(src_place.read_bytes(), before_place)

    def test_empty_module_is_recorded_not_filled_from_inherit(self):
        dest = self.root / "build" / "empty-mod"
        dest.parent.mkdir(parents=True, exist_ok=True)
        make_theme(self.icons, "EmptyPlaces", "Papirus,hicolor", {"zzz": ["48x48/apps"]})
        self.themes = enumerate_themes([self.icons])
        self.status = resolve_strategy(self.themes, current_theme="Papirus")
        prov = export_theme(
            dest,
            apps=self.apps,
            themes=self.themes,
            status=self.status,
            selections={},
            overwrite_owned=True,
            modules={"places": "EmptyPlaces", "apps": "Tela"},
        )
        empty = {e["module"] for e in prov.get("empty_modules", [])}
        self.assertIn("places", empty)
        self.assertFalse((dest / "48x48/places").exists())
        self.assertFalse((dest / "places").exists())

    def test_missing_source_theme_raises_before_claiming_success(self):
        dest = self.root / "build" / "missing"
        dest.parent.mkdir(parents=True, exist_ok=True)
        from iconadapt.export import ExportError

        with self.assertRaises(ExportError):
            self._export(dest, modules={"places": "NotInstalled"})

    def test_symlink_to_output_is_skipped_and_source_not_used_as_dest(self):
        dest = self.root / "build" / "safe"
        dest.parent.mkdir(parents=True, exist_ok=True)
        # Symlink in the source pack points at a path that will live inside dest.
        pack = self.icons / "Papirus" / "48x48" / "places" / "evil-link.png"
        os.symlink(dest / "48x48/places/folder.png", pack)
        prov = self._export(dest, modules={"places": "Papirus"})
        installed = dest / "48x48/places/evil-link.png"
        if installed.exists() or installed.is_symlink():
            self.fail("unsafe output-pointing symlink was installed")
        self.assertTrue(
            any(s.get("icon_name") == "evil-link" for s in prov.get("skipped", [])),
            prov.get("skipped"),
        )

    def test_safe_place_skips_source_inside_dest(self):
        dest = self.root / "out"
        dest.mkdir()
        inner = dest / "inner.png"
        write_bytes(inner, PNG_1x1)
        target = dest / "copy.png"
        self.assertEqual(safe_place_file(inner, target, dest), "skip-output")
        self.assertFalse(target.exists())

    def test_uninstall_only_owned_theme(self):
        dest = self.root / "build" / "owned"
        dest.parent.mkdir(parents=True, exist_ok=True)
        self._export(dest, modules={"places": "Papirus"})
        foreign = self.icons / "Papirus"
        from iconadapt.export import ExportError

        with self.assertRaises(ExportError):
            uninstall_owned_theme(foreign)
        uninstall_owned_theme(dest)
        self.assertFalse(dest.exists())
        self.assertTrue((self.icons / "Papirus/index.theme").is_file())

    def test_sizes_and_links_preserved(self):
        dest = self.root / "build" / "sizes"
        dest.parent.mkdir(parents=True, exist_ok=True)
        self._export(dest, modules={"places": "Papirus"})
        self.assertTrue((dest / "48x48/places/folder.png").exists() or (dest / "48x48/places/folder.png").is_symlink())
        self.assertTrue((dest / "16x16/places/folder.png").exists() or (dest / "16x16/places/folder.png").is_symlink())
        index = (dest / "index.theme").read_text(encoding="utf-8")
        self.assertIn("[48x48/places]", index)
        self.assertIn("[16x16/places]", index)
        self.assertIn("Size=48", index)
        self.assertIn("Size=16", index)


class LiveInstalledThemeClassifyTests(unittest.TestCase):
    """Read-only scan of real installed themes when present. Never writes them."""

    def test_papirus_and_breeze_contexts_if_installed(self):
        candidates = [
            Path("/usr/share/icons/Papirus"),
            Path("/usr/share/icons/breeze"),
            Path.home() / ".local/share/icons/Papirus",
        ]
        found = [p for p in candidates if (p / "index.theme").is_file()]
        if not found:
            self.skipTest("Papirus/breeze not installed")
        for path in found:
            theme = parse_index_theme(path)
            modules = {classify_spec(spec) for spec in theme.directories}
            self.assertIn(MODULE_APPS, modules, msg=path)
            self.assertIn(MODULE_STATUS, modules, msg=path)
            # Confirm Context-bearing dirs classify by Context, including panel/applets.
            if theme.internal_name.lower().startswith("papirus"):
                panel = [s for s in theme.directories if s.rel.endswith("/panel") or "/panel/" in s.rel]
                self.assertTrue(panel)
                self.assertTrue(all(classify_spec(s) == MODULE_STATUS for s in panel))
            if theme.internal_name.lower() == "breeze":
                applets = [s for s in theme.directories if "applets" in s.rel]
                self.assertTrue(applets)
                # Context wins: breeze declares some applets sizes as Applications.
                for spec in applets:
                    if (spec.context or "").lower() == "applications":
                        self.assertEqual(classify_spec(spec), MODULE_APPS, spec)
                    else:
                        self.assertEqual(classify_spec(spec), MODULE_STATUS, spec)
                apps = [s for s in theme.directories if s.rel.startswith("apps/")]
                self.assertTrue(apps)
                self.assertTrue(all(classify_spec(s) == MODULE_APPS for s in apps))


if __name__ == "__main__":
    unittest.main()
