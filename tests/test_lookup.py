import tempfile
import unittest
from pathlib import Path

from iconadapt.lookup import lookup_icon, lookup_in_theme_files, collect_exact_candidates
from iconadapt.theme import enumerate_themes, inherit_chain
from tests.fixtures import make_theme


class InheritAndLookupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "icons"
        self.root.mkdir()
        # A inherits B, B inherits A (cycle) plus C
        make_theme(self.root, "ThemeA", "ThemeB,hicolor", {"only-a": ["48x48/apps"]})
        make_theme(self.root, "ThemeB", "ThemeA,hicolor", {"only-b": ["48x48/apps"]})
        make_theme(self.root, "hicolor", "", {"generic": ["48x48/apps"]})
        make_theme(self.root, "breeze", "hicolor", {"breeze-only": ["48x48/apps"], "folder": ["48x48/apps"]})
        make_theme(self.root, "Papirus", "breeze,hicolor", {"pap": ["48x48/apps", "scalable/apps"]})
        self.themes = enumerate_themes([self.root])

    def tearDown(self):
        self.tmp.cleanup()

    def test_cycle_does_not_loop_forever(self):
        chain, warnings = inherit_chain("ThemeA", self.themes)
        self.assertIn("ThemeA", chain)
        self.assertIn("ThemeB", chain)
        self.assertTrue(any("cycle" in w for w in warnings))
        # terminates and can look up both
        ra = lookup_icon("only-a", "ThemeA", self.themes, kde_breeze_extra=False)
        rb = lookup_icon("only-b", "ThemeA", self.themes, kde_breeze_extra=False)
        self.assertTrue(ra.found)
        self.assertEqual(ra.hit.theme, "ThemeA")
        self.assertTrue(ra.hit.in_theme_proper)
        self.assertTrue(rb.found)
        self.assertEqual(rb.hit.theme, "ThemeB")
        self.assertFalse(rb.hit.in_theme_proper)

    def test_fallback_vs_current(self):
        r = lookup_icon("pap", "Papirus", self.themes, kde_breeze_extra=False)
        self.assertEqual(r.status, "in_current")
        r2 = lookup_icon("breeze-only", "Papirus", self.themes, kde_breeze_extra=False)
        self.assertEqual(r2.status, "fallback")
        self.assertEqual(r2.hit.theme, "breeze")
        r3 = lookup_icon("generic", "Papirus", self.themes, kde_breeze_extra=False)
        self.assertEqual(r3.status, "fallback")
        self.assertEqual(r3.hit.theme, "hicolor")

    def test_missing(self):
        r = lookup_icon("no-such-icon", "Papirus", self.themes, kde_breeze_extra=False)
        self.assertFalse(r.found)
        self.assertEqual(r.status, "missing")

    def test_extra_breeze_fallback(self):
        make_theme(self.root, "Custom", "hicolor", {"custom-a": ["48x48/apps"]})
        themes = enumerate_themes([self.root])
        r = lookup_icon("breeze-only", "Custom", themes, kde_breeze_extra=True)
        self.assertTrue(r.found)
        self.assertEqual(r.status, "extra_fallback")
        self.assertEqual(r.hit.theme, "breeze")

    def test_candidates_are_per_theme_exact(self):
        cands = collect_exact_candidates("folder", self.themes)
        names = {c.theme for c in cands}
        self.assertIn("breeze", names)
        self.assertNotIn("Papirus", names)  # Papirus fixture has no folder

    def test_inherited_theme_missing_warns(self):
        make_theme(self.root, "Orphan", "NotInstalled,hicolor", {"x": ["48x48/apps"]})
        themes = enumerate_themes([self.root])
        chain, warnings = inherit_chain("Orphan", themes)
        self.assertTrue(any("NotInstalled" in w for w in warnings))
        self.assertIn("hicolor", chain)


if __name__ == "__main__":
    unittest.main()
