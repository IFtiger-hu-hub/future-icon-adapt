"""QSvgRenderer + QImage must be safe and pixel-identical on a dedicated QThread."""

from __future__ import annotations

import os
import threading
import time
import unittest
from pathlib import Path

from PyQt6.QtCore import QByteArray, QObject, QThread, pyqtSignal, pyqtSlot
from PyQt6.QtGui import QImage, QPainter
from PyQt6.QtSvg import QSvgRenderer

from tests.qtutil import ensure_qt_app

SVG = b"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 48 48">
  <defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">
    <stop offset="0" stop-color="#2b8cee"/><stop offset="1" stop-color="#16325c"/>
  </linearGradient></defs>
  <rect width="48" height="48" rx="8" fill="url(#g)"/>
  <circle cx="24" cy="24" r="12" fill="#fff"/>
</svg>
"""


def _raw(data: bytes, size: int) -> QImage:
    renderer = QSvgRenderer(QByteArray(data))
    img = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(0)
    painter = QPainter(img)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    renderer.render(painter)
    painter.end()
    return img


def _fp(img: QImage) -> tuple:
    bits = img.bits()
    bits.setsize(img.sizeInBytes())
    data = bytes(bits)
    return (img.width(), img.height(), img.format(), hash(data), data[:32])


class _Worker(QObject):
    done = pyqtSignal()

    def __init__(self, jobs):
        super().__init__()
        self.jobs = jobs
        self.thread_id = None
        self.error = None
        self.results = []

    @pyqtSlot()
    def run(self) -> None:
        try:
            self.thread_id = threading.get_ident()
            from iconadapt.mask import MaskSettings, apply_mask_to_image

            for kind, payload, size in self.jobs:
                if kind == "bytes":
                    img = _raw(payload, size)
                else:
                    settings = MaskSettings(enabled=True, shape="circle", background="#00000000", padding=0.0)
                    img = apply_mask_to_image(Path(payload), size, settings)
                self.results.append(_fp(img))
        except Exception as exc:  # noqa: BLE001
            self.error = f"{type(exc).__name__}: {exc}"
        self.done.emit()


class SvgWorkerThreadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        cls._app = ensure_qt_app()

    def test_qimage_svg_matches_gui_thread(self):
        from iconadapt.mask import MaskSettings, apply_mask_to_image, ensure_gui_app

        ensure_gui_app()
        tmp = Path(self._app.applicationDirPath() if False else "/tmp")
        # Use in-memory SVG plus a temp file so apply_mask_to_image is also compared.
        import tempfile

        with tempfile.TemporaryDirectory(prefix="iconadapt-svg-thread-") as raw:
            path = Path(raw) / "icon.svg"
            path.write_bytes(SVG)
            jobs = [("bytes", SVG, 48), ("bytes", SVG, 256), ("file", str(path), 48), ("file", str(path), 256)]
            gui_id = threading.get_ident()
            gui = []
            for kind, payload, size in jobs:
                if kind == "bytes":
                    gui.append(_fp(_raw(payload, size)))
                else:
                    settings = MaskSettings(enabled=True, shape="circle", background="#00000000", padding=0.0)
                    gui.append(_fp(apply_mask_to_image(Path(payload), size, settings)))

            worker = _Worker(jobs)
            thread = QThread()
            worker.moveToThread(thread)
            finished = {"ok": False}

            def mark():
                finished["ok"] = True

            worker.done.connect(mark)
            thread.started.connect(worker.run)
            worker.done.connect(thread.quit)
            thread.start()
            end = time.monotonic() + 20
            while time.monotonic() < end and thread.isRunning():
                self._app.processEvents()
                time.sleep(0.005)
            thread.wait(3000)
            self.assertIsNone(worker.error, worker.error)
            self.assertNotEqual(worker.thread_id, gui_id)
            self.assertEqual(len(worker.results), len(gui))
            self.assertEqual(worker.results, gui)
            thread.deleteLater()
            self._app.processEvents()


if __name__ == "__main__":
    unittest.main()
