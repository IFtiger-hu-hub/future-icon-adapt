import tempfile
import unittest
from pathlib import Path

from iconadapt.xdgpaths import kde_icon_theme_name
from tests.fixtures import write


class KdeThemeReadTests(unittest.TestCase):
    def test_reads_kdedefaults_when_user_kdeglobals_has_no_icons(self):
        tmp = tempfile.TemporaryDirectory()
        root = Path(tmp.name)
        write(root / "kdeglobals", "[General]\nName=user\n")
        write(root / "kdedefaults" / "kdeglobals", "[Icons]\nTheme=Papirus\n")
        self.assertEqual(kde_icon_theme_name(root), "Papirus")
        tmp.cleanup()

    def test_user_kdeglobals_wins_over_kdedefaults(self):
        tmp = tempfile.TemporaryDirectory()
        root = Path(tmp.name)
        write(root / "kdeglobals", "[Icons]\nTheme=breeze-dark\n")
        write(root / "kdedefaults" / "kdeglobals", "[Icons]\nTheme=Papirus\n")
        self.assertEqual(kde_icon_theme_name(root), "breeze-dark")
        tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
