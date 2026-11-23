import tempfile
import unittest
from pathlib import Path

from iconadapt.desktop import scan_applications
from tests.fixtures import isolated_env, make_desktop, write


class DesktopScanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.env = isolated_env(self.root)
        self.user = self.env["data_home"] / "applications"
        self.sys = self.root / "sys" / "applications"
        self.sys.mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_user_overrides_system_same_id(self):
        make_desktop(self.sys, "foo.desktop", name="System Foo", icon="foo")
        make_desktop(self.user, "foo.desktop", name="User Foo", icon="foo-user")
        apps = scan_applications([self.user, self.sys], current_desktop="KDE")
        self.assertEqual(len(apps), 1)
        self.assertEqual(apps[0].name, "User Foo")
        self.assertEqual(apps[0].icon, "foo-user")
        self.assertEqual(apps[0].desktop_id, "foo")

    def test_hidden_and_nodisplay_excluded(self):
        make_desktop(self.user, "a.desktop", name="A", icon="a")
        make_desktop(self.user, "b.desktop", name="B", icon="b", extra="Hidden=true")
        make_desktop(self.user, "c.desktop", name="C", icon="c", extra="NoDisplay=true")
        apps = scan_applications([self.user], current_desktop="KDE")
        ids = {a.desktop_id for a in apps}
        self.assertEqual(ids, {"a"})

    def test_only_show_in_kde(self):
        make_desktop(self.user, "g.desktop", name="G", icon="g", extra="OnlyShowIn=GNOME;")
        make_desktop(self.user, "k.desktop", name="K", icon="k", extra="OnlyShowIn=KDE;GNOME;")
        apps = scan_applications([self.user], current_desktop="KDE")
        self.assertEqual([a.desktop_id for a in apps], ["k"])

    def test_not_show_in_kde(self):
        make_desktop(self.user, "x.desktop", name="X", icon="x", extra="NotShowIn=KDE;")
        apps = scan_applications([self.user], current_desktop="KDE")
        self.assertEqual(apps, [])

    def test_nested_desktop_id(self):
        make_desktop(self.user / "wine", "chat.desktop", name="Chat", icon="chat")
        # write into nested: make_desktop uses dir/filename
        apps = scan_applications([self.user], current_desktop="KDE")
        self.assertEqual(apps[0].desktop_id, "wine-chat")

    def test_absolute_icon_kind(self):
        make_desktop(self.user, "abs.desktop", name="Abs", icon="/opt/app/icon.svg")
        apps = scan_applications([self.user], current_desktop="KDE")
        self.assertEqual(apps[0].icon_kind, "absolute")

    def test_type_link_not_launchable(self):
        make_desktop(self.user, "l.desktop", name="L", icon="l", typ="Link")
        apps = scan_applications([self.user], current_desktop="KDE")
        self.assertEqual(apps, [])
        all_apps = scan_applications([self.user], current_desktop="KDE", include_non_launchable=True)
        self.assertEqual(all_apps[0].launchable, False)


if __name__ == "__main__":
    unittest.main()
