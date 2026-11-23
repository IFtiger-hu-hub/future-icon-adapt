"""Offscreen GUI stays responsive when selecting apps and switching candidates."""

from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from iconadapt.constants import MODULE_APPS, THEME_INTERNAL_NAME
from iconadapt.mask import MaskSettings, save_settings
from iconadapt.persist import save_choices, upsert_selection
from tests.fixtures import make_desktop, make_theme, write
from tests.qtutil import ensure_qt_app

NORMAL = """<svg xmlns="http://www.w3.org/2000/svg" width="32" height="32" viewBox="0 0 32 32">
  <rect width="32" height="32" fill="#2ecc71"/>
</svg>
"""
BLUE = """<svg xmlns="http://www.w3.org/2000/svg" width="32" height="32" viewBox="0 0 32 32">
  <circle cx="16" cy="16" r="14" fill="#3daee9"/>
</svg>
"""
RED = """<svg xmlns="http://www.w3.org/2000/svg" width="32" height="32" viewBox="0 0 32 32">
  <rect x="4" y="4" width="24" height="24" fill="#da4453"/>
</svg>
"""


def _write_svg_theme(root: Path, name: str, icons: dict[str, str]) -> Path:
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
    for icon_name, body in icons.items():
        (tdir / rel / f"{icon_name}.svg").write_text(body, encoding="utf-8")
    return tdir


def _qml_dict(value):
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    to_variant = getattr(value, "toVariant", None)
    if callable(to_variant):
        converted = to_variant()
        return converted if isinstance(converted, dict) else {}
    return {}


def _pump(app, predicate, timeout=8.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.005)
        app.processEvents()
    return False


class Heartbeat:
    def __init__(self, app, interval_ms: int = 16):
        from PyQt6.QtCore import QTimer

        self.gaps = []
        self._last = time.monotonic()
        self._timer = QTimer()
        self._timer.setInterval(interval_ms)
        self._timer.timeout.connect(self._tick)
        self._timer.start()
        self._app = app

    def _tick(self):
        now = time.monotonic()
        self.gaps.append(now - self._last)
        self._last = now

    @property
    def max_gap(self) -> float:
        return max(self.gaps) if self.gaps else 0.0

    def stop(self):
        self._timer.stop()


class AppDetailResponsivenessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._app = ensure_qt_app()

    def setUp(self):
        self._backends = []
        self.tmp = tempfile.TemporaryDirectory(prefix="iconadapt-test-")
        self.root = Path(self.tmp.name)
        self.icons = self.root / "icons"
        self.apps_dir = self.root / "apps"
        self.apps_dir.mkdir()
        # Many same-name candidates across packs so first-click work is non-trivial.
        icons = {f"app-one": NORMAL}
        for i in range(12):
            icons[f"app-one"] = NORMAL  # one name per pack below
        _write_svg_theme(self.icons, "PackA", {"app-one": NORMAL, "app-two": BLUE})
        _write_svg_theme(self.icons, "PackB", {"app-one": BLUE, "app-two": RED})
        _write_svg_theme(self.icons, "PackC", {"app-one": RED})
        # More candidate volume: extra packs with the same name.
        for i in range(8):
            _write_svg_theme(self.icons, f"PackX{i}", {"app-one": NORMAL, "app-two": BLUE})
        make_theme(self.icons, "hicolor", "", {"folder": ["48x48/places"]})
        make_desktop(self.apps_dir, "one.desktop", name="One", icon="app-one")
        make_desktop(self.apps_dir, "two.desktop", name="Two", icon="app-two")
        self.iso = {
            "data_home": str(self.root / "data"),
            "config_home": str(self.root / "config"),
            "icon_dirs": [str(self.icons)],
            "app_dirs": [str(self.apps_dir)],
            "current_theme": "PackA",
            "current_desktop": "KDE",
        }

    def tearDown(self):
        for be in self._backends:
            try:
                be.shutdown()
            except Exception:
                pass
        self._backends.clear()
        self.tmp.cleanup()

    def _backend(self):
        from iconadapt.backend import IconAdaptBackend

        be = IconAdaptBackend(isolated=self.iso)
        self._backends.append(be)
        return be

    def test_request_does_not_block_and_stale_results_are_ignored(self):
        be = self._backend()
        ready = []
        be.appDetailReady.connect(lambda i, p: ready.append((i, json.loads(p))))
        hb = Heartbeat(self._app)
        t0 = time.monotonic()
        be.requestAppDetail("one")
        start_cost = time.monotonic() - t0
        self.assertLess(start_cost, 0.15, f"requestAppDetail itself blocked {start_cost:.3f}s")
        # Immediately switch to the other app; the first job must not overwrite it later.
        be.requestAppDetail("two")
        ok = _pump(
            self._app,
            lambda: any(
                i == "two" and not (d.get("detail_loading")) and "candidates" in d
                for i, d in ready
            ),
        )
        hb.stop()
        self.assertTrue(ok, f"timed out waiting for app two; events={[(i, d.get('detail_loading')) for i,d in ready]}")
        self.assertGreaterEqual(len(hb.gaps), 2, "event loop heartbeat did not run during load")
        self.assertLess(hb.max_gap, 0.8, f"event loop stalled for {hb.max_gap:.3f}s")
        last_two = [d for i, d in ready if i == "two"][-1]
        self.assertFalse(last_two.get("detail_loading"))
        self.assertTrue(last_two.get("candidates"), last_two)
        # After two is complete, a late result for one must not be the last selected payload
        # applied to the backend selection.
        self.assertEqual(be._selected_id, "two")
        late_one = [d for i, d in ready if i == "one" and not d.get("detail_loading")]
        if late_one:
            # If a complete payload for one arrived, it must have been before two finished
            # or ignored for the selected id. The latest complete event must be two.
            last_complete = [pair for pair in ready if not pair[1].get("detail_loading")][-1]
            self.assertEqual(last_complete[0], "two", ready[-5:])

    def test_sync_app_detail_still_returns_previews(self):
        be = self._backend()
        detail = json.loads(be.appDetailJson("one"))
        self.assertFalse(detail.get("detail_loading"))
        self.assertTrue(detail.get("candidates"), detail)
        self.assertTrue(
            any((c.get("preview_url") or "").startswith("data:image/png") for c in detail["candidates"]),
            detail["candidates"][0],
        )

    def test_save_selection_does_not_rescan_or_drop_module_cache(self):
        be = self._backend()
        json.loads(be.modulesJson)
        self.assertIsNotNone(be._modules_overview_cache)
        src = self.icons / "PackA" / "scalable" / "apps" / "app-one.svg"
        be.saveSelection("one", "app-one", "PackA", str(src))
        self.assertIsNotNone(be._modules_overview_cache)
        rows = json.loads(be.appsJson)
        one = next(r for r in rows if r["desktop_id"] == "one")
        self.assertTrue(one["has_selection"])
        # Isolated choices only — not the user's real choices.json.
        self.assertTrue(str(be.state_dir()).startswith(str(self.root)))

    def test_offscreen_qml_click_path_stays_responsive(self):
        from PyQt6.QtCore import QUrl, QTimer
        from PyQt6.QtQml import QQmlApplicationEngine

        from iconadapt.backend import IconAdaptBackend
        from iconadapt.gui import qml_dir

        be = self._backend()
        engine = QQmlApplicationEngine()
        engine.rootContext().setContextProperty("iconAdapt", be)
        engine.load(QUrl.fromLocalFile(str(qml_dir() / "Main.qml")))
        self.assertTrue(engine.rootObjects(), "QML failed to load")
        root = engine.rootObjects()[0]
        hb = Heartbeat(self._app)
        # Drive the same slots the list click uses.
        root.setProperty("selectedId", "one")
        be.requestAppDetail("one")
        ok = _pump(
            self._app,
            lambda: _qml_dict(root.property("detail")).get("desktop_id") == "one"
            and not _qml_dict(root.property("detail")).get("detail_loading"),
        )
        # Rapid switch.
        root.setProperty("selectedId", "two")
        be.requestAppDetail("two")
        ok2 = _pump(
            self._app,
            lambda: _qml_dict(root.property("detail")).get("desktop_id") == "two"
            and not _qml_dict(root.property("detail")).get("detail_loading"),
        )
        hb.stop()
        self.assertTrue(ok, _qml_dict(root.property("detail")))
        self.assertTrue(ok2, _qml_dict(root.property("detail")))
        self.assertGreaterEqual(len(hb.gaps), 2)
        self.assertLess(hb.max_gap, 0.8, f"event loop stalled for {hb.max_gap:.3f}s")
        detail = _qml_dict(root.property("detail"))
        self.assertEqual(detail.get("desktop_id"), "two")
        self.assertFalse(detail.get("detail_loading"))
        del engine


if __name__ == "__main__":
    unittest.main()
