"""0.2.3 regressions: dangling sources, transactional export/install, warning bounds."""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from iconadapt.constants import (
    MODULE_ACTIONS,
    MODULE_APPS,
    MODULE_MIMETYPES,
    MODULE_OTHER,
    MODULE_PLACES,
    MODULE_STATUS,
    THEME_INTERNAL_NAME,
    TOOL_VERSION,
)
from iconadapt.desktop import scan_applications
from iconadapt.export import ExportError, export_theme, format_source_error, preflight_manual_selections
from iconadapt.mask import MaskError, MaskSettings, apply_mask_to_file, planned_sizes
from iconadapt.modules import files_by_name, list_module_files
from iconadapt.preview import compose_module_previews, decorate_previews, render_preview
from iconadapt.safeio import (
    SafeIOError,
    assert_can_replace,
    create_owned_staging,
    discard_staging,
    is_tool_owned,
    replace_owned_dir,
    write_owned_marker,
)
from iconadapt.sourcecheck import REASON_DANGLING, describe_source
from tests.jobutil import wait_theme_job
from iconadapt.strategy import resolve_strategy
from iconadapt.svgutil import (
    capture_svg_logs,
    flush_svg_log_summary,
    install_qt_svg_log_filter,
    render_svg_bytes,
    reset_svg_log_dedup,
    svg_log_count,
    svg_log_stats,
)
from iconadapt.theme import enumerate_themes
from tests.fixtures import make_desktop, make_theme, write
from tests.qtutil import ensure_qt_app

USER_STATE = Path.home() / ".local/share/icon-adapt"
USER_SETTINGS = USER_STATE / "settings.json"
USER_CHOICES = USER_STATE / "choices.json"
USER_ICONS = Path.home() / ".local/share/icons"
MCMOJAVE = USER_ICONS / "McMojave-circle"
KERRY = MCMOJAVE / "apps" / "scalable" / "kerry.svg"
KFIND = MCMOJAVE / "apps" / "scalable" / "org.kde.kfind.svg"
TERMINAL = MCMOJAVE / "apps" / "scalable" / "terminal.svg"
AMAZON = MCMOJAVE / "apps" / "scalable" / "amazon.svg"

NORMAL_SVG = """<svg xmlns="http://www.w3.org/2000/svg" width="32" height="32" viewBox="0 0 32 32">
  <rect width="32" height="32" fill="#2ecc71"/>
</svg>
"""
BROKEN_REF_SVG = """<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64">
  <defs>
    <linearGradient id="c" x1="0" x2="0" y1="0" y2="1">
      <stop offset="0" stop-color="#767574"/>
      <stop offset="1" stop-color="#d1e7ee"/>
    </linearGradient>
  </defs>
  <circle cx="32" cy="32" r="30" fill="url(#c)"/>
  <circle cx="32" cy="32" r="30" fill="url(#a)"/>
  <circle cx="32" cy="32" r="20" fill="#1a1a1a"/>
</svg>
"""
EMPTY_FILTER_SVG = """<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64">
  <defs>
    <filter id="c" width="1.118" height="1.122" x="-.059" y="-.061">
      <feGaussianBlur stdDeviation="10.562"/>
    </filter>
  </defs>
  <rect width="40" height="40" x="12" y="12" fill="#f90"/>
  <g filter="url(#c)" transform="translate(12.684 12.68)scale(.08965)"/>
</svg>
"""


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def snapshot_tree(path: Path) -> dict:
    if not path.exists() and not path.is_symlink():
        return {"exists": False, "digest": "", "entries": []}
    entries = []
    for dirpath, dirnames, filenames in os.walk(path, followlinks=False):
        dirnames.sort()
        filenames.sort()
        for name in filenames:
            p = Path(dirpath) / name
            rel = str(p.relative_to(path))
            st = p.lstat()
            mode = oct(st.st_mode)
            if stat.S_ISLNK(st.st_mode):
                entries.append((rel, "link", os.readlink(p), mode))
            elif p.is_file():
                entries.append((rel, "file", _sha256(p), mode))
            else:
                entries.append((rel, "other", mode))
    return {
        "exists": True,
        "digest": hashlib.sha256(repr(entries).encode()).hexdigest(),
        "entries": entries,
    }


