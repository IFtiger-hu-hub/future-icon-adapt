import sys
import tempfile
import unittest
from pathlib import Path

from iconadapt.backend import IconAdaptBackend
from tests.qtutil import ensure_qt_app
from tests.jobutil import wait_theme_job
from iconadapt.constants import OWNED_MARKER, THEME_INTERNAL_NAME
from tests.fixtures import make_desktop, make_theme


class BackendIsolatedTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._app = ensure_qt_app()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.data = self.root / "data"
        self.config = self.root / "config"
        icons = self.root / "icons"
        apps_sys = self.root / "apps-sys"
        make_theme(icons, "Tela", "hicolor", {"demo": ["48x48/apps"]})
        make_theme(icons, "Papirus", "breeze,hicolor", {"demo": ["48x48/apps"]})
        make_theme(icons, "breeze", "hicolor", {"demo": ["48x48/apps"]})
        make_theme(icons, "hicolor", "", {})
        make_desktop(apps_sys, "demo.desktop", name="Demo", icon="demo")
        make_desktop(apps_sys, "gone.desktop", name="Gone", icon="no-such-icon")
        self.iso = {
            "data_home": str(self.data),
            "config_home": str(self.config),
            "icon_dirs": [str(icons)],
            "app_dirs": [str(apps_sys)],
            "current_theme": "Papirus",
            "current_desktop": "KDE",
        }

    def tearDown(self):
        self.tmp.cleanup()

    def test_scan_and_lazy_detail_and_export(self):
        be = IconAdaptBackend(isolated=self.iso)
        self.assertEqual(be.currentTheme, "Papirus")
        import json
        apps = json.loads(be.appsJson)
        ids = {a["desktop_id"] for a in apps}
        self.assertEqual(ids, {"demo", "gone"})
        gone = next(a for a in apps if a["desktop_id"] == "gone")
        self.assertEqual(gone["status"], "missing")
        detail = json.loads(be.appDetailJson("demo"))
        self.assertTrue(detail["candidates"])
        themes = {c["theme"] for c in detail["candidates"]}
        self.assertIn("Tela", themes)
        be.saveSelection("demo", "demo", "Tela", detail["candidates"][0]["path"])
        out = wait_theme_job(be, "generateTheme")
        self.assertTrue(out["ok"])
        dest = self.data / "icon-adapt" / "build" / THEME_INTERNAL_NAME
        self.assertTrue((dest / OWNED_MARKER).is_file())
        inst = wait_theme_job(be, "installUserTheme")
        self.assertTrue(inst["ok"])
        installed = self.data / "icons" / THEME_INTERNAL_NAME
        self.assertTrue((installed / OWNED_MARKER).is_file())
        un = wait_theme_job(be, "uninstallOutputs")
        self.assertTrue(un["ok"])
        self.assertFalse(installed.exists())
        # hand selection state remains
        self.assertTrue((self.data / "icon-adapt").exists())

    def test_search_combines_with_filter(self):
        import json
        make_desktop(Path(self.iso["app_dirs"][0]), "wechat.desktop", name="微信", icon="demo", extra="Comment=Tencent")
        be = IconAdaptBackend(isolated=self.iso)
        be.setQuery("微信")
        ids = {a["desktop_id"] for a in json.loads(be.appsJson)}
        self.assertIn("wechat", ids)
        self.assertNotIn("gone", ids)
        be.setFilter("missing")
        ids = {a["desktop_id"] for a in json.loads(be.appsJson)}
        self.assertEqual(ids, set())
        be.setQuery("gone")
        ids = {a["desktop_id"] for a in json.loads(be.appsJson)}
        self.assertEqual(ids, {"gone"})

    def test_mask_toggle_persists(self):
        import json
        be = IconAdaptBackend(isolated=self.iso)
        raw = json.loads(be.setMaskJson(json.dumps({
            "enabled": True, "shape": "circle", "padding": 0.05, "background": "#00000000"
        })))
        self.assertTrue(raw["ok"])
        be2 = IconAdaptBackend(isolated=self.iso)
        mask = json.loads(be2.maskJson)
        self.assertTrue(mask["enabled"])
        self.assertEqual(mask["shape"], "circle")
        self.assertAlmostEqual(mask["padding"], 0.05)
        bad = json.loads(be2.setMaskJson(json.dumps({
            "enabled": True, "shape": "custom", "custom_path": "/no/such/mask.png"
        })))
        self.assertFalse(bad["ok"])
        # failed save must not replace good settings
        be3 = IconAdaptBackend(isolated=self.iso)
        mask = json.loads(be3.maskJson)
        self.assertTrue(mask["enabled"])
        self.assertEqual(mask["shape"], "circle")

    def test_install_refresh_does_not_bounce_when_not_adapt(self):
        import json
        log = {"change_icons": []}
        # isolated current_theme is Papirus; hooks record only
        from iconadapt.refresh import isolated_hooks, RefreshHooks

        recorded = []

        def fake_change(name: str) -> None:
            recorded.append(name)

        hooks = isolated_hooks(log)
        hooks.change_icons = fake_change
        self.iso["refresh_hooks"] = hooks
        self.iso["cache_home"] = str(self.root / "cache")
        cache = Path(self.iso["cache_home"])
        cache.mkdir()
        (cache / "icon-cache.kcache").write_text("x", encoding="utf-8")
        (cache / "plasma_theme_default.kcache").write_text("keep", encoding="utf-8")
        be = IconAdaptBackend(isolated=self.iso)
        self.assertEqual(be.currentTheme, "Papirus")
        be.saveSelection("demo", "demo", "Tela", str(Path(self.iso["icon_dirs"][0]) / "Tela" / "48x48" / "apps" / "demo.png"))
        inst = wait_theme_job(be, "installTheme")
        self.assertTrue(inst["ok"])
        self.assertIn("refresh", inst)
        self.assertTrue(inst["refresh"]["cache_cleared"])
        self.assertFalse((cache / "icon-cache.kcache").exists())
        self.assertTrue((cache / "plasma_theme_default.kcache").is_file())
        self.assertTrue(inst["refresh"]["signals"])
        self.assertFalse(inst["refresh"]["bounced"])
        self.assertEqual(recorded, [])
        un = wait_theme_job(be, "uninstallOutputs")
        self.assertTrue(un["ok"])
        self.assertIsNotNone(un["refresh"])
        self.assertFalse(un["refresh"]["bounced"])
        self.assertEqual(recorded, [])



    def test_module_selection_preview_persist_and_composed_install(self):
        import json
        from tests.fixtures import make_theme

        icons = Path(self.iso["icon_dirs"][0])
        make_theme(icons, "FolderPack", "hicolor", {"folder": ["48x48/places"]})
        self.iso["cache_home"] = str(self.root / "cache")
        Path(self.iso["cache_home"]).mkdir(exist_ok=True)
        be = IconAdaptBackend(isolated=self.iso)
        rows = {r["id"]: r for r in json.loads(be.modulesJson)}
        self.assertIn("places", rows)
        self.assertEqual(rows["places"]["source"], "__system__")
        self.assertFalse(rows["places"]["has_content"])
        names = {o["name"] for o in json.loads(be.moduleThemeListJson())}
        self.assertIn("FolderPack", names)
        self.assertIn("__system__", names)
        self.assertNotIn("icon-adapt", names)
        opts = {o["name"]: o for o in json.loads(be.moduleThemeOptionsJson("places"))}
        self.assertFalse(opts["FolderPack"]["empty"])
        self.assertGreater(opts["FolderPack"]["count"], 0)

        preview = json.loads(be.modulePreviewJson("places", "FolderPack", 8))
        items = preview["previews"] if isinstance(preview, dict) else preview
        self.assertTrue(items)
        self.assertEqual(items[0]["name"], "folder")
        self.assertEqual(items[0]["theme"], "FolderPack")
        self.assertTrue(Path(items[0]["path"]).is_file())

        # Tela fixture has no places of its own; any preview must be marked fallback.
        tela = json.loads(be.modulePreviewJson("places", "Tela", 8))
        self.assertFalse(tela.get("has_content"))
        self.assertTrue(tela.get("empty"))
        for item in tela.get("previews") or []:
            self.assertNotEqual(item["theme"], "Tela")
            self.assertTrue(item.get("fallback"))

        saved = json.loads(be.setModuleSource("places", "FolderPack"))
        self.assertTrue(saved["ok"])
        be.saveSelection(
            "demo",
            "demo",
            "Tela",
            str(icons / "Tela" / "48x48" / "apps" / "demo.png"),
        )
        mask = json.loads(be.setMaskJson(json.dumps({
            "enabled": False, "shape": "circle", "padding": 0.08, "background": "#00000000"
        })))
        self.assertTrue(mask["ok"])

        be2 = IconAdaptBackend(isolated=self.iso)
        rows2 = {r["id"]: r for r in json.loads(be2.modulesJson)}
        self.assertEqual(rows2["places"]["source"], "FolderPack")
        self.assertTrue(rows2["places"]["has_content"])
        self.assertFalse(json.loads(be2.maskJson)["enabled"])
        detail = json.loads(be2.appDetailJson("demo"))
        self.assertTrue(detail.get("selection"))

        inst = wait_theme_job(be2, "installTheme")
        self.assertTrue(inst["ok"])
        installed = Path(self.iso["data_home"]) / "icons" / "icon-adapt"
        place = installed / "48x48" / "places" / "folder.png"
        self.assertTrue(place.exists() or place.is_symlink())
        index = (installed / "index.theme").read_text(encoding="utf-8")
        self.assertIn("Context=Places", index)
        # original pack untouched
        self.assertTrue((icons / "FolderPack" / "48x48" / "places" / "folder.png").is_file())

        bad = json.loads(be2.setModuleSource("not-a-module", "FolderPack"))
        self.assertFalse(bad["ok"])
        missing = json.loads(be2.setModuleSource("places", "NoSuchPack"))
        self.assertFalse(missing["ok"])
        # failed change must not drop previous places choice
        rows3 = {r["id"]: r for r in json.loads(be2.modulesJson)}
        self.assertEqual(rows3["places"]["source"], "FolderPack")

    def test_install_without_modules_stays_legacy_overlay(self):
        import json
        from tests.fixtures import make_theme

        icons = Path(self.iso["icon_dirs"][0])
        make_theme(icons, "FolderPack", "hicolor", {"folder": ["48x48/places"]})
        self.iso["cache_home"] = str(self.root / "cache")
        Path(self.iso["cache_home"]).mkdir(exist_ok=True)
        be = IconAdaptBackend(isolated=self.iso)
        inst = wait_theme_job(be, "installTheme")
        self.assertTrue(inst["ok"])
        installed = Path(self.iso["data_home"]) / "icons" / "icon-adapt"
        self.assertFalse((installed / "48x48" / "places").exists())
        self.assertTrue((installed / "48x48" / "apps" / "demo.png").exists() or (installed / "48x48" / "apps" / "demo.png").is_symlink())



    def test_system_module_previews_use_inherit_not_adapt(self):
        import json
        from tests.fixtures import make_theme, write_bytes, PNG_1x1, mini_index

        icons = Path(self.iso["icon_dirs"][0])
        adapt = make_theme(icons, "icon-adapt", "Papirus,breeze,hicolor", {"folder": ["48x48/places"]})
        write_bytes(adapt / "48x48" / "places" / "only-in-adapt.png", PNG_1x1)
        (icons / "Papirus" / "48x48" / "places").mkdir(parents=True, exist_ok=True)
        write_bytes(icons / "Papirus" / "48x48" / "places" / "folder.png", PNG_1x1)
        (icons / "Papirus" / "index.theme").write_text(
            mini_index("Papirus", "breeze,hicolor", "48x48/apps,48x48/places"),
            encoding="utf-8",
        )
        self.iso["current_theme"] = "icon-adapt"
        be = IconAdaptBackend(isolated=self.iso)
        self.assertEqual(be.currentTheme, "icon-adapt")
        rows = {r["id"]: r for r in json.loads(be.modulesJson)}
        places = rows["places"]
        self.assertEqual(places["source"], "__system__")
        self.assertTrue(places["previews"], places.get("status_line"))
        names = {i["name"] for i in places["previews"]}
        themes = {i["theme"] for i in places["previews"]}
        self.assertNotIn("icon-adapt", themes)
        self.assertNotIn("only-in-adapt", names)
        self.assertIn("Papirus", themes)
        for item in places["previews"]:
            self.assertEqual(item.get("origin"), "base")
            self.assertFalse(item.get("fallback"))
            if item.get("preview_url"):
                self.assertTrue(item["preview_url"].startswith("data:image/png"))
                self.assertNotEqual(item.get("render_status"), "error")



if __name__ == "__main__":
    unittest.main()
