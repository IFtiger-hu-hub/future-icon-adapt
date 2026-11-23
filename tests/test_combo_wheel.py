"""Wheel over a collapsed ComboBox must scroll the page, not change the value."""

from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path

from tests.qtutil import ensure_qt_app

QML_DIR = Path(__file__).resolve().parents[1] / "qml"

PROBE = """
import QtQuick
import QtQuick.Controls
import QtQuick.Window
import "file:{qml_dir}"

Window {{
    id: root
    width: 360
    height: 420
    visible: true
    color: "#ffffff"

    property int activatedCount: 0
    property bool popupOpen: box.popup.opened
    property bool boxDown: box.down
    function openPopup() {{ box.popup.open() }}
    function closePopup() {{ box.popup.close() }}
    function focusBox() {{ box.forceActiveFocus() }}

    Flickable {{
        id: flick
        objectName: "flick"
        anchors.fill: parent
        contentWidth: width
        contentHeight: col.height
        boundsBehavior: Flickable.StopAtBounds
        clip: true
        interactive: true

        Column {{
            id: col
            width: flick.width
            spacing: 8
            Repeater {{
                model: 3
                Rectangle {{ width: col.width; height: 24; color: "#f0f0f0" }}
            }}
            WheelSafeComboBox {{
                id: box
                objectName: "box"
                width: 220
                model: ["alpha", "beta", "gamma", "delta", "epsilon", "zeta", "eta", "theta"]
                currentIndex: 1
                onActivated: root.activatedCount += 1
            }}
            Repeater {{
                model: 28
                Rectangle {{
                    width: col.width
                    height: 36
                    color: index % 2 ? "#eeeeee" : "#e0e0e0"
                }}
            }}
        }}
    }}
}}
""".format(qml_dir=QML_DIR.as_posix())


