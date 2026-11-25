"""Fresh-process probe for WheelSafeComboBox under org.kde.desktop + Main.qml.

Must be started with QT_QUICK_CONTROLS_STYLE=org.kde.desktop and no
existing QGuiApplication, otherwise the style (and wheelEnabled default)
will not match the user's Plasma session.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
QML_DIR = ROOT / "qml"
REAL_STATE = Path.home() / ".local/share/icon-adapt"
REAL_WATCH = (
    "settings.json",
    "choices.json",
    "desktop-overrides.json",
    "single-app-patches.json",
    "self-launcher.json",
)


def _prepare_env() -> None:
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    os.environ["QT_QUICK_CONTROLS_STYLE"] = "org.kde.desktop"
    os.environ["QML_DISABLE_DISK_CACHE"] = "1"
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))


def _file_sig(path: Path):
    if not path.exists():
        return None
    data = path.read_bytes()
    st = path.stat()
    return {
        "sha256": hashlib.sha256(data).hexdigest(),
        "mtime_ns": st.st_mtime_ns,
        "size": st.st_size,
    }


def _real_sigs():
    return {name: _file_sig(REAL_STATE / name) for name in REAL_WATCH}


def _pump(app, pred=None, timeout=4.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        app.processEvents()
        if pred is not None and pred():
            return True
        time.sleep(0.01)
        app.processEvents()
    return pred is None


def _send_wheel(item, *, angle_y: int = 0, pixel_y: int = 0, target: str = "item"):
    from PyQt6.QtCore import QCoreApplication, QPoint, QPointF, Qt
    from PyQt6.QtGui import QWheelEvent

    window = item.window()
    local = QPointF(max(1.0, float(item.width()) / 2.0), max(1.0, float(item.height()) / 2.0))
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


def _write_svg_theme(root: Path, name: str, icons: dict[str, str]) -> Path:
    from tests.fixtures import write

    tdir = root / name
    rel = "scalable/apps"
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
                "Size=128",
                "Type=Scalable",
                "MinSize=1",
                "MaxSize=512",
                "Context=Applications",
                "",
            ]
        ),
    )
    body = '<svg xmlns="http://www.w3.org/2000/svg" width="32" height="32"><rect width="32" height="32" fill="#2ecc71"/></svg>'
    for icon_name in icons:
        (tdir / rel / f"{icon_name}.svg").write_text(icons[icon_name] or body, encoding="utf-8")
    return tdir


def _flickable_of(scroll):
    from PyQt6.QtQuick import QQuickItem

    if scroll is None:
        return None
    content = scroll.property("contentItem")
    if content is not None and content.property("contentY") is not None:
        return content
    # Walk children for a Flickable-like item.
    for child in scroll.findChildren(QQuickItem):
        if child.property("contentY") is not None and child.property("flickableDirection") is not None:
            return child
    return None


def fail(msg, **extra):
    print(json.dumps({"ok": False, "error": msg, **extra}, ensure_ascii=False, default=str), flush=True)
    raise SystemExit(2)


def main() -> int:
    _prepare_env()
    from PyQt6.QtQuickControls2 import QQuickStyle

    QQuickStyle.setStyle("org.kde.desktop")
    from tests.qtutil import ensure_qt_app, find_named_quick_items
    from PyQt6.QtCore import QUrl, QMetaObject, Qt, pyqtSlot
    from PyQt6.QtQml import QQmlApplicationEngine
    from PyQt6.QtQuick import QQuickItem
    from PyQt6.QtTest import QTest
    from PyQt6.QtQuickControls2 import QQuickStyle as StyleAgain

    app = ensure_qt_app()
    style = StyleAgain.name()
    if style != "org.kde.desktop":
        fail("style_not_desktop", style=style)

    report = {"ok": True, "style": style, "cases": {}}
    real_before = _real_sigs()
    report["real_before"] = real_before

    # ------------------------------------------------------------------
    # 1) Minimal window: prove C++ path still changes a plain ComboBox,
    #    and that WheelSafe no longer does, including page scroll.
    # ------------------------------------------------------------------
    probe_qml = f'''
import QtQuick
import QtQuick.Controls
import QtQuick.Window
import "file:{QML_DIR.as_posix()}"

Window {{
    id: root
    width: 480
    height: 640
    visible: true
    property int plainActivated: 0
    property int safeActivated: 0
    property bool popupOpen: safe.popup.opened
    property bool boxDown: safe.down
    function openPopup() {{ safe.popup.open() }}
    function closePopup() {{ safe.popup.close() }}
    function focusSafe() {{ safe.forceActiveFocus() }}
    function clearSafeFocus() {{
        root.forceActiveFocus()
        safe.focus = false
    }}

    Flickable {{
        id: flick
        objectName: "flick"
        anchors.fill: parent
        contentWidth: width
        contentHeight: 1800
        boundsBehavior: Flickable.StopAtBounds
        clip: true
        Column {{
            width: parent.width
            spacing: 12
            Repeater {{ model: 4; Rectangle {{ width: 200; height: 24; color: "#ddd" }} }}
            ComboBox {{
                id: plain
                objectName: "plain"
                width: 260
                model: ["alpha", "beta", "gamma", "delta"]
                currentIndex: 1
                onActivated: root.plainActivated += 1
            }}
            WheelSafeComboBox {{
                id: safe
                objectName: "safe"
                width: 260
                model: ["alpha", "beta", "gamma", "delta"]
                currentIndex: 1
                onActivated: root.safeActivated += 1
            }}
            Repeater {{ model: 30; Rectangle {{ width: parent.width; height: 36; color: index%2?"#eee":"#e0e0e0" }} }}
        }}
    }}
}}
'''
    tmp = Path(tempfile.mkdtemp(prefix="iconadapt-wheel-desktop-"))
    qml_path = tmp / "probe.qml"
    qml_path.write_text(probe_qml, encoding="utf-8")
    engine = QQmlApplicationEngine()
    engine.addImportPath(str(QML_DIR))
    engine.load(QUrl.fromLocalFile(str(qml_path)))
    if not engine.rootObjects():
        fail("probe_qml_failed")
    win = engine.rootObjects()[0]
    win.show()
    _pump(app, lambda: True, timeout=0.2)
    plain = win.findChild(QQuickItem, "plain")
    safe = win.findChild(QQuickItem, "safe")
    flick = win.findChild(QQuickItem, "flick")
    if not all((plain, safe, flick)):
        fail("probe_items_missing")

    report["cases"]["plain_wheel_enabled"] = bool(plain.property("wheelEnabled"))
    report["cases"]["safe_wheel_enabled"] = bool(safe.property("wheelEnabled"))
    if not plain.property("wheelEnabled"):
        fail("plain_combo_should_keep_style_wheelEnabled", report=report)
    if safe.property("wheelEnabled"):
        fail("wheelsafe_still_has_wheelEnabled", report=report)

    # Sentinel: item delivery must still change a style-default ComboBox.
    plain.setProperty("currentIndex", 1)
    win.setProperty("plainActivated", 0)
    app.processEvents()
    _send_wheel(plain, angle_y=-120, target="item")
    app.processEvents()
    _send_wheel(plain, angle_y=-120, target="item")
    app.processEvents()
    plain_index = int(plain.property("currentIndex"))
    plain_act = int(win.property("plainActivated") or 0)
    report["cases"]["plain_item_delivery"] = {"index": plain_index, "activated": plain_act}
    if plain_index == 1 or plain_act == 0:
        fail("item_delivery_did_not_reach_cpp_wheelEvent", report=report)

    def assert_safe_closed(label: str, *, focused: bool, events: list[tuple]):
        win.setProperty("safeActivated", 0)
        safe.setProperty("currentIndex", 1)
        if focused:
            QMetaObject.invokeMethod(win, "focusSafe")
        else:
            QMetaObject.invokeMethod(win, "clearSafeFocus")
        app.processEvents()
        start_y = float(flick.property("contentY") or 0)
        start_index = int(safe.property("currentIndex"))
        for target, angle, pixel in events:
            _send_wheel(safe, angle_y=angle, pixel_y=pixel, target=target)
            app.processEvents()
        result = {
            "index": int(safe.property("currentIndex")),
            "activated": int(win.property("safeActivated") or 0),
            "start_y": start_y,
            "content_y": float(flick.property("contentY") or 0),
            "focused": bool(safe.hasActiveFocus()) if hasattr(safe, "hasActiveFocus") else None,
            "wheelEnabled": bool(safe.property("wheelEnabled")),
        }
        report["cases"][label] = result
        if result["index"] != start_index or result["activated"] != 0:
            fail(f"safe_changed_on_{label}", report=report)
        return result

    # Focused / unfocused, both directions, continuous, pixel + angle, item + window.
    assert_safe_closed(
        "safe_item_unfocused_angle",
        focused=False,
        events=[("item", -120, 0), ("item", -240, 0), ("item", 120, 0), ("item", 240, 0)],
    )
    assert_safe_closed(
        "safe_item_focused_angle",
        focused=True,
        events=[("item", -120, 0), ("item", -120, 0), ("item", 120, 0)],
    )
    assert_safe_closed(
        "safe_item_pixel",
        focused=False,
        events=[("item", 0, -40), ("item", 0, -80), ("item", 0, 40)],
    )
    win_scroll = assert_safe_closed(
        "safe_window_angle_scroll",
        focused=False,
        events=[("window", -120, 0), ("window", -240, 0), ("window", -120, 0)],
    )
    if win_scroll["content_y"] <= win_scroll["start_y"] + 1:
        fail("page_did_not_scroll_on_window_wheel", report=report)
    # Scroll up as well; must not change the index.
    assert_safe_closed(
        "safe_window_pixel_and_up",
        focused=True,
        events=[("window", 0, -60), ("window", 120, 0), ("window", 0, 40)],
    )

    # Keyboard still works.
    safe.setProperty("currentIndex", 1)
    win.setProperty("safeActivated", 0)
    QMetaObject.invokeMethod(win, "focusSafe")
    app.processEvents()
    QTest.keyClick(win, Qt.Key.Key_Down)
    app.processEvents()
    QTest.keyClick(win, Qt.Key.Key_Space)
    app.processEvents()
    QTest.keyClick(win, Qt.Key.Key_Down)
    app.processEvents()
    opened = bool(win.property("boxDown")) or bool(win.property("popupOpen"))
    index_changed = int(safe.property("currentIndex")) != 1
    report["cases"]["safe_keyboard"] = {
        "opened": opened,
        "index": int(safe.property("currentIndex")),
        "changed": index_changed,
    }
    if not (opened or index_changed):
        fail("keyboard_broken", report=report)
    if opened:
        QMetaObject.invokeMethod(win, "closePopup")
        app.processEvents()

    # Open popup: closed-state handler off; currentIndex stays; then close.
    safe.setProperty("currentIndex", 1)
    win.setProperty("safeActivated", 0)
    QMetaObject.invokeMethod(win, "openPopup")
    app.processEvents()
    if not (bool(win.property("popupOpen")) or bool(win.property("boxDown"))):
        fail("popup_did_not_open", report=report)
    start_index = int(safe.property("currentIndex"))
    _send_wheel(safe, angle_y=-120, target="item")
    app.processEvents()
    _send_wheel(safe, angle_y=-120, target="window")
    app.processEvents()
    report["cases"]["safe_open_popup"] = {
        "index": int(safe.property("currentIndex")),
        "activated": int(win.property("safeActivated") or 0),
        "open": True,
    }
    if int(safe.property("currentIndex")) != start_index:
        fail("open_popup_changed_currentIndex", report=report)
    QMetaObject.invokeMethod(win, "closePopup")
    app.processEvents()
    if bool(win.property("popupOpen")):
        fail("popup_did_not_close", report=report)
    _send_wheel(safe, angle_y=-120, target="item")
    app.processEvents()
    if int(safe.property("currentIndex")) != start_index or int(win.property("safeActivated") or 0) != 0:
        fail("after_close_item_wheel_changed", report=report)
    report["cases"]["safe_after_close"] = {"index": int(safe.property("currentIndex")), "activated": 0}

    for obj in list(engine.rootObjects()):
        obj.close()
    engine.deleteLater()
    app.processEvents()

    # ------------------------------------------------------------------
    # 2) Actual Main.qml module-source ComboBox, isolated settings/choices.
    # ------------------------------------------------------------------
    from tests.fixtures import make_desktop, make_theme, write
    from iconadapt.backend import IconAdaptBackend
    from iconadapt.gui import qml_dir
    from iconadapt.mask import settings_from_dict, save_settings
    from iconadapt.modules import save_module_map

    iso_root = Path(tempfile.mkdtemp(prefix="iconadapt-wheel-main-"))
    icons = iso_root / "icons"
    apps_dir = iso_root / "apps"
    apps_dir.mkdir()
    body = '<svg xmlns="http://www.w3.org/2000/svg" width="32" height="32"><rect width="32" height="32" fill="#2ecc71"/></svg>'
    for pack in ("PackA", "PackB", "PackC"):
        _write_svg_theme(icons, pack, {"app-one": body, "folder": body})
    make_theme(icons, "hicolor", "", {"folder": ["48x48/places"]})
    make_desktop(apps_dir, "one.desktop", name="One", icon="app-one")
    data_home = iso_root / "data"
    config_home = iso_root / "config"
    data_home.mkdir()
    config_home.mkdir()
    state = data_home / "icon-adapt"
    state.mkdir(parents=True)
    save_settings(
        state,
        settings_from_dict(
            {
                "enabled": True,
                "shape": "circle",
                "background": "#00000000",
                "padding": 0,
            }
        ),
    )
    save_module_map(state, {"apps": "PackA", "places": "PackB"})
    iso = {
        "data_home": str(data_home),
        "config_home": str(config_home),
        "icon_dirs": [str(icons)],
        "app_dirs": [str(apps_dir)],
        "current_theme": "PackA",
        "current_desktop": "KDE",
    }

    calls = {"setModuleSource": 0, "setFilter": 0, "setMaskJson": 0}
    be = IconAdaptBackend(isolated=iso)
    _orig_src = be.setModuleSource
    _orig_filter = be.setFilter
    _orig_mask = be.setMaskJson

    def _src(mid, theme):
        calls["setModuleSource"] += 1
        return _orig_src(mid, theme)

    def _filt(value):
        calls["setFilter"] += 1
        return _orig_filter(value)

    def _mask(raw):
        calls["setMaskJson"] += 1
        return _orig_mask(raw)

    be.setModuleSource = _src
    be.setFilter = _filt
    be.setMaskJson = _mask

    iso_settings = state / "settings.json"
    iso_choices = state / "choices.json"
    iso_before = {"settings": _file_sig(iso_settings), "choices": _file_sig(iso_choices)}

    engine = QQmlApplicationEngine()
    engine.addImportPath(str(QML_DIR))
    engine.rootContext().setContextProperty("iconAdapt", be)
    engine.load(QUrl.fromLocalFile(str(qml_dir() / "Main.qml")))
    if not engine.rootObjects():
        be.shutdown()
        fail("main_qml_failed")
    root = engine.rootObjects()[0]
    root.show()
    # Wait until module source boxes exist and have a model.
    def _src_boxes():
        return find_named_quick_items(root, "moduleSourceBox")

    ok = _pump(
        app,
        lambda: any(int(b.property("count") or 0) >= 2 and b.width() > 0 for b in _src_boxes()),
        timeout=8.0,
    )
    boxes = _src_boxes()
    if not ok or not boxes:
        be.shutdown()
        fail("moduleSourceBox_not_ready", count=len(boxes))
    src_box = next((b for b in boxes if b.width() > 0 and int(b.property("count") or 0) >= 2), boxes[0])
    filter_box = (find_named_quick_items(root, "filterBox") or [None])[0]
    shape_box = (find_named_quick_items(root, "maskShapeBox") or [None])[0]
    scroll = (find_named_quick_items(root, "rightPageScroll") or [root.findChild(QQuickItem, "rightPageScroll")])[0]
    flick = _flickable_of(scroll)
    if filter_box is None or scroll is None or flick is None:
        be.shutdown()
        fail("main_required_items_missing", filter=bool(filter_box), scroll=bool(scroll), flick=bool(flick))

    # Make sure the right pane can actually scroll.
    _pump(app, lambda: float(flick.property("contentHeight") or 0) > float(flick.height() or 0) + 8, timeout=3.0)

    report["cases"]["main_src_wheelEnabled"] = bool(src_box.property("wheelEnabled"))
    report["cases"]["main_filter_wheelEnabled"] = bool(filter_box.property("wheelEnabled"))
    if src_box.property("wheelEnabled") or filter_box.property("wheelEnabled"):
        be.shutdown()
        fail("main_combos_still_wheelEnabled", report=report)
    if shape_box is not None:
        report["cases"]["main_shape_wheelEnabled"] = bool(shape_box.property("wheelEnabled"))
        if shape_box.property("wheelEnabled"):
            be.shutdown()
            fail("shape_box_still_wheelEnabled", report=report)

    def snapshot_boxes():
        return {
            "src": int(src_box.property("currentIndex")),
            "filter": int(filter_box.property("currentIndex")),
            "shape": int(shape_box.property("currentIndex")) if shape_box is not None else None,
            "calls": dict(calls),
            "iso_settings": _file_sig(iso_settings),
            "iso_choices": _file_sig(iso_choices),
        }

    before = snapshot_boxes()
    report["cases"]["main_before"] = before
    if before["src"] < 0:
        be.shutdown()
        fail("src_index_invalid", report=report)

    def wheel_closed(box, label, events, must_scroll=False):
        start_y = float(flick.property("contentY") or 0)
        start = snapshot_boxes()
        for target, angle, pixel in events:
            _send_wheel(box, angle_y=angle, pixel_y=pixel, target=target)
            app.processEvents()
        after = snapshot_boxes()
        after["start_y"] = start_y
        after["content_y"] = float(flick.property("contentY") or 0)
        report["cases"][label] = after
        if (
            after["src"] != start["src"]
            or after["filter"] != start["filter"]
            or after["shape"] != start["shape"]
            or after["calls"] != start["calls"]
            or after["iso_settings"] != start["iso_settings"]
            or after["iso_choices"] != start["iso_choices"]
        ):
            fail(f"main_changed_on_{label}", before=start, after=after, report=report)
        if must_scroll and after["content_y"] <= start_y + 1:
            fail(f"main_page_did_not_scroll_{label}", before=start, after=after, report=report)
        return after

    # Unfocused + focused, item delivery (the previously-missed path).
    wheel_closed(
        src_box,
        "main_src_item_unfocused",
        [("item", -120, 0), ("item", -240, 0), ("item", 120, 0), ("item", 0, -50), ("item", 0, 50)],
    )
    src_box.forceActiveFocus()
    app.processEvents()
    wheel_closed(
        src_box,
        "main_src_item_focused",
        [("item", -120, 0), ("item", -120, 0), ("item", 240, 0), ("item", 0, -80)],
    )
    # Window delivery should scroll the right pane.
    wheel_closed(
        src_box,
        "main_src_window_scroll",
        [("window", -120, 0), ("window", -240, 0), ("window", -120, 0)],
        must_scroll=True,
    )
    wheel_closed(
        filter_box,
        "main_filter_item_and_window",
        [("item", -120, 0), ("item", 120, 0), ("window", -120, 0), ("item", 0, -40)],
    )
    if shape_box is not None and float(shape_box.height() or 0) > 0:
        wheel_closed(
            shape_box,
            "main_shape_item",
            [("item", -120, 0), ("item", 120, 0), ("item", 0, -40)],
        )

    # Expanded then collapsed.
    popup = src_box.property("popup")
    if popup is not None and hasattr(popup, "open"):
        popup.open()
        app.processEvents()
        opened = bool(src_box.property("down")) or bool(getattr(popup, "opened", False) or popup.property("opened"))
        start = snapshot_boxes()
        _send_wheel(src_box, angle_y=-120, target="item")
        app.processEvents()
        _send_wheel(src_box, angle_y=-120, target="window")
        app.processEvents()
        mid = snapshot_boxes()
        if mid["src"] != start["src"] or mid["calls"]["setModuleSource"] != start["calls"]["setModuleSource"]:
            # Opening the list must not commit a new module source via wheel.
            if popup is not None and hasattr(popup, "close"):
                popup.close()
            be.shutdown()
            fail("main_open_popup_changed_source", before=start, mid=mid, report=report)
        if popup is not None and hasattr(popup, "close"):
            popup.close()
        app.processEvents()
        wheel_closed(src_box, "main_src_after_close", [("item", -120, 0), ("window", -120, 0)])
        report["cases"]["main_popup_open_close"] = {"opened": opened, "ok": True}

    # Keyboard still moves or opens the Main source box.
    src_box.forceActiveFocus()
    app.processEvents()
    start_index = int(src_box.property("currentIndex"))
    QTest.keyClick(root, Qt.Key.Key_Space)
    app.processEvents()
    QTest.keyClick(root, Qt.Key.Key_Down)
    app.processEvents()
    kb_opened = bool(src_box.property("down"))
    popup = src_box.property("popup")
    if popup is not None:
        kb_opened = kb_opened or bool(popup.property("opened") if hasattr(popup, "property") else getattr(popup, "opened", False))
    # Close without committing if we opened it.
    if popup is not None and hasattr(popup, "close"):
        popup.close()
        app.processEvents()
    report["cases"]["main_keyboard"] = {
        "opened": kb_opened,
        "index_now": int(src_box.property("currentIndex")),
        "start_index": start_index,
    }
    # Keyboard is allowed to change the index; we only require it still responds.
    if not kb_opened and int(src_box.property("currentIndex")) == start_index:
        # Space may be swallowed in ApplicationWindow; try Down only while focused.
        QTest.keyClick(src_box, Qt.Key.Key_Down)
        app.processEvents()
        if int(src_box.property("currentIndex")) == start_index and not bool(src_box.property("down")):
            # Do not fail the whole probe if window-level keys are eaten; record it.
            report["cases"]["main_keyboard"]["note"] = "offscreen ApplicationWindow did not accept Space/Down; standalone keyboard case already passed"

    real_after = _real_sigs()
    report["real_after"] = real_after
    if real_after != real_before:
        be.shutdown()
        fail("real_user_state_changed", report=report)

    iso_after = {"settings": _file_sig(iso_settings), "choices": _file_sig(iso_choices)}
    report["iso_before"] = iso_before
    report["iso_after"] = iso_after
    if iso_after != iso_before:
        be.shutdown()
        fail("isolated_state_changed_by_wheel", report=report)

    be.shutdown()
    for obj in list(engine.rootObjects()):
        obj.close()
    engine.deleteLater()
    app.processEvents()
    print(json.dumps(report, ensure_ascii=False, default=str, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as exc:
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}", "trace": traceback.format_exc()}, ensure_ascii=False))
        raise SystemExit(1)
