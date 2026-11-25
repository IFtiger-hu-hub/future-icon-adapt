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


def find_named_quick_items(root, name):
    """Find QQuickItems by objectName, including Repeater delegates.

    Repeater delegates parented into a ColumnLayout show up in childItems()
    and do not always appear in QObject.children() / findChildren().
    Keep object references while walking so SIP wrappers are not reused.
    """
    from PyQt6 import sip
    from PyQt6.QtQuick import QQuickItem

    found, seen, kept = [], set(), []

    def _ptr(obj):
        try:
            return int(sip.unwrapinstance(obj))
        except Exception:
            return id(obj)

    def walk(obj):
        if obj is None:
            return
        p = _ptr(obj)
        if p in seen:
            return
        seen.add(p)
        kept.append(obj)
        try:
            if obj.objectName() == name:
                item = obj if isinstance(obj, QQuickItem) else sip.cast(obj, QQuickItem)
                found.append(item)
        except Exception:
            pass
        kids = []
        if hasattr(obj, "childItems"):
            try:
                kids.extend(list(obj.childItems()))
            except Exception:
                pass
        try:
            kids.extend(list(obj.children()))
        except Exception:
            pass
        for child in kids:
            walk(child)

    walk(root)
    return found
