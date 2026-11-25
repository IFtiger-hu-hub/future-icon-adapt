"""Wheel tests under org.kde.desktop, the style Plasma actually uses.

0.2.8's offscreen tests used the default Fusion style, where ComboBox.wheelEnabled
is already false, so they could not see QQuickComboBox::wheelEvent changing the
index. These tests start a fresh process with QT_QUICK_CONTROLS_STYLE=org.kde.desktop
and send wheel events to the ComboBox item itself. They load QML offscreen; they
are not a live Wayland/Plasma session test.
"""

from __future__ import annotations

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
STYLE = "org.kde.desktop"


def _style_name() -> str:
    from PyQt6.QtQuickControls2 import QQuickStyle

    return QQuickStyle.name()


def _require_kde_desktop_style():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    if os.environ.get("QT_QUICK_CONTROLS_STYLE") != STYLE:
        raise unittest.SkipTest(f"requires QT_QUICK_CONTROLS_STYLE={STYLE} in a fresh process")
    app = ensure_qt_app()
    if _style_name() != STYLE:
        raise unittest.SkipTest(
            f"style is {_style_name()!r}, not {STYLE} (must be set before QGuiApplication)"
        )
    return app


class KdeDesktopStyleWheelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._app = _require_kde_desktop_style()

    def setUp(self):
        from PyQt6.QtCore import QUrl
        from PyQt6.QtQml import QQmlApplicationEngine
        from PyQt6.QtQuick import QQuickItem

        self._tmp = tempfile.TemporaryDirectory(prefix="iconadapt-wheel-kde-")
        qml_path = Path(self._tmp.name) / "probe.qml"
        qml_path.write_text(PROBE, encoding="utf-8")
        self.engine = QQmlApplicationEngine()
        self.engine.addImportPath(str(QML_DIR))
        self.engine.load(QUrl.fromLocalFile(str(qml_path)))
        self.assertTrue(self.engine.rootObjects(), "kde-style probe QML failed to load")
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

    def test_style_enables_wheel_on_plain_combobox_but_safe_box_stays_put(self):
        from PyQt6.QtCore import QUrl
        from PyQt6.QtQml import QQmlApplicationEngine
        from PyQt6.QtQuick import QQuickItem

        # Document the style bug: a plain ComboBox under org.kde.desktop
        # changes currentIndex on item-targeted wheel.
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
            "        objectName: \"plain\"\n"
            "        width: 200\n"
            "        model: [\"a\", \"b\", \"c\", \"d\"]\n"
            "        currentIndex: 1\n"
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
        self.assertTrue(
            bool(plain.property("wheelEnabled")),
            "org.kde.desktop should arm wheelEnabled on a plain ComboBox",
        )
        _send_wheel(plain, angle_y=-120, target="item")
        self._app.processEvents()
        _send_wheel(plain, angle_y=-120, target="item")
        self._app.processEvents()
        self.assertNotEqual(int(plain.property("currentIndex")), 1)
        self.assertGreater(int(root.property("activatedCount") or 0), 0)
        root.close()
        engine.deleteLater()
        self._app.processEvents()

        self.assertFalse(bool(self.box.property("wheelEnabled")))
        start = int(self.box.property("currentIndex"))
        for angle in (-120, -240, 120, 240):
            _send_wheel(self.box, angle_y=angle, target="item")
            self._app.processEvents()
        for pixel in (-40, 40, -80):
            _send_wheel(self.box, angle_y=0, pixel_y=pixel, target="item")
            self._app.processEvents()
        self.assertEqual(int(self.box.property("currentIndex")), start)
        self.assertEqual(int(self.root.property("activatedCount") or 0), 0)

    def test_focused_closed_box_item_and_window_wheel_keep_value(self):
        from PyQt6.QtCore import QMetaObject

        QMetaObject.invokeMethod(self.root, "focusBox")
        self._app.processEvents()
        start = int(self.box.property("currentIndex"))
        _send_wheel(self.box, angle_y=-120, target="item")
        self._app.processEvents()
        _send_wheel(self.box, angle_y=-240, target="window")
        self._app.processEvents()
        _send_wheel(self.box, angle_y=0, pixel_y=-60, target="item")
        self._app.processEvents()
        self.assertEqual(int(self.box.property("currentIndex")), start)
        self.assertEqual(int(self.root.property("activatedCount") or 0), 0)
        self.assertFalse(bool(self.box.property("wheelEnabled")))


