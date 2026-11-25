"""Wheel tests under org.kde.breeze, the style this Plasma session loads.

0.2.10 only covered org.kde.desktop's wheelEnabled path. The live process
loads org.kde.breeze via KDEPlasmaPlatformTheme6 (libBreezeStyle.so). That
style leaves wheelEnabled false, but ComboBox.qml installs a WheelHandler
{ target: control } that calls incrementCurrentIndex while collapsed.

QCoreApplication.sendEvent() to the ComboBox item does not reach that
handler, which is why earlier breeze checks looked green. Window / pointer
delivery does, and is the path that matches a real hover wheel. These
tests start a fresh process with QT_QUICK_CONTROLS_STYLE=org.kde.breeze.
They are offscreen QML tests, not a live Wayland/Plasma end-to-end run.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from tests.qtutil import ensure_qt_app, find_named_quick_items
from tests.test_combo_wheel import PROBE, QML_DIR, _pump, _send_wheel

PROJECT_ROOT = Path(__file__).resolve().parents[1]
STYLE = "org.kde.breeze"


def _style_name() -> str:
    from PyQt6.QtQuickControls2 import QQuickStyle

    return QQuickStyle.name()


def _require_breeze_style():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ.setdefault("QML_DISABLE_DISK_CACHE", "1")
    if os.environ.get("QT_QUICK_CONTROLS_STYLE") != STYLE:
        raise unittest.SkipTest(f"requires QT_QUICK_CONTROLS_STYLE={STYLE} in a fresh process")
    app = ensure_qt_app()
    if _style_name() != STYLE:
        raise unittest.SkipTest(
            f"style is {_style_name()!r}, not {STYLE} (must be set before QGuiApplication)"
        )
    return app


def _handler_info(box):
    """List QQuickWheelHandler children of a ComboBox."""
    found = []
    seen = set()
    kept = []
    from PyQt6 import sip

    def ptr(obj):
        try:
            return int(sip.unwrapinstance(obj))
        except Exception:
            return id(obj)

    def walk(obj):
        if obj is None:
            return
        p = ptr(obj)
        if p in seen:
            return
        seen.add(p)
        kept.append(obj)
        try:
            cls = obj.metaObject().className()
        except Exception:
            cls = type(obj).__name__
        if cls == "QQuickWheelHandler":
            found.append(
                {
                    "objectName": obj.objectName(),
                    "enabled": bool(obj.property("enabled")),
                    "acceptedDevices": str(obj.property("acceptedDevices")),
                    "item": obj,
                }
            )
        try:
            for child in obj.children():
                walk(child)
        except Exception:
            pass

    walk(box)
    return found


class BreezeStyleWheelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._app = _require_breeze_style()

    def setUp(self):
        from PyQt6.QtCore import QUrl
        from PyQt6.QtQml import QQmlApplicationEngine
        from PyQt6.QtQuick import QQuickItem

        self._tmp = tempfile.TemporaryDirectory(prefix="iconadapt-wheel-breeze-")
        qml_path = Path(self._tmp.name) / "probe.qml"
        qml_path.write_text(PROBE, encoding="utf-8")
        self.engine = QQmlApplicationEngine()
        self.engine.addImportPath(str(QML_DIR))
        self.engine.load(QUrl.fromLocalFile(str(qml_path)))
        self.assertTrue(self.engine.rootObjects(), "breeze-style probe QML failed to load")
        self.root = self.engine.rootObjects()[0]
        self.root.show()
        self._app.processEvents()
        self.box = self.root.findChild(QQuickItem, "box")
        self.flick = self.root.findChild(QQuickItem, "flick")
        self.assertIsNotNone(self.box)
        _pump(self._app, lambda: self.box.width() > 0)

    def tearDown(self):
        for obj in list(self.engine.rootObjects()):
            obj.close()
        self.engine.deleteLater()
        self._app.processEvents()
        self._tmp.cleanup()

    def test_plain_breeze_changes_on_window_wheel_even_when_wheel_enabled_is_false(self):
        from PyQt6.QtCore import QUrl
        from PyQt6.QtQml import QQmlApplicationEngine
        from PyQt6.QtQuick import QQuickItem

        # Documents the style bug and the event-delivery gap: item sendEvent
        # does not move the index; window delivery does, despite wheelEnabled
        # already being false.
        qml = (
            "import QtQuick\n"
            "import QtQuick.Controls\n"
            "import QtQuick.Window\n"
            "Window {\n"
            "    id: root\n"
            "    width: 280\n"
            "    height: 160\n"
            "    visible: true\n"
            "    property int activatedCount: 0\n"
            "    ComboBox {\n"
            '        objectName: "plain"\n'
            "        width: 200\n"
            '        model: ["McMojave-circle", "Papirus", "Tela", "Breeze"]\n'
            "        currentIndex: 0\n"
            "        onActivated: root.activatedCount += 1\n"
            "    }\n"
            "}\n"
        )
        path = Path(self._tmp.name) / "plain.qml"
        path.write_text(qml, encoding="utf-8")
        engine = QQmlApplicationEngine()
        engine.load(QUrl.fromLocalFile(str(path)))
        self.assertTrue(engine.rootObjects())
        root = engine.rootObjects()[0]
        root.show()
        self._app.processEvents()
        plain = root.findChild(QQuickItem, "plain")
        self.assertIsNotNone(plain)
        self.assertFalse(
            bool(plain.property("wheelEnabled")),
            "org.kde.breeze should already leave wheelEnabled false",
        )
        handlers = _handler_info(plain)
        self.assertTrue(
            any(h["enabled"] and "Mouse" in h["acceptedDevices"] for h in handlers),
            f"expected an enabled style WheelHandler, got {handlers}",
        )

        _send_wheel(plain, angle_y=-120, target="item")
        self._app.processEvents()
        _send_wheel(plain, angle_y=-120, target="item")
        self._app.processEvents()
        self.assertEqual(
            int(plain.property("currentIndex")),
            0,
            "item-targeted sendEvent must not be treated as covering the breeze path",
        )

        for _ in range(4):
            _send_wheel(plain, angle_y=-120, target="window")
            self._app.processEvents()
        self.assertNotEqual(int(plain.property("currentIndex")), 0)
        self.assertGreater(int(root.property("activatedCount") or 0), 0)
        root.close()
        engine.deleteLater()
        self._app.processEvents()

    def test_wheelsafe_window_wheel_keeps_value_and_disarms_style_handler(self):
        self.assertFalse(bool(self.box.property("wheelEnabled")))
        handlers = _handler_info(self.box)
        foreign = [
            h
            for h in handlers
            if h["objectName"] != "iconAdaptClosedWheelGate"
        ]
        self.assertTrue(foreign, f"expected the breeze style WheelHandler to exist: {handlers}")
        self.assertTrue(
            all(not h["enabled"] for h in foreign),
            f"style WheelHandler should be disarmed, got {foreign}",
        )
        start = int(self.box.property("currentIndex"))
        start_activated = int(self.root.property("activatedCount") or 0)
        # The path that previously still changed WheelSafe under breeze.
        for _ in range(4):
            _send_wheel(self.box, angle_y=-120, target="window")
            self._app.processEvents()
        for angle in (-240, 120, 240):
            _send_wheel(self.box, angle_y=angle, target="window")
            self._app.processEvents()
        _send_wheel(self.box, angle_y=0, pixel_y=-60, target="window")
        self._app.processEvents()
        _send_wheel(self.box, angle_y=-120, target="item")
        self._app.processEvents()
        self.assertEqual(int(self.box.property("currentIndex")), start)
        self.assertEqual(int(self.root.property("activatedCount") or 0), start_activated)
        self.assertFalse(bool(self.box.property("down")))

    def test_keyboard_still_changes_or_opens_under_breeze(self):
        from PyQt6.QtCore import QCoreApplication, Qt
        from PyQt6.QtGui import QKeyEvent
        from PyQt6.QtQuick import QQuickItem

        self.box.forceActiveFocus()
        self._app.processEvents()
        start = int(self.box.property("currentIndex"))

        def _key(k, modifiers=Qt.KeyboardModifier.NoModifier):
            QCoreApplication.sendEvent(
                self.box, QKeyEvent(QKeyEvent.Type.KeyPress, k, modifiers)
            )
            QCoreApplication.sendEvent(
                self.box, QKeyEvent(QKeyEvent.Type.KeyRelease, k, modifiers)
            )
            self._app.processEvents()

        _key(Qt.Key.Key_Down)
        changed = int(self.box.property("currentIndex")) != start
        opened = bool(self.box.property("down"))
        if not changed and not opened:
            _key(Qt.Key.Key_Space)
            opened = bool(self.box.property("down"))
        self.assertTrue(
            changed or opened,
            "keyboard should still change the index or open the popup under breeze",
        )

    def test_open_popup_still_opens_under_breeze(self):
        from PyQt6.QtCore import QMetaObject

        QMetaObject.invokeMethod(self.root, "openPopup")
        self._app.processEvents()
        popup_open = bool(self.box.property("down"))
        popup = self.box.property("popup")
        if not popup_open and popup is not None and hasattr(popup, "property"):
            popup_open = bool(popup.property("opened"))
        self.assertTrue(popup_open, "opening the popup should still work")


class BreezeMainQmlWheelTests(unittest.TestCase):
    """Real Main.qml module source ComboBox (the control the user scrolled)."""

    @classmethod
    def setUpClass(cls):
        cls._app = _require_breeze_style()

    def setUp(self):
        from PyQt6.QtCore import QUrl
        from PyQt6.QtQml import QQmlApplicationEngine

        from iconadapt.backend import IconAdaptBackend
        from iconadapt.gui import qml_dir
        from tests.fixtures import make_desktop, make_theme

        self.tmp = tempfile.TemporaryDirectory(prefix="iconadapt-wheel-breeze-main-")
        root = Path(self.tmp.name)
        icons = root / "icons"
        apps = root / "apps"
        apps.mkdir()
        make_theme(icons, "McMojave-circle", "hicolor", {"demo": ["48x48/apps"], "folder": ["48x48/places"]})
        make_theme(icons, "Papirus", "hicolor", {"demo": ["48x48/apps"], "folder": ["48x48/places"]})
        make_theme(icons, "hicolor", "", {"demo": ["48x48/apps"]})
        make_desktop(apps, "demo.desktop", name="Demo", icon="demo")
        self.iso = {
            "data_home": str(root / "data"),
            "config_home": str(root / "config"),
            "cache_home": str(root / "cache"),
            "icon_dirs": [str(icons)],
            "app_dirs": [str(apps)],
            "current_theme": "McMojave-circle",
            "current_desktop": "KDE",
        }
        Path(self.iso["cache_home"]).mkdir(parents=True, exist_ok=True)
        self.be = IconAdaptBackend(isolated=self.iso)
        self.be.setMaskJson(
            '{"enabled": true, "shape": "circle", "custom_path": "", '
            '"background": "#00000000", "padding": 0.0}'
        )
        self.be.setModuleSource("apps", "McMojave-circle")
        self.engine = QQmlApplicationEngine()
        self.engine.rootContext().setContextProperty("iconAdapt", self.be)
        self.engine.load(QUrl.fromLocalFile(str(qml_dir() / "Main.qml")))
        self.assertTrue(self.engine.rootObjects(), "Main.qml failed to load under org.kde.breeze")
        self.win = self.engine.rootObjects()[0]
        if hasattr(self.win, "setWidth"):
            self.win.setWidth(1200)
            self.win.setHeight(800)
        self.win.show()
        self._app.processEvents()
        found = _pump(
            self._app,
            lambda: bool(find_named_quick_items(self.win, "moduleSourceBox")),
            timeout=4.0,
        )
        self.src_boxes = find_named_quick_items(self.win, "moduleSourceBox")
        self.filter_box = (find_named_quick_items(self.win, "filterBox") or [None])[0]
        self.shape_box = (find_named_quick_items(self.win, "maskShapeBox") or [None])[0]
        self.assertTrue(found and self.src_boxes, "no moduleSourceBox in Main.qml visual tree")
        self.assertIsNotNone(self.filter_box)
        self.assertIsNotNone(self.shape_box)
        _pump(
            self._app,
            lambda: self.src_boxes[0].width() > 0 and self.filter_box.height() > 0,
            timeout=3.0,
        )

    def tearDown(self):
        for obj in list(self.engine.rootObjects()):
            obj.close()
        self._app.processEvents()
        try:
            self.be.shutdown()
        except Exception:
            pass
        self.engine.deleteLater()
        self._app.processEvents()
        self.tmp.cleanup()

    def _assert_unmoved_window_wheel(self, item, label):
        self.assertFalse(bool(item.property("wheelEnabled")), f"{label} wheelEnabled")
        start = int(item.property("currentIndex"))
        item.forceActiveFocus()
        self._app.processEvents()
        for angle in (-120, -120, -240, 120):
            _send_wheel(item, angle_y=angle, target="window")
            self._app.processEvents()
        _send_wheel(item, angle_y=0, pixel_y=-50, target="window")
        self._app.processEvents()
        self.assertEqual(
            int(item.property("currentIndex")),
            start,
            f"{label} index changed by closed-state window wheel under org.kde.breeze",
        )
        self.assertFalse(bool(item.property("down")), f"{label} popup should stay closed")

    def test_apps_module_source_box_ignores_closed_window_wheel(self):
        # The first module source box is "应用图标", the control in the user's screenshot.
        src = self.src_boxes[0]
        start_modules = json.loads(self.be.modulesJson)
        self.assertEqual(start_modules[0]["id"], "apps")
        self.assertEqual(start_modules[0]["source"], "McMojave-circle")
        self._assert_unmoved_window_wheel(src, "apps moduleSourceBox")
        self._assert_unmoved_window_wheel(self.filter_box, "filterBox")
        self._assert_unmoved_window_wheel(self.shape_box, "maskShapeBox")
        now = json.loads(self.be.modulesJson)
        self.assertEqual(now[0]["source"], "McMojave-circle")
        self.assertEqual(now, start_modules)


class SpawnBreezeWheelTests(unittest.TestCase):
    def test_org_kde_breeze_suite_in_fresh_process(self):
        if os.environ.get("ICONADAPT_WHEEL_STYLE_CHILD") == "1":
            self.skipTest("already inside the style child process")
        env = os.environ.copy()
        env["QT_QPA_PLATFORM"] = "offscreen"
        env["QT_QUICK_CONTROLS_STYLE"] = STYLE
        env["QML_DISABLE_DISK_CACHE"] = "1"
        env["ICONADAPT_WHEEL_STYLE_CHILD"] = "1"
        env["PYTHONPATH"] = str(PROJECT_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
        # Do not inherit the user's live display.
        env.pop("DISPLAY", None)
        env.pop("WAYLAND_DISPLAY", None)
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "unittest",
                "tests.test_combo_wheel_breeze.BreezeStyleWheelTests",
                "tests.test_combo_wheel_breeze.BreezeMainQmlWheelTests",
                "-v",
            ],
            cwd=str(PROJECT_ROOT),
            env=env,
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            self.fail(proc.stdout + "\n" + proc.stderr)


if __name__ == "__main__":
    unittest.main()
