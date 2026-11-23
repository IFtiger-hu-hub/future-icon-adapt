"""Async generate/install must not block the GUI thread."""

from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from iconadapt.backend import IconAdaptBackend
from iconadapt.constants import OWNED_MARKER, THEME_INTERNAL_NAME
from iconadapt.export import ExportError
from iconadapt.mask import MaskError
from iconadapt.safeio import is_tool_owned, write_owned_marker
from tests.fixtures import make_desktop, make_theme
from tests.jobutil import wait_theme_job
from tests.qtutil import ensure_qt_app

SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 48 48">
  <rect width="48" height="48" rx="8" fill="#2b8cee"/>
  <circle cx="24" cy="24" r="12" fill="#ffffff"/>
</svg>
"""


def _write_svg_pack(root: Path, name: str, icons: list[str], extra: bool = True) -> Path:
    tdir = root / name
    dirs = ["scalable/apps"]
    if extra:
        dirs += ["scalable/places"]
    lines = [
        "[Icon Theme]",
        f"Name={name}",
        "Comment=test",
        "Inherits=hicolor",
        "Directories=" + ",".join(dirs),
        "",
    ]
    for d in dirs:
        ctx = "Places" if "places" in d else "Applications"
        lines += [f"[{d}]", "Size=48", f"Context={ctx}", "Type=Scalable", "MinSize=1", "MaxSize=256", ""]
    (tdir / "index.theme").parent.mkdir(parents=True, exist_ok=True)
    (tdir / "index.theme").write_text("\n".join(lines) + "\n", encoding="utf-8")
    for icon in icons:
        p = tdir / "scalable" / "apps" / f"{icon}.svg"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(SVG, encoding="utf-8")
    if extra:
        p = tdir / "scalable" / "places" / "folder.svg"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(SVG, encoding="utf-8")
    return tdir


class Heartbeat:
    def __init__(self, app, interval_ms: int = 16):
        from PyQt6.QtCore import QTimer

        self.gaps = []
        self.ticks = 0
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
        self.ticks += 1

    @property
    def max_gap(self) -> float:
        return max(self.gaps) if self.gaps else 0.0

    def stop(self):
        self._timer.stop()


class ThemeJobTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        cls._app = ensure_qt_app()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="iconadapt-themejob-")
        self.root = Path(self.tmp.name)
        self.icons = self.root / "icons"
        self.apps_dir = self.root / "apps"
        self.apps_dir.mkdir()
        self.n = 24
        names = [f"app-{i:03d}" for i in range(self.n)]
        _write_svg_pack(self.icons, "PackA", names, extra=True)
        make_theme(self.icons, "hicolor", "", {"folder": ["48x48/places"]})
        for name in names:
            make_desktop(self.apps_dir, f"{name}.desktop", name=name, icon=name)
        self.iso = {
            "data_home": str(self.root / "data"),
            "config_home": str(self.root / "config"),
            "cache_home": str(self.root / "cache"),
            "icon_dirs": [str(self.icons)],
            "app_dirs": [str(self.apps_dir)],
            "current_theme": "PackA",
            "current_desktop": "KDE",
        }
        Path(self.iso["cache_home"]).mkdir(parents=True, exist_ok=True)
        self.backends = []

    def tearDown(self):
        for be in self.backends:
            try:
                be.shutdown()
            except Exception:
                pass
        self.tmp.cleanup()

    def _backend(self) -> IconAdaptBackend:
        be = IconAdaptBackend(isolated=self.iso)
        self.backends.append(be)
        be.setMaskJson(json.dumps({
            "enabled": True,
            "shape": "circle",
            "custom_path": "",
            "background": "#00000000",
            "padding": 0.0,
        }))
        for mid in ("apps", "places", "mimetypes", "actions", "status", "other"):
            be.setModuleSource(mid, "PackA" if mid in {"apps", "places"} else "system")
        return be

    def test_install_slot_returns_quickly_and_heartbeat_continues(self):
        be = self._backend()
        stages = []

        def on_prog():
            stages.append((be.jobStage, be.jobDone, be.jobTotal, be.jobLabel))

        be.jobProgress.connect(on_prog)
        hb = Heartbeat(self._app)
        # Warm the timer so we know it works.
        t_warm = time.monotonic()
        while time.monotonic() - t_warm < 0.08:
            self._app.processEvents()
            time.sleep(0.004)
        ticks_before = hb.ticks
        result = wait_theme_job(be, "installTheme", timeout=60, app=self._app)
        hb.stop()
        self.assertTrue(result.get("ok"), result)
        self.assertLess(result.get("_start_sec", 1), 0.25, result)
        self.assertGreater(hb.ticks, ticks_before, "GUI timer must keep firing during install")
        self.assertLess(hb.max_gap, 0.6, f"GUI heartbeat stalled: max_gap={hb.max_gap:.3f}s")
        dest = Path(result["dest"])
        self.assertTrue(str(dest).startswith(str(self.root)))
        self.assertTrue(is_tool_owned(dest))
        self.assertGreater(result.get("icons") or 0, 0)
        seen_stages = {s[0] for s in stages if s[0]}
        self.assertTrue({"export", "render"} & seen_stages, stages[:8])
        self.assertTrue({"copy", "commit", "refresh"} & seen_stages, stages)
        # Totals, when known, must be real counts, not a made-up 100%.
        totals = [s[2] for s in stages if s[2] > 0]
        self.assertTrue(totals)

    def test_generate_does_not_install_and_reentry_is_rejected(self):
        be = self._backend()
        started = json.loads(be.generateTheme())
        self.assertTrue(started.get("started"), started)
        self.assertTrue(be.jobBusy)
        second = json.loads(be.installTheme())
        self.assertFalse(second.get("started"))
        self.assertFalse(second.get("ok"))
        self.assertIn("请稍候", second.get("error") or "")
        end = time.monotonic() + 60
        while time.monotonic() < end and be.jobBusy:
            self._app.processEvents()
            time.sleep(0.01)
        self.assertFalse(be.jobBusy)
        build = Path(self.iso["data_home"]) / "icon-adapt" / "build" / THEME_INTERNAL_NAME
        installed = Path(self.iso["data_home"]) / "icons" / THEME_INTERNAL_NAME
        self.assertTrue(is_tool_owned(build))
        self.assertFalse(installed.exists())

    def test_failure_rolls_back_existing_install(self):
        be = self._backend()
        first = wait_theme_job(be, "installTheme", timeout=60, app=self._app)
        self.assertTrue(first.get("ok"), first)
        dest = Path(first["dest"])
        sentinel = dest / "keep-me.txt"
        # The dest is owned; writing a sidecar next to index is enough to detect a replace.
        marker = dest / "index.theme"
        before = marker.read_bytes() if marker.is_file() else b""
        # Inject a failure during the next export, before dest is replaced.
        orig = None
        from iconadapt import mask as maskmod

        orig = maskmod.apply_mask_to_file
        calls = {"n": 0}

        def boom(*a, **k):
            calls["n"] += 1
            if calls["n"] > 2:
                raise MaskError("injected export failure")
            return orig(*a, **k)

        with mock.patch("iconadapt.mask.apply_mask_to_file", side_effect=boom), mock.patch(
            "iconadapt.export.apply_mask_to_file", side_effect=boom
        ):
            second = wait_theme_job(be, "installTheme", timeout=60, app=self._app)
        self.assertFalse(second.get("ok"), second)
        self.assertIn("injected", second.get("error") or "")
        self.assertTrue(dest.is_dir())
        self.assertTrue(is_tool_owned(dest))
        self.assertEqual(marker.read_bytes() if marker.is_file() else b"", before)
        leftover = [p.name for p in dest.parent.iterdir() if p.name.startswith(f".{THEME_INTERNAL_NAME}.")]
        self.assertFalse(leftover, leftover)

    def test_cancel_at_safe_boundary_does_not_install(self):
        be = self._backend()
        from iconadapt import mask as maskmod

        orig = maskmod.apply_mask_to_file

        def slow(*a, **k):
            time.sleep(0.03)
            return orig(*a, **k)

        with mock.patch("iconadapt.mask.apply_mask_to_file", side_effect=slow), mock.patch(
            "iconadapt.export.apply_mask_to_file", side_effect=slow
        ):
            started = json.loads(be.installTheme())
            self.assertTrue(started.get("started"), started)
            t0 = time.monotonic()
            while time.monotonic() - t0 < 0.2 and not be.jobBusy:
                self._app.processEvents()
                time.sleep(0.005)
            cancelled = json.loads(be.cancelThemeJob())
            self.assertTrue(cancelled.get("ok"), cancelled)
            end = time.monotonic() + 30
            result_holder = {}

            def on_fin(raw):
                result_holder.update(json.loads(raw))

            be.jobFinished.connect(on_fin)
            while time.monotonic() < end and be.jobBusy:
                self._app.processEvents()
                time.sleep(0.01)
            for _ in range(10):
                self._app.processEvents()
                time.sleep(0.005)
        self.assertFalse(be.jobBusy)
        self.assertTrue(result_holder.get("cancelled") or not result_holder.get("ok"), result_holder)
        installed = Path(self.iso["data_home"]) / "icons" / THEME_INTERNAL_NAME
        # Cancel before commit must not publish a new user theme.
        # If the job finished too fast and already committed, this would fail — require cancel won.
        self.assertFalse(installed.exists(), "cancel should discard staging and leave dest uninstalled")

    def test_shutdown_during_job_does_not_hang(self):
        be = self._backend()
        started = json.loads(be.installTheme())
        self.assertTrue(started.get("started"), started)
        t0 = time.monotonic()
        be.shutdown()
        elapsed = time.monotonic() - t0
        self.assertLess(elapsed, 30, f"shutdown hung for {elapsed:.1f}s")
        self.assertFalse(be.jobBusy)
        # Backend is shut down; do not use it further.


if __name__ == "__main__":
    unittest.main()
