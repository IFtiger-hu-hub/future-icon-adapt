"""Run WheelSafeComboBox checks in a fresh org.kde.desktop process.

These cannot share a QGuiApplication created by other tests, because
QT_QUICK_CONTROLS_STYLE must be applied before the first QGuiApplication
(the 0.2.8 suite missed the Plasma style for that reason).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROBE = Path(__file__).resolve().parent / "_combo_wheel_desktop_probe.py"


class KdeDesktopStyleWheelTests(unittest.TestCase):
    def test_wheelsafe_under_org_kde_desktop_and_main_qml(self):
        env = os.environ.copy()
        env["QT_QPA_PLATFORM"] = "offscreen"
        env["QT_QUICK_CONTROLS_STYLE"] = "org.kde.desktop"
        env["QML_DISABLE_DISK_CACHE"] = "1"
        env["PYTHONPATH"] = os.pathsep.join([str(ROOT), env.get("PYTHONPATH", "")]).rstrip(os.pathsep)
        proc = subprocess.run(
            [sys.executable, str(PROBE)],
            cwd=str(ROOT),
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )
        stdout = proc.stdout.strip()
        # The probe prints a JSON object as its last (or only) stdout payload.
        payload = stdout
        if payload:
            # If Qt / Kirigami logged to stdout, take the last JSON object.
            start = payload.rfind("{")
            if start > 0:
                # find matching top-level object start
                start = payload.find("{")
                # Prefer the last complete object if multiple
                try:
                    json.loads(payload)
                except json.JSONDecodeError:
                    for i, ch in enumerate(payload):
                        if ch == "{":
                            try:
                                json.loads(payload[i:])
                                start = i
                                break
                            except json.JSONDecodeError:
                                continue
                    payload = payload[start:]
        try:
            report = json.loads(payload) if payload else {"ok": False, "error": "empty stdout"}
        except json.JSONDecodeError:
            self.fail(
                "desktop wheel probe did not print JSON\n"
                f"rc={proc.returncode}\nstdout={proc.stdout[-4000:]}\nstderr={proc.stderr[-4000:]}"
            )
        self.assertTrue(
            report.get("ok") and proc.returncode == 0,
            f"desktop wheel probe failed: {report.get('error')}\n"
            f"rc={proc.returncode}\nreport={json.dumps(report, ensure_ascii=False, default=str)[-4000:]}\n"
            f"stderr={proc.stderr[-3000:]}",
        )
        self.assertEqual(report.get("style"), "org.kde.desktop")
        cases = report.get("cases") or {}
        self.assertTrue(cases.get("plain_wheel_enabled"))
        self.assertFalse(cases.get("safe_wheel_enabled"))
        self.assertFalse(cases.get("main_src_wheelEnabled"))
        self.assertGreater(cases.get("plain_item_delivery", {}).get("activated", 0), 0)
        self.assertGreater(
            cases.get("safe_window_angle_scroll", {}).get("content_y", 0),
            cases.get("safe_window_angle_scroll", {}).get("start_y", 0),
        )
        self.assertGreater(
            cases.get("main_src_window_scroll", {}).get("content_y", 0),
            cases.get("main_src_window_scroll", {}).get("start_y", 0),
        )
        self.assertEqual(report.get("real_before"), report.get("real_after"))
        self.assertEqual(report.get("iso_before"), report.get("iso_after"))


if __name__ == "__main__":
    unittest.main()
