"""Shared offscreen Qt app for tests that need QImage/QML/backend."""

from __future__ import annotations

import os
import sys


def ensure_qt_app():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtCore import QCoreApplication
    from PyQt6.QtGui import QGuiApplication

    inst = QCoreApplication.instance()
    if inst is None:
        inst = QGuiApplication(sys.argv if sys.argv else ["iconadapt-tests"])
    return inst
