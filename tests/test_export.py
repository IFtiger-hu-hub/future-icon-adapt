import tempfile
import unittest
from pathlib import Path

from iconadapt.desktop import scan_applications
from iconadapt.export import ExportError, export_theme, is_tool_owned, uninstall_owned_theme
from iconadapt.persist import upsert_selection
from iconadapt.strategy import resolve_strategy
from iconadapt.theme import enumerate_themes, parse_index_theme
from tests.fixtures import PNG_1x1, make_desktop, make_theme, write, write_bytes


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.icons = self.root / "icons"
        self.appsdir = self.root / "apps"
        self.icons.mkdir()
        self.appsdir.mkdir()
        make_theme(self.icons, "Tela", "hicolor", {"wechat": ["48x48/apps", "scalable/apps"]})
        make_theme(self.icons, "Colloid", "hicolor", {"cn.feishu.Feishu": ["scalable/apps"]})
        make_theme(self.icons, "Papirus", "breeze,hicolor", {"okapp": ["48x48/apps"]})
        make_theme(self.icons, "breeze", "hicolor", {"folder": ["48x48/apps"]})
        make_theme(self.icons, "hicolor", "", {})
        # action icon in Tela should not be mixed even if a desktop asked... we only export app icons
        write_bytes(self.icons / "Tela" / "48x48" / "actions" / "edit-copy.png", PNG_1x1)
        self.themes = enumerate_themes([self.icons])
        self.status = resolve_strategy(self.themes, current_theme="Papirus")
        make_desktop(self.appsdir, "wechat.desktop", name="WeChat", icon="wechat")
        make_desktop(self.appsdir, "feishu.desktop", name="Feishu", icon="cn.feishu.Feishu")
        make_desktop(self.appsdir, "okapp.desktop", name="Ok", icon="okapp")
        make_desktop(self.appsdir, "abs.desktop", name="Abs", icon="/opt/x.png")
        self.apps = scan_applications([self.appsdir], current_desktop="KDE")

    def tearDown(self):
        self.tmp.cleanup()

    def test_export_tela_and_colloid_inherit_papirus(self):
        dest = self.root / "out" / "icon-adapt"
        dest.parent.mkdir()
        prov = export_theme(dest, apps=self.apps, themes=self.themes, status=self.status, selections={}, overwrite_owned=True)
        self.assertTrue(is_tool_owned(dest))
        self.assertEqual(self.status.inherit, ["Papirus", "breeze", "hicolor"])
        theme = parse_index_theme(dest)
        self.assertEqual(theme.inherits, ["Papirus", "breeze", "hicolor"])
        self.assertTrue((dest / "48x48" / "apps" / "wechat.png").exists())
        self.assertTrue((dest / "scalable" / "apps" / "cn.feishu.Feishu.png").exists())
        # okapp relies on Papirus inherit — not necessarily copied
        copied = {row["icon_name"] for row in prov["icons"]}
        self.assertIn("wechat", copied)
        self.assertIn("cn.feishu.Feishu", copied)
        self.assertNotIn("okapp", copied)
        # no actions mixed in
        self.assertFalse((dest / "48x48" / "actions").exists())

    def test_refuse_overwrite_foreign_dir(self):
        dest = self.root / "out" / "icon-adapt"
        dest.mkdir(parents=True)
        write(dest / "index.theme", "[Icon Theme]\nName=Foreign\nDirectories=\n")
        with self.assertRaises(ExportError):
            export_theme(dest, apps=self.apps, themes=self.themes, status=self.status, selections={})

    def test_manual_selection_and_uninstall_safety(self):
        dest = self.root / "out" / "icon-adapt"
        dest.parent.mkdir()
        sels = {}
        upsert_selection(sels, "okapp", icon_name="okapp", source_theme="Papirus", source_path=str(self.icons / "Papirus" / "48x48" / "apps" / "okapp.png"))
        # upsert_selection expects store with selections key
        from iconadapt.persist import empty_store
        store = empty_store()
        upsert_selection(store, "okapp", icon_name="okapp", source_theme="Papirus", source_path=str(self.icons / "Papirus" / "48x48" / "apps" / "okapp.png"))
        export_theme(dest, apps=self.apps, themes=self.themes, status=self.status, selections=store["selections"])
        self.assertTrue((dest / "48x48" / "apps" / "okapp.png").exists())
        msg = uninstall_owned_theme(dest)
        self.assertIn("已删除", msg)
        self.assertFalse(dest.exists())
        # cannot uninstall papirus
        with self.assertRaises(ExportError):
            uninstall_owned_theme(self.icons / "Papirus")

    def test_missing_tela_is_honest(self):
        # rebuild themes without Tela
        icons2 = self.root / "icons2"
        icons2.mkdir()
        make_theme(icons2, "Papirus", "breeze,hicolor", {"okapp": ["48x48/apps"]})
        make_theme(icons2, "breeze", "hicolor", {})
        make_theme(icons2, "hicolor", "", {})
        themes = enumerate_themes([icons2])
        status = resolve_strategy(themes)
        self.assertIsNone(status.tela)
        self.assertIn("Tela", status.missing)
        dest = self.root / "out2" / "icon-adapt"
        dest.parent.mkdir()
        prov = export_theme(dest, apps=self.apps, themes=themes, status=status, selections={})
        self.assertFalse((dest / "48x48" / "apps" / "wechat.png").exists())
        self.assertIn("Papirus", status.inherit)


if __name__ == "__main__":
    unittest.main()
