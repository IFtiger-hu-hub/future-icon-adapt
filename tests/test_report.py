import tempfile
import unittest
from pathlib import Path

from iconadapt.desktop import scan_applications
from iconadapt.report import classify_app, report_apps
from iconadapt.theme import enumerate_themes
from tests.fixtures import make_desktop, make_theme, write, write_bytes, PNG_1x1


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.icons = self.root / "icons"
        self.appsdir = self.root / "apps"
        self.icons.mkdir()
        self.appsdir.mkdir()
        make_theme(self.icons, "Tela", "hicolor", {"wechat": ["48x48/apps"]})
        make_theme(self.icons, "Papirus", "breeze,hicolor", {"wechat": ["48x48/apps"], "okapp": ["48x48/apps"]})
        make_theme(self.icons, "breeze", "hicolor", {"folder": ["48x48/apps"]})
        make_theme(self.icons, "hicolor", "", {})
        self.themes = enumerate_themes([self.icons])

    def tearDown(self):
        self.tmp.cleanup()

    def test_absolute_existing_is_not_missing(self):
        icon = self.root / "logo.png"
        write_bytes(icon, PNG_1x1)
        make_desktop(self.appsdir, "abs.desktop", name="Abs", icon=str(icon))
        app = scan_applications([self.appsdir], current_desktop="KDE")[0]
        rep = classify_app(app, "Papirus", self.themes)
        self.assertEqual(rep.status, "absolute_ok")
        self.assertNotEqual(rep.status, "missing")

    def test_absolute_missing_file(self):
        make_desktop(self.appsdir, "gone.desktop", name="Gone", icon="/no/such/icon.svg")
        app = scan_applications([self.appsdir], current_desktop="KDE")[0]
        rep = classify_app(app, "Papirus", self.themes)
        self.assertEqual(rep.status, "absolute_missing")

    def test_fallback_and_ok_and_missing(self):
        make_desktop(self.appsdir, "okapp.desktop", name="Ok", icon="okapp")
        make_desktop(self.appsdir, "folder.desktop", name="Folder", icon="folder")
        make_desktop(self.appsdir, "none.desktop", name="None", icon="does-not-exist")
        apps = scan_applications([self.appsdir], current_desktop="KDE")
        rows = {r.desktop_id: r for r in report_apps(apps, "Papirus", self.themes)}
        self.assertEqual(rows["okapp"].status, "ok")
        self.assertEqual(rows["folder"].status, "fallback")
        self.assertEqual(rows["none"].status, "missing")
        self.assertTrue(rows["none"].hints is not None)

    def test_hints_not_auto_for_qq(self):
        make_theme(self.icons, "Other", "hicolor", {"qq": ["48x48/apps"]})
        themes = enumerate_themes([self.icons])
        make_desktop(self.appsdir, "linuxqq.desktop", name="QQ", icon="linuxqq")
        app = scan_applications([self.appsdir], current_desktop="KDE")[0]
        rep = classify_app(app, "Papirus", themes)
        self.assertEqual(rep.status, "missing")
        kinds = {h["kind"] for h in rep.hints}
        names = {h["icon_name"] for h in rep.hints}
        self.assertIn("alias", kinds)
        self.assertIn("qq", names)


if __name__ == "__main__":
    unittest.main()
