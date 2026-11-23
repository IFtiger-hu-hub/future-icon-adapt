"""Single-app apply: one icon, no full export, shared-name isolation, rollback, UI state."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from iconadapt.applyone import (
    SingleAppError,
    apply_one,
    applied_status_for,
    has_override,
    install_name_for,
    merge_single_app_outputs,
    plan_apply_one,
    rollback_one,
)
from iconadapt.backend import IconAdaptBackend
from iconadapt.constants import (
    MASK_COMMON_SIZES,
    OWNED_MARKER,
    SINGLE_APP_ICON_PREFIX,
    THEME_INTERNAL_NAME,
    TOOL_ID,
    TOOL_VERSION,
)
from iconadapt.desktop import scan_applications
from iconadapt.export import export_theme
from iconadapt.mask import MaskSettings
from iconadapt.safeio import write_owned_marker
from iconadapt.strategy import resolve_strategy
from iconadapt.theme import enumerate_themes
from tests.fixtures import PNG_1x1, make_desktop, make_theme, write, write_bytes
from tests.qtutil import ensure_qt_app
from tests.jobutil import wait_theme_job

BLUE_SVG = """<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64" viewBox="0 0 64 64">
  <rect width="64" height="64" fill="#0000ff"/>
</svg>
"""
RED_SVG = """<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64" viewBox="0 0 64 64">
  <rect width="64" height="64" fill="#ff0000"/>
