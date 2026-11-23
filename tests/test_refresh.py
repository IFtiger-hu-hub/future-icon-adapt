import tempfile
import unittest
from pathlib import Path

from iconadapt.constants import THEME_INTERNAL_NAME
from iconadapt.refresh import (
    ICON_CACHE_BASENAME,
    KICONLOADER_GROUPS,
    RefreshError,
    RefreshHooks,
    bounce_theme,
    clear_icon_shared_cache,
    icon_cache_path,
    refresh_desktop_icons,
    safe_theme_name,
    should_bounce,
    isolated_hooks,
)
from tests.fixtures import write


class CacheSafetyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.cache = self.root / "cache"
        self.cache.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def test_clears_only_icon_cache_file(self):
        target = self.cache / ICON_CACHE_BASENAME
        other = self.cache / "plasma_theme_default.kcache"
        foreign = self.cache / "not-icon-cache.kcache"
        write(target, "icon")
        write(other, "plasma")
        write(foreign, "nope")
        result = clear_icon_shared_cache(self.cache)
        self.assertTrue(result["removed"])
        self.assertEqual(Path(result["path"]), target)
        self.assertFalse(target.exists())
        self.assertTrue(other.is_file())
        self.assertTrue(foreign.is_file())

    def test_missing_cache_is_ok(self):
        result = clear_icon_shared_cache(self.cache)
        self.assertFalse(result["removed"])
        self.assertIn("not present", result["reason"])

    def test_refuses_wrong_basename_and_directory(self):
        other = self.cache / "plasma_theme_default.kcache"
        write(other, "x")
        with self.assertRaises(RefreshError):
            clear_icon_shared_cache(path=other)
        self.assertTrue(other.is_file())
        directory = self.cache / ICON_CACHE_BASENAME
        directory.mkdir()
        with self.assertRaises(RefreshError):
            clear_icon_shared_cache(path=directory)
        self.assertTrue(directory.is_dir())

    def test_icon_cache_path_uses_generic_cache_home(self):
        self.assertEqual(icon_cache_path(self.cache), self.cache / "icon-cache.kcache")

    def test_safe_theme_name_rejects_flags_and_paths(self):
        with self.assertRaises(RefreshError):
            safe_theme_name("--help")
        with self.assertRaises(RefreshError):
            safe_theme_name("/usr/share/icons/Papirus")
        with self.assertRaises(RefreshError):
            safe_theme_name("Papirus,breeze")
        self.assertEqual(safe_theme_name("icon-adapt"), "icon-adapt")


class BouncePolicyTests(unittest.TestCase):
    def test_does_not_bounce_when_current_is_not_adapt(self):
        ok, why = should_bounce("Papirus", adapt_dir_exists=True)
        self.assertFalse(ok)
        self.assertIn("not the tool theme", why)

        # refresh() must not call change_icons
        called = []
        hooks = RefreshHooks(
            emit_icon_changed=lambda g: None,
            run_kbuildsycoca=lambda: None,
            change_icons=lambda name: called.append(name),
            theme_dir_exists=lambda name: name in {"Papirus", "breeze", THEME_INTERNAL_NAME},
        )
        result = refresh_desktop_icons(
            current_theme="Papirus",
            theme_dir=None,
            hooks=hooks,
            allow_theme_bounce=True,
            icon_dirs=[],
        )
        self.assertFalse(result.bounced)
        self.assertEqual(called, [])
        self.assertIn("not the tool theme", result.skipped_bounce_reason)

    def test_skips_bounce_after_uninstall_if_dir_gone(self):
        ok, why = should_bounce(THEME_INTERNAL_NAME, adapt_dir_exists=False, just_uninstalled=True)
        self.assertFalse(ok)
        self.assertIn("gone", why)

    def test_bounce_failure_restores_original(self):
        log = []

        def change(name: str) -> None:
            log.append(name)
            if name == THEME_INTERNAL_NAME and log.count(THEME_INTERNAL_NAME) == 0:
                pass
            if name == THEME_INTERNAL_NAME and "Papirus" in log:
                # first restore attempt fails, later succeeds? we'll fail all then see
                raise RuntimeError("restore failed")

        # fail restore always
        with self.assertRaises(RefreshError) as ctx:
            bounce_theme(
                THEME_INTERNAL_NAME,
                change_icons=change,
                theme_exists=lambda n: n in {"Papirus", "breeze", THEME_INTERNAL_NAME},
            )
        self.assertIn("未能切回", str(ctx.exception))
        self.assertEqual(log[0], "Papirus")
        self.assertGreaterEqual(log.count(THEME_INTERNAL_NAME), 3)
        self.assertNotEqual(log[-1], "Papirus")  # last attempts are restore

    def test_bounce_restores_if_switch_away_fails_after_partial(self):
        log = []

        def change(name: str) -> None:
            log.append(name)
            if name == "Papirus":
                raise RuntimeError("cannot switch away")

        result = bounce_theme(
            THEME_INTERNAL_NAME,
            change_icons=change,
            theme_exists=lambda n: n in {"Papirus", THEME_INTERNAL_NAME},
        )
        self.assertFalse(result["bounced"])
        self.assertEqual(result["restored"], THEME_INTERNAL_NAME)
        self.assertIn(THEME_INTERNAL_NAME, log)

    def test_successful_bounce_returns_to_origin(self):
        log = []
        result = bounce_theme(
            THEME_INTERNAL_NAME,
            change_icons=log.append,
            theme_exists=lambda n: n in {"Papirus", THEME_INTERNAL_NAME},
        )
        self.assertTrue(result["bounced"])
        self.assertEqual(result["via"], "Papirus")
        self.assertEqual(result["restored"], THEME_INTERNAL_NAME)
        self.assertEqual(log, ["Papirus", THEME_INTERNAL_NAME])


class RefreshSequenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.cache = self.root / "cache"
        self.cache.mkdir()
        write(self.cache / ICON_CACHE_BASENAME, "cached")
        write(self.cache / "plasma_theme_default.kcache", "keep")
        self.theme = self.root / "icons" / THEME_INTERNAL_NAME
        write(self.theme / "index.theme", "[Icon Theme]\nName=Icon Adapt\nDirectories=\n")
        self.signals = []
        self.sycoca = 0
        self.changes = []
        self.hooks = RefreshHooks(
            emit_icon_changed=self.signals.append,
            run_kbuildsycoca=self._sycoca,
            change_icons=self.changes.append,
            update_theme_cache=lambda p: setattr(self, "cached_dir", p),
            theme_dir_exists=lambda n: n in {"Papirus", "breeze", THEME_INTERNAL_NAME},
        )

    def _sycoca(self):
        self.sycoca += 1

    def tearDown(self):
        self.tmp.cleanup()

    def test_primary_refresh_is_called_and_does_not_delete_other_caches(self):
        before = (self.theme / "index.theme").stat().st_mtime
        result = refresh_desktop_icons(
            current_theme="Papirus",
            theme_dir=self.theme,
            cache_home=self.cache,
            hooks=self.hooks,
            allow_theme_bounce=True,
        )
        self.assertTrue(result.cache_cleared)
        self.assertFalse((self.cache / ICON_CACHE_BASENAME).exists())
        self.assertTrue((self.cache / "plasma_theme_default.kcache").is_file())
        self.assertEqual(result.signals, list(KICONLOADER_GROUPS))
        self.assertTrue(result.sycoca)
        self.assertEqual(self.sycoca, 1)
        self.assertTrue(result.index_touched)
        self.assertGreaterEqual((self.theme / "index.theme").stat().st_mtime, before)
        self.assertFalse(result.bounced)
        self.assertEqual(self.changes, [])

    def test_bounce_when_current_is_adapt_and_dir_exists(self):
        result = refresh_desktop_icons(
            current_theme=THEME_INTERNAL_NAME,
            theme_dir=self.theme,
            cache_home=self.cache,
            hooks=self.hooks,
            allow_theme_bounce=True,
        )
        self.assertTrue(result.bounced)
        self.assertEqual(self.changes, ["Papirus", THEME_INTERNAL_NAME])
        self.assertEqual(result.restored_theme, THEME_INTERNAL_NAME)

    def test_isolated_hooks_do_not_provide_change_icons(self):
        log = {}
        hooks = isolated_hooks(log)
        self.assertIsNone(hooks.change_icons)
        result = refresh_desktop_icons(
            current_theme=THEME_INTERNAL_NAME,
            theme_dir=self.theme,
            cache_home=self.cache,
            hooks=hooks,
            allow_theme_bounce=True,
        )
        self.assertFalse(result.bounced)
        self.assertTrue(log["signals"])
        self.assertEqual(log["sycoca"], 1)
        self.assertEqual(log["change_icons"], [])


if __name__ == "__main__":
    unittest.main()
