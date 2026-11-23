"""Kirigami / Qt Quick GUI loader."""

from __future__ import annotations

import sys
from pathlib import Path

from PyQt6.QtCore import QTimer, QUrl
from PyQt6.QtGui import QGuiApplication, QIcon
from PyQt6.QtQml import QQmlApplicationEngine

from .backend import IconAdaptBackend
from .constants import LAUNCHER_DESKTOP_ID, LAUNCHER_ICON_NAME, LAUNCHER_MENU_NAME, THEME_DISPLAY_NAME, TOOL_VERSION
from .svgutil import install_qt_svg_log_filter


def qml_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "qml"


def run_gui(*, quit_after_load: bool = False) -> int:
    app = QGuiApplication(sys.argv)
    app.setApplicationName(LAUNCHER_DESKTOP_ID)
    app.setApplicationDisplayName(LAUNCHER_MENU_NAME)
    app.setApplicationVersion(TOOL_VERSION)
    app.setDesktopFileName(LAUNCHER_DESKTOP_ID)
    theme_icon = QIcon.fromTheme(LAUNCHER_ICON_NAME)
    if not theme_icon.isNull():
        app.setWindowIcon(theme_icon)
    install_qt_svg_log_filter()
    engine = QQmlApplicationEngine()
    backend = IconAdaptBackend()
    engine.rootContext().setContextProperty("iconAdapt", backend)
    qml_file = qml_dir() / "Main.qml"
    if not qml_file.is_file():
        print(f"missing QML: {qml_file}", file=sys.stderr)
        return 1
    engine.load(QUrl.fromLocalFile(str(qml_file)))
    # Kirigami/QML may replace the Qt message handler during load.
    install_qt_svg_log_filter()
    if not engine.rootObjects():
        print("QML failed to load", file=sys.stderr)
        return 1
    if quit_after_load:
        QTimer.singleShot(400, app.quit)
    try:
        return app.exec()
    finally:
        backend.shutdown()