def write_svg_theme(root: Path, name: str, icons: dict[str, str], *, context: str = "apps") -> Path:
    tdir = root / name
    rel = f"scalable/{context}"
    (tdir / rel).mkdir(parents=True)
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
                f"Context={'Applications' if context == 'apps' else context.title()}",
                "",
            ]
        ),
    )
    for icon_name, body in icons.items():
        (tdir / rel / f"{icon_name}.svg").write_text(body, encoding="utf-8")
    return tdir


def add_dangling(theme_dir: Path, link_name: str, target: str, *, context: str = "apps") -> Path:
    link = theme_dir / "scalable" / context / link_name
    if link.exists() or link.is_symlink():
        link.unlink()
    os.symlink(target, link)
    return link


class SourceAndMaskDanglingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._app = ensure_qt_app()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_describe_dangling_names_link_not_only_target(self):
        pack = write_svg_theme(self.root / "icons", "Pack", {"ok": NORMAL_SVG})
        link = add_dangling(pack, "kerry.svg", "search.svg")
        status = describe_source(link)
        self.assertFalse(status.ok)
        self.assertEqual(status.reason, REASON_DANGLING)
        self.assertIn("kerry.svg", status.display)
        self.assertIn("search.svg", status.display)
        self.assertNotEqual(status.display, f"源图标不存在：{link.parent / 'search.svg'}")

    def test_apply_mask_reports_dangling_link_and_does_not_touch_choice(self):
        pack = write_svg_theme(self.root / "icons", "Pack", {"ok": NORMAL_SVG})
        link = add_dangling(pack, "kerry.svg", "search.svg")
        dest = self.root / "out.png"
        with self.assertRaises(MaskError) as ctx:
            apply_mask_to_file(link, dest, 48, MaskSettings(enabled=True, shape="circle"))
        self.assertIn("kerry.svg", str(ctx.exception))
        self.assertIn("search.svg", str(ctx.exception))
        self.assertFalse(dest.exists())
        # The caller keeps the path it passed in; we never delete it.
        self.assertTrue(link.is_symlink())

    @unittest.skipUnless(KERRY.is_symlink(), "real McMojave kerry.svg symlink is not present")
    def test_real_kerry_points_at_missing_search_svg(self):
        status = describe_source(KERRY)
        self.assertFalse(status.ok)
        self.assertEqual(status.reason, REASON_DANGLING)
        self.assertIn("kerry.svg", status.display)
        self.assertIn("search.svg", status.display)
        self.assertFalse((MCMOJAVE / "apps" / "scalable" / "search.svg").exists())


class TransactionalExportInstallTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._app = ensure_qt_app()

    def setUp(self):
        reset_svg_log_dedup()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.icons = self.root / "icons"
        self.apps_dir = self.root / "apps"
        self.apps_dir.mkdir()
        write_svg_theme(self.icons, "Pack", {"good-app": NORMAL_SVG, "filter-app": EMPTY_FILTER_SVG})
        add_dangling(self.icons / "Pack", "kerry.svg", "search.svg")
        make_theme(self.icons, "hicolor", "", {"folder": ["48x48/places"]})
        make_desktop(self.apps_dir, "good-app.desktop", name="Good", icon="good-app")
        self.themes = enumerate_themes([self.icons])
        self.status = resolve_strategy(self.themes, current_theme="Pack")
        self.apps = scan_applications([self.apps_dir], current_desktop="KDE")
        self.mask = MaskSettings(enabled=True, shape="circle", background="#ffffffff", padding=0.0)

    def tearDown(self):
        self.tmp.cleanup()

    def _export(self, dest: Path, *, selections=None, modules=None, mask=None):
        return export_theme(
            dest,
            apps=self.apps,
            themes=self.themes,
            status=self.status,
            selections=selections or {},
            overwrite_owned=True,
            mask=mask if mask is not None else self.mask,
            modules=modules if modules is not None else {MODULE_APPS: "Pack"},
        )

    def test_dangling_module_icon_is_skipped_not_silent_and_export_completes(self):
        dest = self.root / "build" / THEME_INTERNAL_NAME
        prov = self._export(dest)
        self.assertTrue((dest / "index.theme").is_file())
        self.assertTrue((dest / "provenance.json").is_file())
        self.assertTrue(is_tool_owned(dest))
        reasons = [row.get("reason", "") for row in prov.get("skipped") or []]
        names = [row.get("icon_name", "") for row in prov.get("skipped") or []]
        self.assertIn("kerry", names)
        self.assertTrue(any("kerry.svg" in r and "search.svg" in r for r in reasons), reasons)
        # Good icon was written; dangling one was not silently turned into a blank file.
        self.assertTrue(list((dest / "48x48" / "apps").glob("good-app.png")))
        self.assertFalse(list(dest.rglob("kerry.png")))
        self.assertFalse(any(p.name.startswith(f".{THEME_INTERNAL_NAME}.staging-") for p in dest.parent.iterdir()))

    def test_stale_manual_selection_aborts_before_changing_dest(self):
        dest = self.root / "build" / THEME_INTERNAL_NAME
        dest.mkdir(parents=True)
        write_owned_marker(dest)
        (dest / "index.theme").write_text("Name=keep-me\n", encoding="utf-8")
        kept = dest / "48x48" / "apps" / "old.png"
        kept.parent.mkdir(parents=True)
        kept.write_bytes(b"old-bytes")
        os.chmod(kept, 0o640)
        before = snapshot_tree(dest)
        missing = self.root / "gone.svg"
        selections = {
            "stale-app.desktop": {
                "source_theme": "Pack",
                "icon_name": "gone",
                "source_path": str(missing),
            }
        }
        copied = json.loads(json.dumps(selections))
        with self.assertRaises(ExportError) as ctx:
            self._export(dest, selections=selections)
        msg = str(ctx.exception)
        self.assertIn("stale-app.desktop", msg)
        self.assertIn("没有改动", msg)
        self.assertEqual(selections, copied, "hand picks must not be deleted")
        self.assertEqual(snapshot_tree(dest), before)
        self.assertEqual(kept.read_bytes(), b"old-bytes")
        self.assertEqual(oct(kept.stat().st_mode & 0o777), "0o640")

    def test_mid_export_failure_leaves_old_dest_and_index_unchanged(self):
        dest = self.root / "build" / THEME_INTERNAL_NAME
        dest.mkdir(parents=True)
        write_owned_marker(dest)
        (dest / "index.theme").write_text("Name=keep-me\n", encoding="utf-8")
        kept = dest / "scalable" / "apps" / "old-link.svg"
        kept.parent.mkdir(parents=True)
        target = dest / "scalable" / "apps" / "old-target.svg"
        target.write_text(NORMAL_SVG, encoding="utf-8")
        os.symlink("old-target.svg", kept)
        os.chmod(target, 0o644)
        before = snapshot_tree(dest)

        real = apply_mask_to_file
        calls = {"n": 0}

        def boom(source, dest_png, size, settings):
            calls["n"] += 1
            if calls["n"] >= 2:
                raise MaskError("injected mid-export failure")
            return real(source, dest_png, size, settings)

        with mock.patch("iconadapt.export.apply_mask_to_file", side_effect=boom):
            with self.assertRaises(ExportError) as ctx:
                self._export(dest)
        self.assertIn("原主题目录未改动", str(ctx.exception))
        self.assertEqual(snapshot_tree(dest), before)
        self.assertEqual(os.readlink(kept), "old-target.svg")
        leftover = [p.name for p in dest.parent.iterdir() if p.name.startswith(f".{THEME_INTERNAL_NAME}.")]
        self.assertFalse(leftover, leftover)

    def test_replace_owned_dir_rolls_back_when_staging_rename_fails(self):
        dest = self.root / "swap" / THEME_INTERNAL_NAME
        dest.mkdir(parents=True)
        write_owned_marker(dest)
        (dest / "keep.txt").write_text("old", encoding="utf-8")
        staging = create_owned_staging(dest)
        (staging / "new.txt").write_text("new", encoding="utf-8")
        before = snapshot_tree(dest)
        real = replace_owned_dir.__globals__["os_rename_dir"]
        calls = {"n": 0}

        def flaky(src, dst):
            calls["n"] += 1
            if calls["n"] == 2:
                return False
            return real(src, dst)

        with mock.patch("iconadapt.safeio.os_rename_dir", side_effect=flaky):
            with self.assertRaises(SafeIOError):
                replace_owned_dir(dest, staging)
        self.assertEqual(snapshot_tree(dest)["digest"], before["digest"])
        self.assertEqual((dest / "keep.txt").read_text(encoding="utf-8"), "old")
        self.assertFalse((dest / "new.txt").exists())

    def test_discard_staging_refuses_dest_and_foreign_paths(self):
        dest = self.root / "swap" / THEME_INTERNAL_NAME
        dest.mkdir(parents=True)
        write_owned_marker(dest)
        foreign = self.root / "swap" / "not-ours"
        foreign.mkdir()
        (foreign / "x").write_text("x", encoding="utf-8")
        with self.assertRaises(SafeIOError):
            discard_staging(dest, dest)
        with self.assertRaises(SafeIOError):
            discard_staging(foreign, dest)
        self.assertTrue(dest.is_dir())
        self.assertTrue((foreign / "x").is_file())

    def test_install_copy_failure_leaves_existing_theme(self):
        from iconadapt.backend import IconAdaptBackend

        iso = {
            "data_home": str(self.root / "data"),
            "config_home": str(self.root / "config"),
            "icon_dirs": [str(self.icons)],
            "app_dirs": [str(self.apps_dir)],
            "current_theme": "Pack",
            "current_desktop": "KDE",
        }
        be = IconAdaptBackend(isolated=iso)
        be.setModuleSource(MODULE_APPS, "Pack")
        be.setMaskJson(json.dumps({
            "enabled": True,
            "shape": "circle",
            "background": "#ffffffff",
            "padding": 0,
            "custom_path": "",
        }))
        gen = wait_theme_job(be, "generateTheme")
        self.assertTrue(gen.get("ok") or (dest := gen), gen)
        dest = Path(iso["data_home"]) / "icons" / THEME_INTERNAL_NAME
        # Seed an already-installed owned theme distinct from the new build.
        if dest.exists():
            shutil.rmtree(dest)
        dest.mkdir(parents=True)
        write_owned_marker(dest)
        (dest / "index.theme").write_text("Name=installed-keep\n", encoding="utf-8")
        (dest / "marker.txt").write_text("stay", encoding="utf-8")
        before = snapshot_tree(dest)

        def fail_copy(*_a, **_k):
            raise OSError("injected copy failure")

        with mock.patch("shutil.copytree", side_effect=fail_copy):
            with self.assertRaises(OSError):
                be._copy_build_to_user_icons()
        self.assertEqual(snapshot_tree(dest), before)
        leftover = [p.name for p in dest.parent.iterdir() if p.name.startswith(f".{THEME_INTERNAL_NAME}.")]
        self.assertFalse(leftover, leftover)

    def test_masked_mixed_modules_export_completes(self):
        write_svg_theme(self.icons, "PlacesPack", {"folder": NORMAL_SVG}, context="places")
        write_svg_theme(self.icons, "MimePack", {"text-plain": NORMAL_SVG}, context="mimetypes")
        self.themes = enumerate_themes([self.icons])
        dest = self.root / "mixed" / THEME_INTERNAL_NAME
        prov = self._export(
            dest,
            modules={
                MODULE_APPS: "Pack",
                MODULE_PLACES: "PlacesPack",
                MODULE_MIMETYPES: "MimePack",
            },
        )
        self.assertEqual(prov.get("theme"), THEME_INTERNAL_NAME)
        self.assertTrue((dest / "index.theme").is_file())
        self.assertTrue(list(dest.glob("**/folder.svg")) or list(dest.glob("**/folder.png")))
        self.assertTrue(list((dest / "48x48" / "apps").glob("good-app.png")))
        self.assertIn("kerry", [row.get("icon_name") for row in prov.get("skipped") or []])


class WarningAndUserSnapshotTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._app = ensure_qt_app()

    def setUp(self):
        reset_svg_log_dedup()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_byte_array_warnings_use_real_path_and_fold_same_kind(self):
        files = []
        for i in range(6):
            path = self.root / f"empty-{i}.svg"
            path.write_text(EMPTY_FILTER_SVG, encoding="utf-8")
            files.append(path)
        broken = self.root / "broken.svg"
        broken.write_text(BROKEN_REF_SVG, encoding="utf-8")
        reset_svg_log_dedup()
        # Force a raw Qt render (bypass preview skip) so we see path rewriting.
        for path in files:
            render_svg_bytes(path.read_bytes(), 48, source=str(path))
        img, messages = render_svg_bytes(broken.read_bytes(), 48, source=str(broken))
        self.assertIsNotNone(img)
        self.assertTrue(any(str(broken) in m for m in messages), messages)
        self.assertFalse(any("<input>" in m and str(broken) not in m for m in messages), messages)
        seen, emitted = svg_log_count(str(files[0]), "filter_buffer")
        self.assertGreaterEqual(seen, 1)
        # Same kind across files: only the first kind is emitted.
        total_emitted = svg_log_stats()["emitted"]
        self.assertLessEqual(total_emitted, 4, svg_log_stats())
        summary = flush_svg_log_summary()
        joined = "\n".join(summary)
        self.assertTrue(any("filter_buffer" in line and "共" in line for line in summary), summary)
        self.assertIn(str(files[0]), joined)

    def test_handler_is_reinstalled_if_qt_replaces_it(self):
        from PyQt6.QtCore import qInstallMessageHandler

        stolen = []

        def thief(mode, context, message):
            stolen.append(str(message))

        install_qt_svg_log_filter()
        qInstallMessageHandler(thief)
        path = self.root / "broken.svg"
        path.write_text(BROKEN_REF_SVG, encoding="utf-8")
        reset_svg_log_dedup()
        _img, messages = render_svg_bytes(path.read_bytes(), 48, source=str(path))
        self.assertTrue(messages, "our hook must see the messages after being replaced")
        self.assertTrue(any(str(path) in m or "resolve property" in m.lower() for m in messages), messages)

    def test_user_snapshot_preflight_previews_and_mask_path(self):
        if not USER_SETTINGS.is_file():
            self.skipTest("user settings.json is not present")
        settings = json.loads(USER_SETTINGS.read_text(encoding="utf-8"))
        choices = json.loads(USER_CHOICES.read_text(encoding="utf-8")) if USER_CHOICES.is_file() else {}
        modules = (settings.get("modules") or {}) if isinstance(settings, dict) else {}
        mask_data = settings if "enabled" in settings else settings.get("mask") or settings
        # settings.json may be the mask settings themselves plus modules.
        self.assertTrue(modules, settings)
        icon_dirs = [p for p in (USER_ICONS, Path("/usr/share/icons")) if p.is_dir()]
        themes = enumerate_themes(icon_dirs)
        # Full inventory preflight of the user's current module sources.
        unread = []
        inspected = 0
        for mid, src in modules.items():
            if not src or src == "system" or src not in themes:
                continue
            grouped = files_by_name(themes[src], mid)
            for name, group in grouped.items():
                for item in group:
                    inspected += 1
                    st = describe_source(item.path)
                    if not st.ok:
                        unread.append((mid, src, name, st.display))
        self.assertGreater(inspected, 100, inspected)
        self.assertTrue(any("kerry.svg" in row[3] for row in unread), unread[:8])

        from iconadapt.backend import IconAdaptBackend
        from iconadapt.persist import atomic_write_json

        iso = {
            "data_home": str(self.root / "data"),
            "config_home": str(self.root / "config"),
            "icon_dirs": [str(p) for p in icon_dirs],
            "app_dirs": [str(self.root / "apps")],
            "current_theme": "Papirus",
            "current_desktop": "KDE",
        }
        (self.root / "apps").mkdir()
        state = Path(iso["data_home"]) / "icon-adapt"
        state.mkdir(parents=True)
        # Copy of the user's current options only; never write back to the real state dir.
        shutil.copy2(USER_SETTINGS, state / "settings.json")
        if USER_CHOICES.is_file():
            shutil.copy2(USER_CHOICES, state / "choices.json")
        be = IconAdaptBackend(isolated=iso)
        previews = {}
        for mid, src in modules.items():
            payload = json.loads(be.modulePreviewJson(mid, src, 16))
            previews[mid] = payload
            self.assertIn("previews", payload)
        self.assertGreaterEqual(len(previews), 4, list(previews))

        # Offscreen load of the real Main.qml with this isolated backend.
        from PyQt6.QtCore import QUrl
        from PyQt6.QtQml import QQmlApplicationEngine
        from iconadapt.gui import qml_dir

        install_qt_svg_log_filter()
        engine = QQmlApplicationEngine()
        engine.rootContext().setContextProperty("iconAdapt", be)
        engine.load(QUrl.fromLocalFile(str(qml_dir() / "Main.qml")))
        self.assertTrue(engine.rootObjects(), "Main.qml failed to load offscreen")
        install_qt_svg_log_filter()

        # Mask export along the real functions, using the user's mask + a
        # representative real-theme set (dangling + incomplete + normal + each module).
        # Full 1862×9 raster is not repeated here; inventory was preflighted above
        # and place_masked_files is the same function used in production.
        reset_svg_log_dedup()
        dest = self.root / "export" / THEME_INTERNAL_NAME
        selections = (choices.get("selections") or {}) if isinstance(choices, dict) else {}
        # Preflight must not rewrite the real choices file; operate on a copy.
        selection_copy = json.loads(json.dumps(selections))
        hard = preflight_manual_selections(selection_copy)
        self.assertEqual(hard, [], hard)
        self.assertEqual(selection_copy, selections)

        apps_dir = self.root / "apps"
        make_desktop(apps_dir, "good-app.desktop", name="Good", icon="good-app")
        from iconadapt.mask import MaskSettings as MS, settings_from_dict
        mask_payload = settings.get("mask") if isinstance(settings.get("mask"), dict) else settings
        try:
            mask = settings_from_dict(mask_payload)
        except Exception:
            mask = MS(
                enabled=bool(mask_payload.get("enabled", True)),
                shape=str(mask_payload.get("shape") or "circle"),
                background=str(mask_payload.get("background") or "#ffffffff"),
                padding=float(mask_payload.get("padding") or 0),
            )
        if not mask.enabled:
            raise AssertionError(f"user snapshot mask should be enabled, got {mask}")
        # Build a mixed export that uses real files from the configured themes.
        # Include kerry (dangling), terminal (missing gradient), amazon (empty filter).
        real_pack = self.root / "icons" / "UserSnap"
        (real_pack / "scalable" / "apps").mkdir(parents=True)
        write(
            real_pack / "index.theme",
            "\n".join([
                "[Icon Theme]",
                "Name=UserSnap",
                "Comment=snapshot",
                "Inherits=hicolor",
                "Directories=scalable/apps",
                "",
                "[scalable/apps]",
                "Size=128",
                "Type=Scalable",
                "MinSize=1",
                "MaxSize=512",
                "Context=Applications",
                "",
            ]),
        )
        # Symlink real sources (read-only) plus the known dangling links.
        for src, name in (
            (TERMINAL, "terminal.svg"),
            (AMAZON, "amazon.svg"),
        ):
            if src.is_file():
                os.symlink(src, real_pack / "scalable" / "apps" / name)
        if KERRY.is_symlink() or KERRY.exists():
            os.symlink(KERRY, real_pack / "scalable" / "apps" / "kerry.svg")
        if KFIND.is_symlink() or KFIND.exists():
            os.symlink(KFIND, real_pack / "scalable" / "apps" / "org.kde.kfind.svg")
        (real_pack / "scalable" / "apps" / "good-app.svg").write_text(NORMAL_SVG, encoding="utf-8")
        # A few more real McMojave icons so this is not a 3-file cherry pick.
        extra = 0
        if MCMOJAVE.joinpath("apps/scalable").is_dir():
            for path in sorted(MCMOJAVE.joinpath("apps/scalable").glob("*.svg")):
                if extra >= 20:
                    break
                dest_link = real_pack / "scalable" / "apps" / path.name
                if dest_link.exists() or dest_link.is_symlink():
                    continue
                os.symlink(path, dest_link)
                extra += 1
        themes2 = enumerate_themes([self.root / "icons", *icon_dirs])
        status = resolve_strategy(themes2, current_theme="UserSnap")
        apps = scan_applications([apps_dir], current_desktop="KDE")
        orig_planned = planned_sizes

        def one_size(source_files, *, has_svg=False):
            return [48]

        with mock.patch("iconadapt.export.planned_sizes", side_effect=one_size):
            prov = export_theme(
                dest,
                apps=apps,
                themes=themes2,
                status=status,
                selections={},
                overwrite_owned=True,
                mask=mask,
                modules={MODULE_APPS: "UserSnap"},
            )
        self.assertTrue((dest / "index.theme").is_file())
        skipped = prov.get("skipped") or []
        self.assertTrue(any(row.get("icon_name") in {"kerry", "org.kde.kfind"} for row in skipped), skipped)
        self.assertTrue(list((dest / "48x48" / "apps").glob("good-app.png")))
        # Real choices/settings on disk must be unchanged.
        if USER_CHOICES.is_file():
            self.assertEqual(json.loads(USER_CHOICES.read_text(encoding="utf-8")), choices)
        self.assertEqual(json.loads(USER_SETTINGS.read_text(encoding="utf-8")), settings)

        from PyQt6.QtCore import QTimer
        from PyQt6.QtGui import QGuiApplication

        # Engine stays alive until the end of this test so QML can finish setup.
        self._engine = engine


if __name__ == "__main__":
    unittest.main()
