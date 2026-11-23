import tempfile
import unittest
from pathlib import Path

from iconadapt.desktop import scan_applications
from iconadapt.report import report_apps
from iconadapt.search import filter_items, matches_app, matches_query, matches_report
from iconadapt.theme import enumerate_themes
from tests.fixtures import make_desktop, make_theme


class SearchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.appsdir = self.root / "apps"
        self.icons = self.root / "icons"
        self.appsdir.mkdir()
        self.icons.mkdir()
        make_theme(self.icons, "Papirus", "hicolor", {"wechat": ["48x48/apps"], "okapp": ["48x48/apps"]})
        make_theme(self.icons, "hicolor", "", {})
        make_desktop(
            self.appsdir,
            "wechat.desktop",
            name="微信 WeChat",
            icon="wechat",
            extra="GenericName=Instant Messenger\nComment=Tencent chat",
        )
        make_desktop(
            self.appsdir,
            "org.kde.dolphin.desktop",
            name="Dolphin",
            icon="org.kde.dolphin",
            extra="GenericName=File Manager\nComment=文件管理器",
        )
        make_desktop(self.appsdir, "gone.desktop", name="Missing App", icon="no-such-icon")
        self.apps = scan_applications([self.appsdir], current_desktop="KDE")
        self.themes = enumerate_themes([self.icons])
        self.rows = report_apps(self.apps, "Papirus", self.themes)

    def tearDown(self):
        self.tmp.cleanup()

    def _ids(self, query, status=None):
        rows = self.rows
        if status and status != "all":
            if status == "issues":
                rows = [r for r in rows if r.status != "ok"]
            else:
                rows = [r for r in rows if r.status == status]
        return [r.desktop_id for r in rows if matches_report(r, query)]

    def test_case_insensitive_english_and_chinese(self):
        self.assertTrue(matches_query("WECHAT", name="微信 WeChat", desktop_id="wechat", icon="wechat"))
        self.assertTrue(matches_query("wechat", name="微信 WeChat", desktop_id="wechat", icon="wechat"))
        self.assertTrue(matches_query("微信", name="微信 WeChat", desktop_id="wechat", icon="wechat"))
        ids = self._ids("WECHAT")
        self.assertEqual(ids, ["wechat"])
        self.assertEqual(self._ids("微信"), ["wechat"])

    def test_desktop_id_and_icon_and_metadata(self):
        self.assertEqual(self._ids("org.kde.dolphin"), ["org.kde.dolphin"])
        self.assertEqual(self._ids("ORG.KDE"), ["org.kde.dolphin"])
        self.assertIn("wechat", self._ids("Instant Messenger"))
        self.assertIn("org.kde.dolphin", self._ids("文件管理器"))
        self.assertIn("wechat", self._ids("Tencent"))
        # Icon= exact token
        self.assertEqual(self._ids("org.kde.dolphin"), ["org.kde.dolphin"])

    def test_combines_with_status_filter(self):
        # "Missing App" is missing; wechat is ok and must not appear under status=missing
        missing = self._ids("app", status="missing")
        self.assertEqual(missing, ["gone"])
        self.assertNotIn("wechat", missing)
        ok_wechat = self._ids("微信", status="ok")
        self.assertEqual(ok_wechat, ["wechat"])
        self.assertEqual(self._ids("微信", status="missing"), [])

    def test_empty_query_keeps_all(self):
        self.assertEqual(len(filter_items(self.apps, "", matches_app)), len(self.apps))
        self.assertEqual(len(filter_items(self.apps, "   ", matches_app)), len(self.apps))

    def test_no_match(self):
        self.assertEqual(self._ids("definitely-not-an-app"), [])
