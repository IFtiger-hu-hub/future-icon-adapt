import tempfile
import unittest
from pathlib import Path

from iconadapt.strategy import is_feishu_related, resolve_strategy
from iconadapt.theme import enumerate_themes
from tests.fixtures import make_theme


class StrategyTests(unittest.TestCase):
    def test_honest_when_tela_colloid_missing(self):
        tmp = tempfile.TemporaryDirectory()
        root = Path(tmp.name)
        make_theme(root, "Papirus", "breeze,hicolor", {"a": ["48x48/apps"]})
        make_theme(root, "breeze", "hicolor", {})
        make_theme(root, "hicolor", "", {})
        st = resolve_strategy(enumerate_themes([root]), current_theme="Papirus")
        self.assertIsNone(st.tela)
        self.assertIsNone(st.colloid)
        self.assertEqual(st.papirus, "Papirus")
        self.assertEqual(st.inherit, ["Papirus", "breeze", "hicolor"])
        self.assertIn("Tela", st.missing)
        tmp.cleanup()

    def test_prefers_dark_variant_when_current_is_dark(self):
        tmp = tempfile.TemporaryDirectory()
        root = Path(tmp.name)
        make_theme(root, "Tela", "hicolor", {})
        make_theme(root, "Tela-dark", "hicolor", {})
        make_theme(root, "Papirus", "breeze,hicolor", {})
        make_theme(root, "Papirus-Dark", "breeze,hicolor", {})
        make_theme(root, "breeze", "hicolor", {})
        make_theme(root, "breeze-dark", "breeze", {})
        make_theme(root, "hicolor", "", {})
        st = resolve_strategy(enumerate_themes([root]), current_theme="Papirus-Dark")
        self.assertEqual(st.papirus, "Papirus-Dark")
        self.assertEqual(st.tela, "Tela-dark")
        tmp.cleanup()

    def test_feishu_related(self):
        self.assertTrue(is_feishu_related("cn.feishu.Feishu"))
        self.assertTrue(is_feishu_related("feishu"))
        self.assertFalse(is_feishu_related("wechat"))


if __name__ == "__main__":
    unittest.main()