def _pump(app, pred, timeout=2.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        app.processEvents()
        time.sleep(0.01)
    return False


def _send_wheel(item, *, angle_y: int, pixel_y: int = 0, target: str = "window"):
    """Deliver a QWheelEvent to the window or directly to the ComboBox item.

    Window delivery is what the 0.2.8 tests used; it can be consumed by a
    child WheelHandler and never reach QQuickComboBox::wheelEvent. Item
    delivery matches the hover path on a real desktop, where the C++
    wheelEvent still runs when wheelEnabled is true.
    """
    from PyQt6.QtCore import QCoreApplication, QPoint, QPointF, Qt
    from PyQt6.QtGui import QWheelEvent

    window = item.window()
    local = QPointF(max(1.0, item.width() / 2.0), max(1.0, item.height() / 2.0))
    scene = item.mapToScene(local)
    global_pos = window.mapToGlobal(scene)
    pos = local if target == "item" else scene
    event = QWheelEvent(
        pos,
        global_pos,
        QPoint(0, pixel_y),
        QPoint(0, angle_y),
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
        Qt.ScrollPhase.NoScrollPhase,
        False,
    )
    QCoreApplication.sendEvent(item if target == "item" else window, event)


class WheelSafeComboBoxTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        cls._app = ensure_qt_app()

    def setUp(self):
        from PyQt6.QtCore import QUrl
        from PyQt6.QtQml import QQmlApplicationEngine

        self._tmp = tempfile.TemporaryDirectory(prefix="iconadapt-wheel-")
        qml_path = Path(self._tmp.name) / "probe.qml"
        qml_path.write_text(PROBE, encoding="utf-8")
        self.engine = QQmlApplicationEngine()
        self.engine.addImportPath(str(QML_DIR))
        self.engine.load(QUrl.fromLocalFile(str(qml_path)))
        self.assertTrue(self.engine.rootObjects(), "wheel probe QML failed to load")
        self.root = self.engine.rootObjects()[0]
        self.root.show()
        self._app.processEvents()
        from PyQt6.QtQuick import QQuickItem

        self.box = self.root.findChild(QQuickItem, "box")
        self.flick = self.root.findChild(QQuickItem, "flick")
        self.assertIsNotNone(self.box)
        self.assertIsNotNone(self.flick)
        # Give the scene a chance to polish sizes.
        _pump(self._app, lambda: self.box.width() > 0 and self.flick.height() > 0)

    def tearDown(self):
        for obj in list(self.engine.rootObjects()):
            obj.close()
        self.engine.deleteLater()
        self._app.processEvents()
        self._tmp.cleanup()

    def test_closed_wheel_scrolls_page_and_keeps_selection(self):
        start_index = int(self.box.property("currentIndex"))
        self.assertEqual(start_index, 1)
        start_y = float(self.flick.property("contentY") or 0)
        self.assertEqual(start_y, 0.0)
        # Negative angleDelta.y is a typical "scroll down" notch (120 eighth-degrees).
        for _ in range(4):
            _send_wheel(self.box, angle_y=-120)
            self._app.processEvents()
        self.assertEqual(int(self.box.property("currentIndex")), 1)
        self.assertEqual(int(self.root.property("activatedCount") or 0), 0)
        self.assertFalse(bool(self.box.property("down")))
        new_y = float(self.flick.property("contentY") or 0)
        self.assertGreater(
            new_y,
            start_y + 1,
            f"outer Flickable did not move; contentY={new_y} contentHeight={self.flick.property('contentHeight')}",
        )

    def test_closed_wheel_does_not_save_via_activated(self):
        _send_wheel(self.box, angle_y=-240)
        self._app.processEvents()
        _send_wheel(self.box, angle_y=240)
        self._app.processEvents()
        self.assertEqual(int(self.box.property("currentIndex")), 1)
        self.assertEqual(int(self.root.property("activatedCount") or 0), 0)

    def test_keyboard_still_changes_or_opens(self):
        from PyQt6.QtCore import QMetaObject, Qt
        from PyQt6.QtTest import QTest

        QMetaObject.invokeMethod(self.root, "focusBox")
        self._app.processEvents()
        window = self.root
        QTest.keyClick(window, Qt.Key.Key_Down)
        self._app.processEvents()
        QTest.keyClick(window, Qt.Key.Key_Space)
        self._app.processEvents()
        QTest.keyClick(window, Qt.Key.Key_Down)
        self._app.processEvents()
        opened = bool(self.root.property("boxDown")) or bool(self.root.property("popupOpen"))
        index_changed = int(self.box.property("currentIndex")) != 1
        self.assertTrue(
            opened or index_changed,
            "keyboard should still open the popup or move the current index",
        )

    def test_open_popup_is_not_blocked_by_closed_handler(self):
        from PyQt6.QtCore import QMetaObject

        # Opening the popup must still be possible; the closed-state handler disables itself.
        QMetaObject.invokeMethod(self.root, "openPopup")
        self._app.processEvents()
        self.assertTrue(
            bool(self.root.property("popupOpen")) or bool(self.root.property("boxDown")),
            "popup should open via ComboBox.popup.open()",
        )
        start_outer = float(self.flick.property("contentY") or 0)
        start_index = int(self.box.property("currentIndex"))
        # Wheel while open must not fire onActivated or change the closed-box value
        # (selection happens by click / keyboard, not by the collapsed-state handler).
        _send_wheel(self.box, angle_y=-120)
        self._app.processEvents()
        self.assertEqual(int(self.box.property("currentIndex")), start_index)
        self.assertEqual(int(self.root.property("activatedCount") or 0), 0)
        QMetaObject.invokeMethod(self.root, "closePopup")
        self._app.processEvents()
        self.assertFalse(bool(self.root.property("popupOpen")))
        _send_wheel(self.box, angle_y=-120)
        self._app.processEvents()
        self.assertEqual(int(self.box.property("currentIndex")), start_index)
        self.assertGreaterEqual(float(self.flick.property("contentY") or 0), start_outer)

    def test_closed_wheel_to_item_does_not_change_index(self):
        # Direct item delivery is the path that bypassed the 0.2.8 handler.
        start_index = int(self.box.property("currentIndex"))
        for angle in (-120, -240, 120, 240, -120):
            _send_wheel(self.box, angle_y=angle, target="item")
            self._app.processEvents()
        for pixel in (-40, 40, -80):
            _send_wheel(self.box, angle_y=0, pixel_y=pixel, target="item")
            self._app.processEvents()
        self.assertEqual(int(self.box.property("currentIndex")), start_index)
        self.assertEqual(int(self.root.property("activatedCount") or 0), 0)
        self.assertFalse(bool(self.box.property("wheelEnabled")))

    def test_component_forces_wheel_enabled_off(self):
        src = (QML_DIR / "WheelSafeComboBox.qml").read_text(encoding="utf-8")
        self.assertIn("wheelEnabled: false", src)
        self.assertFalse(bool(self.box.property("wheelEnabled")))


class MainQmlComboBoxWiringTests(unittest.TestCase):
    def test_main_qml_uses_wheel_safe_for_all_combos(self):
        text = (QML_DIR / "Main.qml").read_text(encoding="utf-8")
        self.assertNotIn("QQC.ComboBox", text)
        self.assertEqual(text.count("WheelSafeComboBox"), 3)
        for name in ("filterBox", "srcBox", "shapeBox"):
            self.assertIn(f"id: {name}", text)
        for name in ("filterBox", "moduleSourceBox", "maskShapeBox", "rightPageScroll"):
            self.assertIn(f'objectName: "{name}"', text)
        self.assertIn("wheelEnabled: false", (QML_DIR / "WheelSafeComboBox.qml").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
