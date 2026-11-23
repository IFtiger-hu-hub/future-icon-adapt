import tempfile
import unittest
from pathlib import Path

from iconadapt.persist import (
    load_choices,
    merge_keeping_manual,
    remove_selection,
    save_choices,
    upsert_selection,
)


class PersistTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name) / "state"

    def tearDown(self):
        self.tmp.cleanup()

    def test_roundtrip_and_keep_manual_on_merge(self):
        store = load_choices(self.state)
        upsert_selection(store, "app.one", icon_name="one", source_theme="Tela", source_path="/t/one.png", manual=True)
        save_choices(self.state, store)
        loaded = load_choices(self.state)
        self.assertTrue(loaded["selections"]["app.one"]["manual"])

        incoming = {
            "selections": {
                "app.one": {
                    "desktop_id": "app.one",
                    "icon_name": "auto",
                    "source_theme": "Papirus",
                    "source_path": "/p/auto.png",
                    "manual": False,
                },
                "app.two": {
                    "desktop_id": "app.two",
                    "icon_name": "two",
                    "source_theme": "Papirus",
                    "source_path": "/p/two.png",
                    "manual": False,
                },
            }
        }
        merged = merge_keeping_manual(loaded, incoming)
        self.assertEqual(merged["selections"]["app.one"]["icon_name"], "one")
        self.assertTrue(merged["selections"]["app.one"]["manual"])
        self.assertEqual(merged["selections"]["app.two"]["icon_name"], "two")

    def test_remove(self):
        store = load_choices(self.state)
        upsert_selection(store, "x", icon_name="x", source_theme="Tela", source_path="/x")
        self.assertTrue(remove_selection(store, "x"))
        self.assertFalse(remove_selection(store, "x"))


if __name__ == "__main__":
    unittest.main()
