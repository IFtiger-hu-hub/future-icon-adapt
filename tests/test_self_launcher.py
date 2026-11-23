"""This tool's own menu launcher: install, refuse foreign files, remove only ours."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from iconadapt.constants import (
    LAUNCHER_DESKTOP_FILENAME,
    LAUNCHER_DESKTOP_ID,
    LAUNCHER_ICON_NAME,
    LAUNCHER_MENU_NAME,
    LAUNCHER_OWNED_KEY,
    TOOL_ID,
    TOOL_VERSION,
)
from iconadapt.selflaunch import (
    SelfLaunchError,
    app_script,
    install_self_launcher,
    is_owned_launcher,
    launcher_body,
    launcher_status,
    project_root,
    remove_self_launcher,
)
from iconadapt.safeio import write_owned_marker
from tests.fixtures import write


class SelfLauncherTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="iconadapt-test-")
        self.root = Path(self.tmp.name)
        self.apps = self.root / "applications"
        self.state = self.root / "state"
        self.apps.mkdir()
        self.state.mkdir()
        write_owned_marker(self.state, {"tool": TOOL_ID, "kind": "state"})
        self.sycoca = 0

    def tearDown(self):
        self.tmp.cleanup()

    def _sycoca(self):
        self.sycoca += 1

    def test_body_has_required_fields(self):
        script = app_script()
        body = launcher_body()
        self.assertIn("Name=" + LAUNCHER_MENU_NAME, body)
        self.assertIn("Exec=/usr/bin/python3 " + str(script), body)
        self.assertIn("TryExec=/usr/bin/python3", body)
        self.assertIn("Path=" + str(project_root()), body)
        self.assertIn("Icon=" + LAUNCHER_ICON_NAME, body)
        self.assertIn("Terminal=false", body)
        self.assertIn("Categories=Settings;DesktopSettings;", body)
        self.assertIn(f"StartupWMClass={LAUNCHER_DESKTOP_ID}", body)
        self.assertIn(f"{LAUNCHER_OWNED_KEY}={TOOL_ID}", body)
        self.assertTrue(script.is_file())
        self.assertTrue((project_root() / "app.py").is_file())

    def test_install_and_remove_own_launcher(self):
        result = install_self_launcher(
            applications_dir=self.apps,
            state_dir=self.state,
            run_sycoca=self._sycoca,
        )
        self.assertTrue(result["ok"], result)
        target = self.apps / LAUNCHER_DESKTOP_FILENAME
        self.assertTrue(target.is_file())
        self.assertEqual(target.stat().st_mode & 0o777, 0o644)
        text = target.read_text(encoding="utf-8")
        self.assertTrue(is_owned_launcher(target))
        self.assertIn("Name=" + LAUNCHER_MENU_NAME, text)
        self.assertEqual(self.sycoca, 1)
        st = launcher_status(applications_dir=self.apps, state_dir=self.state)
        self.assertTrue(st["installed"])
        self.assertFalse(st["foreign"])
        # reinstall owned file: backup, no error
        again = install_self_launcher(
            applications_dir=self.apps,
            state_dir=self.state,
            run_sycoca=self._sycoca,
        )
        self.assertTrue(again["ok"], again)
        self.assertEqual(again["status"], "updated")
        self.assertTrue(Path(again["backup"]).is_file())
        self.assertTrue(target.is_file())
        rm = remove_self_launcher(
            applications_dir=self.apps,
            state_dir=self.state,
            run_sycoca=self._sycoca,
        )
        self.assertTrue(rm["ok"], rm)
        self.assertFalse(target.exists())
        self.assertEqual(self.sycoca, 3)

    def test_refuses_foreign_desktop_and_does_not_delete_it(self):
        target = self.apps / LAUNCHER_DESKTOP_FILENAME
        write(target, "[Desktop Entry]\nType=Application\nName=Foreign\nExec=true\n")
        before = target.read_text(encoding="utf-8")
        with self.assertRaises(SelfLaunchError) as ctx:
            install_self_launcher(applications_dir=self.apps, state_dir=self.state, run_sycoca=self._sycoca)
        self.assertIn("不属于本工具", str(ctx.exception))
        self.assertEqual(target.read_text(encoding="utf-8"), before)
        with self.assertRaises(SelfLaunchError):
            remove_self_launcher(applications_dir=self.apps, state_dir=self.state, run_sycoca=self._sycoca)
        self.assertTrue(target.is_file())
        self.assertEqual(self.sycoca, 0)

    def test_abs_path_script_imports_from_other_cwd(self):
        import subprocess, os
        env = os.environ.copy()
        env["QT_QPA_PLATFORM"] = "offscreen"
        proc = subprocess.run(
            ["/usr/bin/python3", str(app_script()), "gui", "--quit-after-load"],
            cwd="/tmp",
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + "\n" + proc.stderr)


if __name__ == "__main__":
    unittest.main()