class KdeDesktopMainQmlWheelTests(unittest.TestCase):
    """Real Main.qml under org.kde.desktop, isolated backend only."""

    @classmethod
    def setUpClass(cls):
        cls._app = _require_kde_desktop_style()

    def setUp(self):
        from PyQt6.QtCore import QUrl
        from PyQt6.QtQml import QQmlApplicationEngine
        from PyQt6.QtQuick import QQuickItem

        from iconadapt.backend import IconAdaptBackend
        from iconadapt.gui import qml_dir
        from tests.fixtures import make_desktop, make_theme

        self.tmp = tempfile.TemporaryDirectory(prefix="iconadapt-wheel-main-")
        root = Path(self.tmp.name)
        icons = root / "icons"
        apps = root / "apps"
        apps.mkdir()
        make_theme(icons, "PackA", "hicolor", {"demo": ["48x48/apps"], "folder": ["48x48/places"]})
        make_theme(icons, "PackB", "hicolor", {"demo": ["48x48/apps"], "folder": ["48x48/places"]})
        make_theme(icons, "hicolor", "", {"demo": ["48x48/apps"]})
        make_desktop(apps, "demo.desktop", name="Demo", icon="demo")
        self.iso = {
            "data_home": str(root / "data"),
            "config_home": str(root / "config"),
            "cache_home": str(root / "cache"),
            "icon_dirs": [str(icons)],
            "app_dirs": [str(apps)],
            "current_theme": "PackA",
            "current_desktop": "KDE",
        }
        Path(self.iso["cache_home"]).mkdir(parents=True, exist_ok=True)
        self.be = IconAdaptBackend(isolated=self.iso)
        self.be.setMaskJson(
            '{"enabled": true, "shape": "circle", "custom_path": "", '
            '"background": "#00000000", "padding": 0.0}'
        )
        self.be.setModuleSource("apps", "PackA")
        self.be.setModuleSource("places", "PackA")
        self.engine = QQmlApplicationEngine()
        self.engine.rootContext().setContextProperty("iconAdapt", self.be)
        self.engine.load(QUrl.fromLocalFile(str(qml_dir() / "Main.qml")))
        self.assertTrue(self.engine.rootObjects(), "Main.qml failed to load under org.kde.desktop")
        self.win = self.engine.rootObjects()[0]
        if hasattr(self.win, "setWidth"):
            self.win.setWidth(1200)
            self.win.setHeight(800)
        self.win.show()
        self._app.processEvents()

        found = _pump(self._app, lambda: bool(find_named_quick_items(self.win, "moduleSourceBox")), timeout=4.0)
        self.filter_box = (find_named_quick_items(self.win, "filterBox") or [None])[0]
        self.src_boxes = find_named_quick_items(self.win, "moduleSourceBox")
        self.shape_box = (find_named_quick_items(self.win, "maskShapeBox") or [None])[0]
        self.assertIsNotNone(self.filter_box)
        self.assertTrue(found and self.src_boxes, "no module source ComboBox in Main.qml visual tree")
        self.assertIsNotNone(self.shape_box)
        _pump(
            self._app,
            lambda: self.filter_box.height() > 0 and self.src_boxes[0].width() > 0 and self.shape_box.height() > 0,
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

    def _assert_unmoved(self, item, label):
        self.assertFalse(bool(item.property("wheelEnabled")), f"{label} wheelEnabled should be false")
        start = int(item.property("currentIndex"))
        item.forceActiveFocus()
        self._app.processEvents()
        for target in ("item", "window"):
            for angle in (-120, -240, 120):
                _send_wheel(item, angle_y=angle, target=target)
                self._app.processEvents()
            _send_wheel(item, angle_y=0, pixel_y=-50, target=target)
            self._app.processEvents()
        self.assertEqual(
            int(item.property("currentIndex")),
            start,
            f"{label} index changed by closed-state wheel",
        )
        self.assertFalse(bool(item.property("down")), f"{label} popup should stay closed")

    def test_all_main_combos_ignore_closed_wheel(self):
        import json

        start_filter = self.be._filter
        start_modules = json.loads(self.be.modulesJson)
        start_mask = json.loads(self.be.maskJson)
        self._assert_unmoved(self.filter_box, "filterBox")
        self._assert_unmoved(self.src_boxes[0], "srcBox/moduleSourceBox")
        self._assert_unmoved(self.shape_box, "shapeBox/maskShapeBox")
        self.assertEqual(self.be._filter, start_filter)
        self.assertEqual(json.loads(self.be.modulesJson), start_modules)
        self.assertEqual(json.loads(self.be.maskJson)["shape"], start_mask.get("shape"))


class SpawnKdeDesktopWheelTests(unittest.TestCase):
    def test_org_kde_desktop_suite_in_fresh_process(self):
        if os.environ.get("ICONADAPT_WHEEL_STYLE_CHILD") == "1":
            self.skipTest("already inside the style child process")
        env = os.environ.copy()
        env["QT_QPA_PLATFORM"] = "offscreen"
        env["QT_QUICK_CONTROLS_STYLE"] = STYLE
        env["ICONADAPT_WHEEL_STYLE_CHILD"] = "1"
        env["PYTHONPATH"] = str(PROJECT_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "unittest",
                "tests.test_combo_wheel_kde.KdeDesktopStyleWheelTests",
                "tests.test_combo_wheel_kde.KdeDesktopMainQmlWheelTests",
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