</svg>
"""


def _write_svg_pack(root: Path, name: str, icons: dict[str, str]) -> Path:
    tdir = root / name
    rel = "apps/scalable"
    (tdir / rel).mkdir(parents=True, exist_ok=True)
    write(
        tdir / "index.theme",
        "\n".join(
            [
                "[Icon Theme]",
                f"Name={name}",
                "Comment=test",
                "Inherits=hicolor",
                f"Directories={rel}",
                "",
                f"[{rel}]",
                "Size=64",
                "Type=Scalable",
                "MinSize=1",
                "MaxSize=512",
                "Context=Applications",
                "",
            ]
        ),
    )
    for icon_name, body in icons.items():
        (tdir / rel / f"{icon_name}.svg").write_text(body, encoding="utf-8")
    return tdir


def _write_owned_adapt(dest: Path, *, sentinel_name: str = "keep-other") -> Path:
    dest.mkdir(parents=True, exist_ok=True)
    dirs = ",".join(f"{s}x{s}/apps" for s in MASK_COMMON_SIZES)
    lines = [
        "[Icon Theme]",
        "Name=icon-adapt",
        "Comment=test",
        "Inherits=hicolor",
        f"Directories={dirs}",
        "",
    ]
    for s in MASK_COMMON_SIZES:
        rel = f"{s}x{s}/apps"
        lines += [f"[{rel}]", f"Size={s}", "Context=Applications", "Type=Fixed", ""]
        write_bytes(dest / rel / f"{sentinel_name}.png", PNG_1x1)
    write(dest / "index.theme", "\n".join(lines))
    write_owned_marker(dest, {"tool": TOOL_ID, "kind": "theme"})
    return dest


def _center_rgba(path: Path) -> tuple[int, int, int, int]:
    from PyQt6.QtGui import QImage

    img = QImage(str(path))
    if img.isNull():
        raise AssertionError(f"unreadable {path}")
    c = img.pixelColor(img.width() // 2, img.height() // 2)
    return c.red(), c.green(), c.blue(), c.alpha()


class ApplyOneCoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._app = ensure_qt_app()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="iconadapt-test-")
        self.root = Path(self.tmp.name)
        self.state = self.root / "state"
        self.state.mkdir()
        write_owned_marker(self.state, {"tool": TOOL_ID, "kind": "state"})
        self.icons = self.root / "icons"
        self.apps_dir = self.root / "apps"
        self.user_apps = self.root / "user-apps"
        self.apps_dir.mkdir()
        self.user_apps.mkdir()
        _write_svg_pack(self.icons, "Papirus", {"demo": BLUE_SVG, "shared": BLUE_SVG, "other": RED_SVG})
        make_theme(self.icons, "hicolor", "", {"folder": ["48x48/places"]})
        self.theme_dir = _write_owned_adapt(self.root / "installed" / THEME_INTERNAL_NAME)
        self.sentinel = {
            size: (self.theme_dir / f"{size}x{size}/apps/keep-other.png").read_bytes()
            for size in MASK_COMMON_SIZES
        }
        self.themes = enumerate_themes([self.icons, self.root / "installed"])
        make_desktop(self.apps_dir, "demo.desktop", name="Demo", icon="demo")
        make_desktop(self.apps_dir, "mate.desktop", name="Mate", icon="shared")
        make_desktop(self.apps_dir, "buddy.desktop", name="Buddy", icon="shared")
        make_desktop(self.apps_dir, "abs.desktop", name="Abs", icon=str(self.root / "logo.png"))
        write_bytes(self.root / "logo.png", PNG_1x1)
        self.apps = scan_applications([self.apps_dir], current_desktop="KDE")
        self.by_id = {a.desktop_id: a for a in self.apps}
        self.mask_on = MaskSettings(enabled=True, shape="circle", background="#ffffffff", padding=0)
        self.mask_off = MaskSettings(enabled=False)

    def tearDown(self):
        self.tmp.cleanup()

    def _sel(self, name: str, theme: str = "Papirus") -> dict:
        path = self.icons / theme / "apps" / "scalable" / f"{name}.svg"
        return {
            "desktop_id": name,
            "icon_name": name,
            "source_theme": theme,
            "source_path": str(path),
            "manual": True,
        }

    def _apply(self, desktop_id: str, sel: dict, mask, **kwargs):
        return apply_one(
            desktop_id=desktop_id,
            app=self.by_id[desktop_id],
            selection=sel,
            apps=self.apps,
            state_dir=self.state,
            theme_dir=self.theme_dir,
            current_theme=THEME_INTERNAL_NAME,
            mask=mask,
            themes=self.themes,
            target_apps_dir=self.user_apps,
            **kwargs,
        )

    def _assert_sentinel_untouched(self):
        for size, blob in self.sentinel.items():
            path = self.theme_dir / f"{size}x{size}/apps/keep-other.png"
            self.assertEqual(path.read_bytes(), blob, f"sentinel changed at {size}")

    def test_mask_on_writes_unique_icon_and_desktop(self):
        sel = self._sel("demo")
        result = self._apply("demo", sel, self.mask_on)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["status"], "applied")
        name = install_name_for("demo")
        self.assertEqual(result["install_name"], name)
        self.assertTrue(name.startswith(SINGLE_APP_ICON_PREFIX))
        png = self.theme_dir / "48x48/apps" / f"{name}.png"
        self.assertTrue(png.is_file())
        r, g, b, a = _center_rgba(png)
        self.assertGreater(b, 200)
        self.assertLess(r, 40)
        overlay = self.user_apps / "demo.desktop"
        self.assertTrue(overlay.is_file())
        self.assertIn(f"Icon={name}", overlay.read_text(encoding="utf-8"))
        self.assertNotIn("Icon=demo", overlay.read_text(encoding="utf-8"))
        self.assertEqual((self.apps_dir / "demo.desktop").read_text(encoding="utf-8").count("Icon=demo"), 1)
        self._assert_sentinel_untouched()
        # same-name Icon= that used to be a no-op now actually writes
        again = self._apply("demo", sel, self.mask_on)
        self.assertTrue(again["ok"], again)
        self.assertEqual(again["status"], "unchanged")
        self.assertIn("无需修改", again["message"])

    def test_mask_off_does_not_use_plate_and_still_writes(self):
        sel = self._sel("demo")
        result = self._apply("demo", sel, self.mask_off)
        self.assertEqual(result["status"], "applied", result)
        png = self.theme_dir / "48x48/apps" / f"{install_name_for('demo')}.png"
        self.assertTrue(png.is_file())
        r, g, b, a = _center_rgba(png)
        self.assertGreater(b, 200)
        self.assertLess(r, 40)
        self._assert_sentinel_untouched()

    def test_absolute_icon_rewrites_launcher_to_theme_name(self):
        sel = {
            "desktop_id": "abs",
            "icon_name": "logo",
            "source_theme": "Papirus",
            "source_path": str(self.icons / "Papirus/apps/scalable/demo.svg"),
            "manual": True,
        }
        self.assertTrue((self.by_id["abs"].icon or "").startswith("/"))
        result = self._apply("abs", sel, self.mask_on)
        self.assertEqual(result["status"], "applied", result)
        name = install_name_for("abs")
        text = (self.user_apps / "abs.desktop").read_text(encoding="utf-8")
        self.assertIn(f"Icon={name}", text)
        self.assertNotIn(str(self.root / "logo.png"), text.split("Icon=", 1)[-1].splitlines()[0])
        self.assertTrue((self.theme_dir / "48x48/apps" / f"{name}.png").is_file())

    def test_shared_icon_name_does_not_change_the_other_app(self):
        sel = {
            "desktop_id": "mate",
            "icon_name": "shared",
            "source_theme": "Papirus",
            "source_path": str(self.icons / "Papirus/apps/scalable/shared.svg"),
            "manual": True,
        }
        # a pre-existing file at the shared name must stay
        shared_png = self.theme_dir / "48x48/apps/shared.png"
        write_bytes(shared_png, PNG_1x1)
        before = shared_png.read_bytes()
        result = self._apply("mate", sel, self.mask_on)
        self.assertEqual(result["status"], "applied", result)
        self.assertIn("buddy", result["shared"])
        name = install_name_for("mate")
        self.assertNotEqual(name, "shared")
        self.assertTrue((self.theme_dir / "48x48/apps" / f"{name}.png").is_file())
        self.assertEqual(shared_png.read_bytes(), before)
        self.assertFalse((self.user_apps / "buddy.desktop").exists())
        mate = (self.user_apps / "mate.desktop").read_text(encoding="utf-8")
        self.assertIn(f"Icon={name}", mate)
        buddy_sys = (self.apps_dir / "buddy.desktop").read_text(encoding="utf-8")
        self.assertIn("Icon=shared", buddy_sys)
        self._assert_sentinel_untouched()

    def test_failure_rolls_back_theme_and_skips_desktop(self):
        sel = self._sel("demo")

        def boom(*_args, **_kwargs):
            raise SingleAppError("模拟启动器写入失败")

        result = self._apply("demo", sel, self.mask_on, apply_desktop=boom)
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["status"], "failed")
        name = install_name_for("demo")
        self.assertFalse((self.theme_dir / "48x48/apps" / f"{name}.png").exists())
        self.assertFalse((self.user_apps / "demo.desktop").exists())
        self._assert_sentinel_untouched()

    def test_refuses_missing_theme_and_wrong_current_theme_without_writes(self):
        sel = self._sel("demo")
        missing = plan_apply_one(
            desktop_id="demo",
            app=self.by_id["demo"],
            selection=sel,
            apps=self.apps,
            state_dir=self.state,
            theme_dir=self.root / "no-such-theme",
            current_theme=THEME_INTERNAL_NAME,
            mask=self.mask_on,
            themes=self.themes,
            target_apps_dir=self.user_apps,
        )
        self.assertFalse(missing["ok"])
        self.assertIn("还没有安装", missing["error"])
        self.assertIn("不会自动切换", missing["error"])

        wrong = plan_apply_one(
            desktop_id="demo",
            app=self.by_id["demo"],
            selection=sel,
            apps=self.apps,
            state_dir=self.state,
            theme_dir=self.theme_dir,
            current_theme="Papirus",
            mask=self.mask_on,
            themes=self.themes,
            target_apps_dir=self.user_apps,
        )
        self.assertFalse(wrong["ok"])
        self.assertIn("Papirus", wrong["error"])
        self.assertIn("不会自动切换", wrong["error"])
        applied = apply_one(
            desktop_id="demo",
            app=self.by_id["demo"],
            selection=sel,
            apps=self.apps,
            state_dir=self.state,
            theme_dir=self.theme_dir,
            current_theme="Papirus",
            mask=self.mask_on,
            themes=self.themes,
            target_apps_dir=self.user_apps,
        )
        self.assertFalse(applied["ok"])
        self.assertFalse((self.user_apps / "demo.desktop").exists())
        self.assertFalse((self.theme_dir / "48x48/apps" / f"{install_name_for('demo')}.png").exists())
        self._assert_sentinel_untouched()

    def test_rollback_restores_desktop_and_removes_unique_files(self):
        sel = self._sel("demo")
        self.assertEqual(self._apply("demo", sel, self.mask_on)["status"], "applied")
        name = install_name_for("demo")
        rb = rollback_one(desktop_id="demo", state_dir=self.state, theme_dir=self.theme_dir)
        self.assertTrue(rb["ok"], rb)
        self.assertFalse((self.user_apps / "demo.desktop").exists())
        self.assertFalse((self.theme_dir / "48x48/apps" / f"{name}.png").exists())
        self._assert_sentinel_untouched()


class ApplyOneBackendAndUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._app = ensure_qt_app()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="iconadapt-test-")
        self.root = Path(self.tmp.name)
        self.data = self.root / "data"
        self.config = self.root / "config"
        self.icons = self.root / "icons"
        self.apps_sys = self.root / "apps-sys"
        self.apps_sys.mkdir()
        _write_svg_pack(self.icons, "Papirus", {"demo": BLUE_SVG})
        make_theme(self.icons, "hicolor", "", {"folder": ["48x48/places"]})
        make_desktop(self.apps_sys, "demo.desktop", name="Demo", icon="demo")
        make_desktop(self.apps_sys, "twin.desktop", name="Twin", icon="demo")
        self.iso = {
            "data_home": str(self.data),
            "config_home": str(self.config),
            "icon_dirs": [str(self.icons)],
            "app_dirs": [str(self.apps_sys)],
            "current_theme": THEME_INTERNAL_NAME,
            "current_desktop": "KDE",
            "cache_home": str(self.root / "cache"),
        }

    def tearDown(self):
        self.tmp.cleanup()

    def _backend_with_theme(self, current_theme=THEME_INTERNAL_NAME) -> IconAdaptBackend:
        iso = dict(self.iso)
        iso["current_theme"] = current_theme
        dest = self.data / "icons" / THEME_INTERNAL_NAME
        _write_owned_adapt(dest)
        iso["icon_dirs"] = [str(self.icons), str(self.data / "icons")]
        return IconAdaptBackend(isolated=iso)

    def test_save_selection_does_not_write_theme_or_desktop(self):
        be = self._backend_with_theme()
        path = str(self.icons / "Papirus/apps/scalable/demo.svg")
        be.saveSelection("demo", "demo", "Papirus", path)
        dest = self.data / "icons" / THEME_INTERNAL_NAME
        unique = dest / "48x48/apps" / f"{install_name_for('demo')}.png"
        self.assertFalse(unique.exists())
        self.assertFalse((self.data / "applications" / "demo.desktop").exists())
        apps = json.loads(be.appsJson)
        demo = next(a for a in apps if a["desktop_id"] == "demo")
        self.assertEqual(demo["applied_status"], "pending")
        self.assertTrue(demo["has_selection"])
        detail = json.loads(be.appDetailJson("demo"))
        self.assertEqual(detail["applied_status"], "pending")

    def test_apply_to_app_feedback_and_does_not_full_export(self):
        be = self._backend_with_theme()
        path = str(self.icons / "Papirus/apps/scalable/demo.svg")
        be.saveSelection("demo", "demo", "Papirus", path)
        dest = self.data / "icons" / THEME_INTERNAL_NAME
        # plant a unique foreign file that a full export would not keep if it rebuilt; we just ensure it remains
        stray = dest / "48x48/apps" / "keep-other.png"
        before = stray.read_bytes()
        plan = json.loads(be.planApplyToApp("demo"))
        self.assertTrue(plan["ok"], plan)
        self.assertIn("不会重新生成整套主题", plan["summary"])
        result = json.loads(be.applyToApp("demo"))
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["status"], "applied")
        self.assertIn("已把", result["message"])
        self.assertIn("这个应用", result["message"])
        self.assertTrue(result.get("refresh"))
        self.assertEqual(stray.read_bytes(), before)
        detail = json.loads(be.appDetailJson("demo"))
        self.assertEqual(detail["applied_status"], "applied")
        # same selection again is unchanged, with explicit message
        again = json.loads(be.applyToApp("demo"))
        self.assertEqual(again["status"], "unchanged", again)
        self.assertIn("无需修改", again["message"])
        # twin still has the shared original name and no overlay
        self.assertFalse((self.data / "applications" / "twin.desktop").exists())

    def test_apply_refuses_when_current_theme_is_not_adapt(self):
        be = self._backend_with_theme(current_theme="Papirus")
        path = str(self.icons / "Papirus/apps/scalable/demo.svg")
        be.saveSelection("demo", "demo", "Papirus", path)
        result = json.loads(be.applyToApp("demo"))
        self.assertFalse(result["ok"])
        self.assertIn("不会自动切换", result["error"])
        self.assertFalse((self.data / "applications" / "demo.desktop").exists())

    def test_full_install_preserves_single_app_outputs(self):
        be = self._backend_with_theme()
        path = str(self.icons / "Papirus/apps/scalable/demo.svg")
        be.saveSelection("demo", "demo", "Papirus", path)
        self.assertEqual(json.loads(be.applyToApp("demo"))["status"], "applied")
        dest = self.data / "icons" / THEME_INTERNAL_NAME
        unique = dest / "48x48/apps" / f"{install_name_for('demo')}.png"
        self.assertTrue(unique.is_file())
        blob = unique.read_bytes()
        inst = wait_theme_job(be, "installTheme")
        self.assertTrue(inst["ok"], inst)
        self.assertTrue(unique.is_file(), "full install must not drop single-app icons")
        self.assertEqual(unique.read_bytes(), blob)

    def test_restore_keeps_selection_and_has_override_is_independent(self):
        be = self._backend_with_theme()
        path = str(self.icons / "Papirus/apps/scalable/demo.svg")
        be.saveSelection("demo", "demo", "Papirus", path)
        apps = json.loads(be.appsJson)
        demo = next(a for a in apps if a["desktop_id"] == "demo")
        self.assertFalse(demo["has_override"])
        self.assertEqual(demo["applied_status"], "pending")
        self.assertEqual(json.loads(be.applyToApp("demo"))["status"], "applied")
        demo = next(a for a in json.loads(be.appsJson) if a["desktop_id"] == "demo")
        self.assertTrue(demo["has_override"])
        self.assertEqual(demo["applied_status"], "applied")
        # clear candidate: override remains
        self.assertEqual(be.clearSelection("demo"), "ok")
        demo = next(a for a in json.loads(be.appsJson) if a["desktop_id"] == "demo")
        self.assertTrue(demo["has_override"])
        self.assertEqual(demo["applied_status"], "applied_changed")
        # put selection back, then restore: selection stays, override gone
        be.saveSelection("demo", "demo", "Papirus", path)
        rb = json.loads(be.rollbackApplyToApp("demo"))
        self.assertTrue(rb["ok"], rb)
        self.assertTrue(rb.get("selection_kept"))
        self.assertIn("候选仍保留", rb["message"])
        state = Path(self.data) / "icon-adapt"
        from iconadapt.persist import load_choices
        store = load_choices(state)
        self.assertIn("demo", store.get("selections") or {})
        demo = next(a for a in json.loads(be.appsJson) if a["desktop_id"] == "demo")
        self.assertFalse(demo["has_override"])
        self.assertEqual(demo["applied_status"], "pending")

    def test_qml_distinguishes_pending_and_applied_and_does_not_auto_apply(self):
        qml = (Path(__file__).resolve().parents[1] / "qml" / "Main.qml").read_text(encoding="utf-8")
        self.assertIn("已选择，待应用", qml)
        self.assertIn("已应用", qml)
        self.assertIn("已应用（选择已改）", qml)
        self.assertIn("应用到这个应用", qml)
        self.assertIn("恢复原图标", qml)
        self.assertIn("confirmRestore", qml)
        self.assertIn("restoreDialog", qml)
        self.assertIn("ignoreNextSelect", qml)
        self.assertIn("modelData.has_override", qml)
        self.assertIn("点选只保存在本工具里，不会立刻改菜单", qml)
        self.assertIn("applyToApp", qml)
        self.assertIn("chooseIcon", qml)
        # candidate click still only saves a selection
        self.assertIn("iconAdapt.saveSelection(root.selectedId, iconName || iconNameForSelection(), theme, path)", qml)
        self.assertNotIn("applyToApp(root.selectedId)", qml.split("function chooseIcon")[1].split("function ")[0])
        self.assertNotIn("已选用", qml)
        self.assertNotIn("撤销应用", qml)


if __name__ == "__main__":
    unittest.main()
