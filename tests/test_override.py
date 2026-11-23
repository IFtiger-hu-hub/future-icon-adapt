import tempfile
import unittest
from pathlib import Path

from iconadapt.override import apply_overrides, rollback_overrides, uninstall_tool_state, OverrideError
from iconadapt.persist import atomic_write_json
from iconadapt.constants import OWNED_MARKER, TOOL_ID
from tests.fixtures import make_desktop, write


class OverrideTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state = self.root / "state"
        self.state.mkdir()
        atomic_write_json(self.state / OWNED_MARKER, {"tool": TOOL_ID, "kind": "state"})
        self.sys = self.root / "sysapps"
        self.user = self.root / "userapps"
        self.sys.mkdir()
        make_desktop(self.sys, "foo.desktop", name="Foo", icon="old-icon", extra="Comment=keep-me")

    def tearDown(self):
        self.tmp.cleanup()

    def test_apply_creates_and_rollback_removes(self):
        item = {
            "desktop_id": "foo",
            "source": str(self.sys / "foo.desktop"),
            "target": str(self.user / "foo.desktop"),
            "new_icon": "new-icon",
        }
        result = apply_overrides(self.state, self.user, [item])
        self.assertTrue(result["results"][0]["ok"])
        text = (self.user / "foo.desktop").read_text(encoding="utf-8")
        self.assertIn("Icon=new-icon", text)
        self.assertIn("Comment=keep-me", text)
        self.assertNotIn("Icon=old-icon", text)
        rb = rollback_overrides(self.state, ["foo"])
        self.assertTrue(rb["results"][0]["ok"])
        self.assertEqual(rb["results"][0]["action"], "removed-overlay")
        self.assertFalse((self.user / "foo.desktop").exists())

    def test_apply_backs_up_existing_user_file(self):
        make_desktop(self.user, "foo.desktop", name="User Foo", icon="user-icon", extra="X-User=1")
        item = {
            "desktop_id": "foo",
            "source": str(self.sys / "foo.desktop"),
            "target": str(self.user / "foo.desktop"),
            "new_icon": "new-icon",
        }
        apply_overrides(self.state, self.user, [item])
        text = (self.user / "foo.desktop").read_text(encoding="utf-8")
        self.assertIn("Icon=new-icon", text)
        # existing user keys preserved because we rewrite from backup of user file
        self.assertIn("X-User=1", text)
        rollback_overrides(self.state, ["foo"])
        restored = (self.user / "foo.desktop").read_text(encoding="utf-8")
        self.assertIn("Icon=user-icon", restored)
        self.assertIn("X-User=1", restored)

    def test_uninstall_state_refuses_unmarked(self):
        foreign = self.root / "foreign"
        foreign.mkdir()
        write(foreign / "notes.txt", "not ours")
        with self.assertRaises(OverrideError):
            uninstall_tool_state(foreign)
        # marked state can be removed
        msg = uninstall_tool_state(self.state)
        self.assertIn("已删除", msg)
        self.assertFalse(self.state.exists())


if __name__ == "__main__":
    unittest.main()
