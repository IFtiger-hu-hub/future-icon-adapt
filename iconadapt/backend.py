"""Qt backend for the QML UI. Does not apply desktop/theme changes unless asked."""

from __future__ import annotations

import json
from pathlib import Path

from PyQt6.QtCore import QObject, QThread, QTimer, Qt, pyqtSignal, pyqtSlot, pyqtProperty, QUrl
from PyQt6.QtGui import QDesktopServices

from .constants import (
    MODULE_SOURCE_SYSTEM,
    THEME_INTERNAL_NAME,
    THEME_DISPLAY_NAME,
    TOOL_ID,
    TOOL_VERSION,
    OWNED_MARKER,
)
from .desktop import scan_applications
from .export import (
    ExportError,
    export_theme,
    is_tool_owned,
    uninstall_owned_theme,
)
from .themejob import ThemeBuildWorker
from .mask import (
    MASK_NOTE,
    MaskError,
    MaskSettings,
    load_settings,
    preview_data_url,
    save_settings,
    settings_from_dict,
    validate_settings,
)
from .modules import (
    clear_module_cache,
    default_module_map,
    load_module_map,
    module_title,
    modules_overview,
    normalize_module_map,
    save_module_map,
    theme_options_for_module,
    usable_source_themes,
)
from .preview import (
    clear_preview_cache,
    compose_module_previews,
    decorate_previews,
    render_preview,
)
from .svgutil import install_qt_svg_log_filter
from .search import matches_report
from .applyone import (
    SingleAppError,
    applied_status_for,
    apply_one,
    has_override,
    merge_single_app_outputs,
    plan_apply_one,
    rollback_one,
)
from .selflaunch import (
    SelfLaunchError,
    install_self_launcher,
    launcher_status,
    remove_self_launcher,
)
from .persist import (
    atomic_write_json,
    load_choices,
    remove_selection,
    save_choices,
    upsert_selection,
)
from .report import report_apps, summarize
from .strategy import resolve_strategy
from .theme import enumerate_themes
from . import xdgpaths
from .hints import collect_hint_candidates
from .lookup import collect_exact_candidates
from .refresh import isolated_hooks, production_hooks, refresh_desktop_icons, RefreshError, RefreshHooks, KICONLOADER_GROUPS


class _DetailCollectWorker(QObject):
    """Collect exact-name and hint candidates off the GUI thread. No Qt SVG/QImage here."""

    finished = pyqtSignal(dict)

    def __init__(self, payload: dict) -> None:
        super().__init__()
        self._payload = payload

    @pyqtSlot()
    def run(self) -> None:
        data = self._payload
        out = {
            "gen": data["gen"],
            "desktop_id": data["desktop_id"],
            "ok": True,
            "candidates": [],
            "hints": [],
            "error": "",
        }
        try:
            name = data.get("name") or ""
            themes = data.get("themes") or {}
            if name:
                out["candidates"] = [
                    {
                        "theme": c.theme,
                        "path": str(c.path),
                        "size": c.size,
                        "rel_dir": c.rel_dir,
                        "name": Path(c.path).stem,
                    }
                    for c in collect_exact_candidates(name, themes)
                ]
                if data.get("need_hints") or not out["candidates"]:
                    rows = []
                    for item in collect_hint_candidates(name, themes):
                        row_item = dict(item) if isinstance(item, dict) else {"path": str(item)}
                        row_item.setdefault("name", Path(row_item.get("path") or "").stem)
                        rows.append(row_item)
                    out["hints"] = rows
        except Exception as exc:  # noqa: BLE001 — surface to GUI, do not freeze it
            out["ok"] = False
            out["error"] = str(exc)
        self.finished.emit(out)


class IconAdaptBackend(QObject):
    dataChanged = pyqtSignal()
    statusMessage = pyqtSignal(str)
    errorMessage = pyqtSignal(str)
    appDetailReady = pyqtSignal(str, str)
    selectionChanged = pyqtSignal()
    jobProgress = pyqtSignal()
    jobFinished = pyqtSignal(str)

    def __init__(self, parent=None, *, isolated: dict | None = None) -> None:
        super().__init__(parent)
        # isolated: optional path overrides for tests {data_home, config_home, app_dirs, icon_dirs, current_theme}
        self._iso = isolated or {}
        self._apps = []
        self._reports = []
        self._themes = {}
        self._strategy = None
        self._current_theme = ""
        self._filter = "all"
        self._query = ""
        self._selected_id = ""
        self._mask = MaskSettings()
        self._modules = default_module_map()
        self._modules_overview_cache = None
        self._detail_gen = 0
        self._detail_ready: set[str] = set()
        self._detail_mask_sig: str = ""
        self._detail_thread: QThread | None = None
        self._detail_worker: _DetailCollectWorker | None = None
        self._live_threads: list[QThread] = []
        self._detail_pending_id = ""
        self._detail_busy = False
        self._job_busy = False
        self._job_kind = ""
        self._job_stage = ""
        self._job_done = 0
        self._job_total = 0
        self._job_label = ""
        self._job_thread: QThread | None = None
        self._job_worker: ThemeBuildWorker | None = None
        self._job_hooks_gui = None
        self._job_using_adapt = False
        self._batch_timer = QTimer(self)
        self._batch_timer.setSingleShot(True)
        self._batch_timer.timeout.connect(self._detail_batch_tick)
        self._batch: dict | None = None
        self.refresh(silent=True)

    def _data_home(self) -> Path:
        if "data_home" in self._iso:
            return Path(self._iso["data_home"])
        return xdgpaths.xdg_data_home()

    def _config_home(self) -> Path:
        if "config_home" in self._iso:
            return Path(self._iso["config_home"])
        return xdgpaths.xdg_config_home()

    def state_dir(self) -> Path:
        d = self._data_home() / "icon-adapt"
        return d

    def _ensure_state(self) -> Path:
        d = self.state_dir()
        d.mkdir(parents=True, exist_ok=True)
        marker = d / OWNED_MARKER
        if not marker.exists():
            atomic_write_json(marker, {"tool": TOOL_ID, "kind": "state", "version": TOOL_VERSION})
        return d

    def _icon_dirs(self) -> list[Path]:
        if "icon_dirs" in self._iso:
            return [Path(p) for p in self._iso["icon_dirs"]]
        return xdgpaths.icon_search_dirs()

    def _app_dirs(self) -> list[Path]:
        if "app_dirs" in self._iso:
            return [Path(p) for p in self._iso["app_dirs"]]
        return xdgpaths.applications_dirs()

    def current_theme_name(self) -> str:
        if self._iso.get("current_theme"):
            return self._iso["current_theme"]
        return xdgpaths.kde_icon_theme_name(self._config_home()) or "breeze"

    @pyqtSlot()
    def refresh(self, silent: bool = False) -> None:
        if self._job_busy:
            if not silent:
                self.errorMessage.emit("正在安装或生成主题，请稍候再刷新。")
            return
        self._cancel_detail_job(clear_ready=True)
        self._themes = enumerate_themes(self._icon_dirs())
        self._current_theme = self.current_theme_name()
        if self._current_theme not in self._themes:
            # still allow lookup (inherit warnings)
            pass
        self._strategy = resolve_strategy(self._themes, current_theme=self._current_theme)
        desktop = self._iso.get("current_desktop") or xdgpaths.current_desktop()
        self._apps = scan_applications(self._app_dirs(), current_desktop=desktop)
        clear_module_cache()
        clear_preview_cache()
        install_qt_svg_log_filter()
        self._modules_overview_cache = None
        if self.state_dir().exists():
            self._mask = load_settings(self.state_dir())
            self._modules = load_module_map(self.state_dir())
        else:
            self._modules = default_module_map()
        store = load_choices(self.state_dir()) if self.state_dir().exists() else {"selections": {}}
        # List view: classify only. Candidates/hints are filled when a row is opened.
        self._reports = report_apps(
            self._apps, self._current_theme, self._themes,
            include_candidates=False, include_hints=False,
        )
        sels = store.get("selections") or {}
        for r in self._reports:
            if r.desktop_id in sels:
                r.selection = sels[r.desktop_id]
        if not silent:
            self.statusMessage.emit(
                f"已扫描 {len(self._apps)} 个可启动应用，当前主题 {self._current_theme}"
            )
        self.dataChanged.emit()

    @pyqtProperty(str, notify=dataChanged)
    def currentTheme(self) -> str:
        return self._current_theme

    @pyqtProperty(str, notify=dataChanged)
    def strategyJson(self) -> str:
        return json.dumps(self._strategy.to_dict() if self._strategy else {}, ensure_ascii=False)

    @pyqtProperty(str, notify=dataChanged)
    def summaryJson(self) -> str:
        return json.dumps(summarize(self._reports), ensure_ascii=False)

    @pyqtProperty(str, notify=dataChanged)
    def appsJson(self) -> str:
        rows = []
        for r in self._reports:
            if self._filter == "issues":
                if r.status in {"ok"}:
                    continue
            elif self._filter != "all" and r.status != self._filter:
                continue
            if not matches_report(r, self._query):
                continue
            rows.append(
                {
                    "desktop_id": r.desktop_id,
                    "name": r.name,
                    "icon": r.icon,
                    "icon_kind": r.icon_kind,
                    "status": r.status,
                    "lookup_theme": r.lookup_theme,
                    "lookup_path": r.lookup_path,
                    "has_selection": bool(r.selection),
                    "selection_theme": (r.selection or {}).get("source_theme", ""),
                    "applied_status": self._applied_status(r.desktop_id, r.selection),
                    "has_override": self._has_override(r.desktop_id),
                }
            )
        return json.dumps(rows, ensure_ascii=False)

    @pyqtProperty(str, notify=dataChanged)
    def themesJson(self) -> str:
        rows = []
        for t in sorted(self._themes.values(), key=lambda x: x.internal_name.lower()):
            rows.append(
                {
                    "internal_name": t.internal_name,
                    "display_name": t.display_name,
                    "path": str(t.path),
                    "inherits": t.inherits,
                    "hidden": t.hidden,
                }
            )
        return json.dumps(rows, ensure_ascii=False)

    @pyqtSlot(str)
    def setFilter(self, value: str) -> None:
        self._filter = value or "all"
        self.dataChanged.emit()

    @pyqtSlot(str)
    def setQuery(self, value: str) -> None:
        self._query = value or ""
        self.dataChanged.emit()

    @pyqtProperty(str, notify=dataChanged)
    def query(self) -> str:
        return self._query


    @pyqtProperty(str, notify=dataChanged)
    def modulesJson(self) -> str:
        if self._modules_overview_cache is None:
            inherit = list(self._strategy.inherit) if self._strategy else []
            self._modules_overview_cache = modules_overview(
                self._themes,
                self._modules,
                preview_limit=8,
                inherit=inherit,
                decorate=True,
            )
        return json.dumps(self._modules_overview_cache, ensure_ascii=False)

    @pyqtSlot(result=str)
    def moduleThemeListJson(self) -> str:
        """Installed packs that can be chosen as a module source. No file counts."""
        rows = [
            {
                "id": MODULE_SOURCE_SYSTEM,
                "name": MODULE_SOURCE_SYSTEM,
                "display": "系统原样（跟随基础主题）",
                "count": 0,
                "empty": False,
                "system": True,
            }
        ]
        for name, theme in sorted(
            usable_source_themes(self._themes).items(), key=lambda kv: kv[0].lower()
        ):
            if theme.hidden:
                continue
            rows.append(
                {
                    "id": name,
                    "name": name,
                    "display": theme.display_name or name,
                    "count": 0,
                    "empty": False,
                    "system": False,
                    "counted": False,
                }
            )
        return json.dumps(rows, ensure_ascii=False)

    @pyqtSlot(str, result=str)
    def moduleThemeOptionsJson(self, module_id: str) -> str:
        return json.dumps(theme_options_for_module(self._themes, module_id or ""), ensure_ascii=False)

    @pyqtSlot(str, str, int, result=str)
    def modulePreviewJson(self, module_id: str, theme_name: str, limit: int) -> str:
        mid = (module_id or "").strip()
        source = (theme_name or "").strip() or self._modules.get(mid, MODULE_SOURCE_SYSTEM)
        inherit = list(self._strategy.inherit) if self._strategy else []
        composed = compose_module_previews(
            source, mid, self._themes, inherit=inherit, limit=max(1, int(limit) or 32)
        )
        items = decorate_previews(composed.get("previews") or [])
        composed = dict(composed)
        composed["previews"] = items
        return json.dumps(composed, ensure_ascii=False)

    @pyqtSlot(str, str, result=str)
    def setModuleSource(self, module_id: str, theme_name: str) -> str:
        mid = (module_id or "").strip()
        mapping = normalize_module_map(self._modules)
        if mid not in mapping:
            self.errorMessage.emit(f"未知模块：{module_id}")
            return json.dumps({"ok": False, "error": f"未知模块：{module_id}"}, ensure_ascii=False)
        source = (theme_name or "").strip() or MODULE_SOURCE_SYSTEM
        if source != MODULE_SOURCE_SYSTEM:
            usable = usable_source_themes(self._themes)
            if source not in usable:
                self.errorMessage.emit(f"未安装的图标包：{source}")
                return json.dumps({"ok": False, "error": f"未安装的图标包：{source}"}, ensure_ascii=False)
        mapping[mid] = source
        state = self._ensure_state()
        save_module_map(state, mapping)
        self._modules = mapping
        self._modules_overview_cache = None
        self.dataChanged.emit()
        label = "系统原样" if source == MODULE_SOURCE_SYSTEM else source
        self.statusMessage.emit(f"已选择：{module_title(mid)} ← {label}")
        return json.dumps({"ok": True, "modules": mapping}, ensure_ascii=False)

    @pyqtProperty(str, notify=dataChanged)
    def maskJson(self) -> str:
        import json as _json
        return _json.dumps(self._mask.to_dict(), ensure_ascii=False)

    @pyqtProperty(bool, notify=dataChanged)
    def maskEnabled(self) -> bool:
        return bool(self._mask.enabled)

    @pyqtProperty(str, notify=dataChanged)
    def maskNote(self) -> str:
        return MASK_NOTE

    @pyqtSlot(str, result=str)
    def setMaskJson(self, raw: str) -> str:
        import json as _json
        try:
            data = _json.loads(raw) if raw else {}
            settings = settings_from_dict(data)
            if settings.enabled:
                validate_settings(settings)
            else:
                # Still reject unknown shapes / bad ranges even when disabled.
                validate_settings(settings, require_enabled_ok=False)
        except (MaskError, ValueError, _json.JSONDecodeError) as exc:
            self.errorMessage.emit(str(exc))
            return _json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)
        state = self._ensure_state()
        save_settings(state, settings)
        self._mask = settings
        self.dataChanged.emit()
        self.statusMessage.emit(
            "已保存外形设置。安装主题时会把图标做成统一外形。" if settings.enabled else "已关闭统一外形。"
        )
        return _json.dumps({"ok": True, "mask": settings.to_dict()}, ensure_ascii=False)

    @pyqtSlot(str, result=str)
    def maskedPreviewUrl(self, path: str) -> str:
        if not self._mask.enabled or not path:
            return ""
        try:
            return preview_data_url(Path(path), 64, self._mask)
        except MaskError as exc:
            self.errorMessage.emit(str(exc))
            return ""


    def _mask_sig(self) -> str:
        m = self._mask
        return f"{int(bool(m.enabled))}:{m.shape}:{m.background}:{m.padding}:{m.custom_path}"

    def _find_report(self, desktop_id: str):
        for r in self._reports:
            if r.desktop_id == desktop_id:
                return r
        return None

    def _row_name_for_detail(self, row) -> str:
        name = row.icon
        if row.icon_kind == "absolute" and row.lookup_path:
            name = Path(row.lookup_path).stem
        return name or ""

    def _decorate_lookup_and_masks(self, data: dict) -> dict:
        path = data.get("lookup_path") or ""
        if path and not data.get("lookup_preview_url"):
            rendered = render_preview(path, 64)
            data["lookup_preview_url"] = rendered.data_url
            data["lookup_render_status"] = rendered.status
            data["lookup_render_note"] = rendered.note
        if path and self._mask.enabled and not data.get("lookup_masked_preview_url"):
            try:
                data["lookup_masked_preview_url"] = preview_data_url(Path(path), 64, self._mask)
            except MaskError:
                data["lookup_masked_preview_url"] = ""
        return data

    def _apply_masks(self, items: list[dict], size: int) -> list[dict]:
        if not self._mask.enabled:
            for item in items:
                item["masked_preview_url"] = ""
            return items
        out = []
        for item in items:
            item = dict(item)
            path = item.get("path") or ""
            if path and not item.get("masked_preview_url"):
                try:
                    item["masked_preview_url"] = preview_data_url(Path(path), size, self._mask)
                except MaskError:
                    item["masked_preview_url"] = ""
            out.append(item)
        return out

    def _detail_payload(self, row, *, loading: bool) -> dict:
        data = row.to_dict()
        data["detail_loading"] = bool(loading)
        data["applied_status"] = self._applied_status(row.desktop_id, row.selection)
        data["has_override"] = self._has_override(row.desktop_id)
        if not loading:
            data = self._decorate_lookup_and_masks(data)
            if self._mask.enabled:
                data["candidates"] = self._apply_masks(list(data.get("candidates") or []), 64)
                data["hints"] = self._apply_masks(list(data.get("hints") or []), 32)
        return data

    def _emit_detail(self, row, *, loading: bool) -> str:
        payload = self._detail_payload(row, loading=loading)
        text = json.dumps(payload, ensure_ascii=False)
        self.appDetailReady.emit(row.desktop_id, text)
        return text

    def _cancel_detail_job(self, *, clear_ready: bool = False) -> None:
        self._detail_gen += 1
        self._detail_pending_id = ""
        self._batch = None
        self._detail_busy = False
        if self._batch_timer.isActive():
            self._batch_timer.stop()
        thread = self._detail_thread
        # Leave a running collector finish in the background; its result is ignored
        # because the generation token will not match. Do not terminate().
        self._detail_thread = None
        self._detail_worker = None
        if thread is not None and not thread.isRunning():
            thread.deleteLater()
        if clear_ready:
            self._detail_ready.clear()
            self._detail_mask_sig = ""

    def _forget_thread(self, thread: QThread) -> None:
        try:
            self._live_threads = [t for t in self._live_threads if t is not thread]
        except Exception:
            pass
        thread.deleteLater()

    def shutdown(self) -> None:
        """Stop detail workers and timers so the backend can be destroyed safely."""
        self._cancel_detail_job()
        if self._batch_timer.isActive():
            self._batch_timer.stop()
        if self._job_worker is not None:
            self._job_worker.request_cancel()
        still = []
        for thread in list(self._live_threads):
            if thread.isRunning():
                # Do not terminate(): a job may be in atomic replace or file copy.
                # quit() is thread-safe; cancel lets run() return at the next safe boundary.
                # DirectConnection on finished->quit avoids deadlocking wait() on the GUI thread.
                thread.quit()
                timeout = 120000 if thread is self._job_thread else 3000
                thread.wait(timeout)
                if thread.isRunning():
                    still.append(thread)
                    continue
            self._forget_thread(thread)
        self._live_threads = still
        if not still:
            self._job_thread = None
            self._job_worker = None
        self._detail_thread = None
        self._detail_worker = None
        self._job_busy = False

    def _maybe_start_pending(self) -> None:
        pending = self._detail_pending_id
        if not pending or self._detail_busy:
            return
        self._detail_pending_id = ""
        self.requestAppDetail(pending)

    @pyqtSlot(str)
    def requestAppDetail(self, desktop_id: str) -> None:
        """Load one app's candidates and previews without blocking the GUI thread.

        Collection runs in a worker. Rasterization stays on the GUI thread in
        small batches so the event loop can keep painting. A newer request
        invalidates older results.
        """
        desktop_id = (desktop_id or "").strip()
        self._selected_id = desktop_id
        if not desktop_id:
            return
        row = self._find_report(desktop_id)
        if row is None:
            self.appDetailReady.emit(desktop_id, "{}")
            return
        mask_ok = self._detail_mask_sig == self._mask_sig()
        if desktop_id in self._detail_ready and mask_ok:
            # Already have this row; only fill missing lookup/mask fields.
            self._emit_detail(row, loading=False)
            return
        if desktop_id in self._detail_ready and not mask_ok:
            # Sources are ready; just refresh mask previews in batches.
            self._start_mask_refresh(row)
            return
        if self._detail_busy:
            self._detail_pending_id = desktop_id
            self._detail_gen += 1  # ignore the in-flight result
            self._emit_detail(row, loading=True)
            return
        self._emit_detail(row, loading=True)
        self._start_collect(row)

    def _start_collect(self, row) -> None:
        self._detail_gen += 1
        gen = self._detail_gen
        name = self._row_name_for_detail(row)
        need_hints = row.status in {
            "missing", "fallback", "extra_fallback", "absolute_missing", "empty"
        }
        self._detail_busy = True
        self._batch = None
        payload = {
            "gen": gen,
            "desktop_id": row.desktop_id,
            "name": name,
            "themes": self._themes,
            "need_hints": need_hints,
        }
        thread = QThread()  # unparented: must be waited on shutdown, never destroyed mid-run
        worker = _DetailCollectWorker(payload)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(self._on_collect_finished)
        worker.finished.connect(thread.quit, Qt.ConnectionType.DirectConnection)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(lambda th=thread: self._forget_thread(th))
        self._detail_thread = thread
        self._detail_worker = worker
        self._live_threads.append(thread)
        thread.start()

    @pyqtSlot(dict)
    def _on_collect_finished(self, result: dict) -> None:
        self._detail_busy = False
        self._detail_thread = None
        self._detail_worker = None
        gen = int(result.get("gen") or 0)
        desktop_id = result.get("desktop_id") or ""
        if gen != self._detail_gen or desktop_id != self._selected_id:
            self._maybe_start_pending()
            return
        row = self._find_report(desktop_id)
        if row is None:
            self._maybe_start_pending()
            return
        if not result.get("ok", True):
            data = row.to_dict()
            data["detail_loading"] = False
            data["detail_error"] = result.get("error") or "无法加载候选图标"
            self.appDetailReady.emit(desktop_id, json.dumps(data, ensure_ascii=False))
            self._maybe_start_pending()
            return
        # Rasterize on the GUI thread a few icons at a time.
        raw_cands = list(result.get("candidates") or [])
        raw_hints = list(result.get("hints") or [])
        self._batch = {
            "gen": gen,
            "desktop_id": desktop_id,
            "candidates_in": raw_cands,
            "hints_in": raw_hints,
            "candidates": [],
            "hints": [],
            "lookup_done": False,
        }
        row.candidates = []
        row.hints = []
        self._batch_timer.start(0)

    def _detail_batch_tick(self) -> None:
        batch = self._batch
        if not batch:
            return
        if batch["gen"] != self._detail_gen or batch["desktop_id"] != self._selected_id:
            self._batch = None
            self._maybe_start_pending()
            return
        row = self._find_report(batch["desktop_id"])
        if row is None:
            self._batch = None
            self._maybe_start_pending()
            return
        budget = 2
        if not batch["lookup_done"]:
            data = row.to_dict()
            self._decorate_lookup_and_masks(data)
            # Stash lookup fields on a side dict attached to the batch.
            batch["lookup"] = {
                "lookup_preview_url": data.get("lookup_preview_url") or "",
                "lookup_render_status": data.get("lookup_render_status") or "",
                "lookup_render_note": data.get("lookup_render_note") or "",
                "lookup_masked_preview_url": data.get("lookup_masked_preview_url") or "",
            }
            batch["lookup_done"] = True
            budget -= 1
        while budget > 0 and batch["candidates_in"]:
            chunk = [batch["candidates_in"].pop(0)]
            decorated = self._apply_masks(decorate_previews(chunk, size=64), 64)
            batch["candidates"].extend(decorated)
            budget -= 1
        while budget > 0 and batch["hints_in"]:
            chunk = [batch["hints_in"].pop(0)]
            decorated = self._apply_masks(decorate_previews(chunk, size=32), 32)
            batch["hints"].extend(decorated)
            budget -= 1
        row.candidates = list(batch["candidates"])
        row.hints = list(batch["hints"])
        still = bool(batch["candidates_in"] or batch["hints_in"])
        payload = self._detail_payload(row, loading=still)
        lookup = batch.get("lookup") or {}
        payload.update(lookup)
        self.appDetailReady.emit(row.desktop_id, json.dumps(payload, ensure_ascii=False))
        if still:
            self._batch_timer.start(0)
            return
        self._batch = None
        self._detail_ready.add(row.desktop_id)
        self._detail_mask_sig = self._mask_sig()
        self._maybe_start_pending()

    def _start_mask_refresh(self, row) -> None:
        """Recompute mask previews for an already-loaded row, still batched."""
        self._detail_gen += 1
        items = list(row.candidates or []) + list(row.hints or [])
        # Clear old mask urls so they are rebuilt.
        for item in items:
            item["masked_preview_url"] = ""
        self._batch = {
            "gen": self._detail_gen,
            "desktop_id": row.desktop_id,
            "candidates_in": list(row.candidates or []),
            "hints_in": list(row.hints or []),
            "candidates": [],
            "hints": [],
            "lookup_done": False,
            "mask_only": True,
        }
        row.candidates = []
        row.hints = []
        self._emit_detail(row, loading=True)
        self._batch_timer.start(0)

    @pyqtSlot(str, result=str)
    def appDetailJson(self, desktop_id: str) -> str:
        """Synchronous detail for tests and tools. The GUI should use requestAppDetail."""
        self._selected_id = desktop_id
        for r in self._reports:
            if r.desktop_id == desktop_id:
                self._ensure_detail(r)
                data = self._detail_payload(r, loading=False)
                self._detail_ready.add(desktop_id)
                self._detail_mask_sig = self._mask_sig()
                return json.dumps(data, ensure_ascii=False)
        return "{}"

    @pyqtSlot(str, result=str)
    def iconPreviewUrl(self, path: str) -> str:
        """Bounded raster preview for a single file. Empty if it cannot be shown."""
        if not path:
            return ""
        rendered = render_preview(path, 64)
        return rendered.data_url

    def _ensure_detail(self, row) -> None:
        if row.desktop_id in self._detail_ready:
            return
        if row.candidates or row.hints:
            self._detail_ready.add(row.desktop_id)
            return
        name = row.icon
        if row.icon_kind == "absolute" and row.lookup_path:
            name = Path(row.lookup_path).stem
        if not name:
            return
        row.candidates = decorate_previews(
            [
                {"theme": c.theme, "path": str(c.path), "size": c.size, "rel_dir": c.rel_dir, "name": Path(c.path).stem}
                for c in collect_exact_candidates(name, self._themes)
            ],
            size=64,
        )
        if row.status in {"missing", "fallback", "extra_fallback", "absolute_missing", "empty"} or not row.candidates:
            hints = collect_hint_candidates(name, self._themes)
            rows = []
            for item in hints:
                row_item = dict(item) if isinstance(item, dict) else {"path": str(item)}
                row_item.setdefault("name", Path(row_item.get("path", "")).stem)
                rows.append(row_item)
            row.hints = decorate_previews(rows, size=32)
        self._detail_ready.add(row.desktop_id)

    def _applied_status(self, desktop_id: str, selection: dict | None) -> str:
        state = self.state_dir()
        if not state.exists():
            return "pending" if selection else "none"
        return applied_status_for(desktop_id, selection, state_dir=state, mask=self._mask)

    def _has_override(self, desktop_id: str) -> bool:
        state = self.state_dir()
        if not state.exists():
            return False
        return has_override(desktop_id, state_dir=state)

    def _applications_dir(self) -> Path:
        return xdgpaths.user_applications_dir(self._data_home())

    def _run_sycoca_if_allowed(self):
        """Refresh application menu cache. Isolated backends only record."""
        if self._iso:
            log = self._iso.setdefault("refresh_log", {})
            log["sycoca"] = int(log.get("sycoca") or 0) + 1
            hooks = self._iso.get("refresh_hooks")
            if hooks and getattr(hooks, "run_kbuildsycoca", None):
                hooks.run_kbuildsycoca()
            return
        from .refresh import default_run_kbuildsycoca

        default_run_kbuildsycoca()

    def _installed_theme_dir(self) -> Path:
        return self._data_home() / "icons" / THEME_INTERNAL_NAME

    def _apply_ctx(self, desktop_id: str) -> dict:
        store = load_choices(self.state_dir()) if self.state_dir().exists() else {"selections": {}}
        sel = (store.get("selections") or {}).get(desktop_id)
        app = next((a for a in self._apps if a.desktop_id == desktop_id), None)
        return {
            "desktop_id": desktop_id,
            "app": app,
            "selection": sel,
            "apps": list(self._apps),
            "state_dir": self._ensure_state(),
            "theme_dir": self._installed_theme_dir(),
            "current_theme": self.current_theme_name(),
            "mask": self._mask,
            "themes": self._themes,
            "target_apps_dir": xdgpaths.user_applications_dir(self._data_home()),
        }

    @pyqtSlot(str, str, str, str, result=str)
    def saveSelection(self, desktop_id: str, icon_name: str, source_theme: str, source_path: str) -> str:
        state = self._ensure_state()
        store = load_choices(state)
        upsert_selection(
            store,
            desktop_id,
            icon_name=icon_name,
            source_theme=source_theme,
            source_path=source_path,
            manual=True,
        )
        save_choices(state, store)
        self._apply_selection(desktop_id, store.get("selections") or {})
        self.selectionChanged.emit()
        self.statusMessage.emit("已选择这个图标，尚未应用到系统。要用到菜单里，请再点「应用到这个应用」。")
        return "ok"

    @pyqtSlot(str, result=str)
    def clearSelection(self, desktop_id: str) -> str:
        state = self._ensure_state()
        store = load_choices(state)
        if remove_selection(store, desktop_id):
            save_choices(state, store)
            self._apply_selection(desktop_id, store.get("selections") or {})
            self.selectionChanged.emit()
            self.statusMessage.emit("已撤销本工具里的选择。已经应用到系统的图标不会因此还原。")
            return "ok"
        return "none"

    def _apply_selection(self, desktop_id: str, selections: dict) -> None:
        for r in self._reports:
            if r.desktop_id == desktop_id:
                r.selection = selections.get(desktop_id)
                break

    def _export_build(self) -> dict:
        state = self._ensure_state()
        build = state / "build" / THEME_INTERNAL_NAME
        store = load_choices(state)
        prov = export_theme(
            build,
            apps=self._apps,
            themes=self._themes,
            status=self._strategy,
            selections=store.get("selections") or {},
            overwrite_owned=True,
            theme_internal_name=THEME_INTERNAL_NAME,
            mask=self._mask,
            modules=self._modules,
        )
        summary = prov.get("source_issue_summary") or ""
        return {
            "ok": True,
            "dest": str(build),
            "icons": len(prov.get("icons", [])),
            "skipped": len(prov.get("skipped", [])),
            "source_issues": len(prov.get("source_issues") or []),
            "source_issue_summary": summary,
        }

    def _copy_build_to_user_icons(self) -> dict:
        from shutil import copytree

        from .safeio import (
            SafeIOError,
            assert_can_replace,
            create_owned_staging,
            discard_staging,
            replace_owned_dir,
        )

        state = self._ensure_state()
        build = state / "build" / THEME_INTERNAL_NAME
        if not is_tool_owned(build):
            raise ExportError("还没有生成主题，请先安装或先生成预览。")
        dest = self._data_home() / "icons" / THEME_INTERNAL_NAME
        try:
            assert_can_replace(dest, overwrite_owned=True)
        except SafeIOError as exc:
            raise ExportError(str(exc)) from exc
        staging = create_owned_staging(dest)
        try:
            copytree(build, staging, symlinks=True, dirs_exist_ok=True)
            if dest.exists() and is_tool_owned(dest):
                merge_single_app_outputs(dest, staging)
            replace_owned_dir(dest, staging)
        except Exception:
            try:
                discard_staging(staging, dest)
            except SafeIOError:
                pass
            raise
        return {"ok": True, "dest": str(dest)}


    def _refresh_icons(self, *, theme_dir, allow_bounce: bool, just_uninstalled: bool = False):
        """Refresh caches after install/uninstall. Isolated backends never touch the real session."""
        hooks = self._iso.get("refresh_hooks")
        log = None
        if hooks is None:
            if self._iso:
                log = self._iso.setdefault("refresh_log", {})
                hooks = isolated_hooks(log)
            else:
                hooks = production_hooks()
        if self._iso:
            # Never touch the real ~/.cache from an isolated backend/test.
            cache_home = Path(self._iso["cache_home"]) if self._iso.get("cache_home") else (self._data_home() / "cache")
        else:
            cache_home = None
        try:
            result = refresh_desktop_icons(
                current_theme=self.current_theme_name(),
                adapt_name=THEME_INTERNAL_NAME,
                theme_dir=theme_dir,
                cache_home=cache_home,
                icon_dirs=self._icon_dirs(),
                hooks=hooks,
                allow_theme_bounce=allow_bounce,
                just_uninstalled=just_uninstalled,
            )
            data = result.to_dict()
            if log is not None:
                data["log"] = {k: log.get(k) for k in ("signals", "sycoca", "change_icons", "theme_cache")}
            return data
        except RefreshError as exc:
            return {"ok": False, "error": str(exc), "warnings": [str(exc)]}

    def _using_adapt(self) -> bool:
        return self.current_theme_name() == THEME_INTERNAL_NAME

    def _source_issue_suffix(self, theme_dir: Path | None = None) -> str:
        path = None
        if theme_dir is not None:
            path = Path(theme_dir) / "provenance.json"
        if path is None or not path.is_file():
            build = self.state_dir() / "build" / THEME_INTERNAL_NAME / "provenance.json"
            path = build if build.is_file() else path
        if path is None or not path.is_file():
            return ""
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return ""
        return str(data.get("source_issue_summary") or "")


    def _theme_job_conflict(self) -> str:
        if not self._job_busy:
            return ""
        labels = {
            "install": "正在安装主题",
            "install_existing": "正在安装主题",
            "generate": "正在生成主题",
            "uninstall": "正在卸载主题",
        }
        return f"{labels.get(self._job_kind, '正在处理主题')}，请稍候。"

    def _clear_job_progress(self, *, label: str = "") -> None:
        self._job_stage = ""
        self._job_done = 0
        self._job_total = 0
        self._job_label = label
        self.jobProgress.emit()

    @pyqtProperty(bool, notify=jobProgress)
    def jobBusy(self) -> bool:
        return self._job_busy

    @pyqtProperty(str, notify=jobProgress)
    def jobKind(self) -> str:
        return self._job_kind

    @pyqtProperty(str, notify=jobProgress)
    def jobStage(self) -> str:
        return self._job_stage

    @pyqtProperty(int, notify=jobProgress)
    def jobDone(self) -> int:
        return int(self._job_done)

    @pyqtProperty(int, notify=jobProgress)
    def jobTotal(self) -> int:
        return int(self._job_total)

    @pyqtProperty(str, notify=jobProgress)
    def jobLabel(self) -> str:
        return self._job_label

    def _refresh_hooks_pair(self) -> tuple[RefreshHooks, RefreshHooks, dict | None]:
        hooks = self._iso.get("refresh_hooks")
        log = None
        if hooks is None:
            if self._iso:
                log = self._iso.setdefault("refresh_log", {})
                hooks = isolated_hooks(log)
            else:
                hooks = production_hooks()
        worker_hooks = RefreshHooks(
            emit_icon_changed=None,
            run_kbuildsycoca=hooks.run_kbuildsycoca,
            change_icons=hooks.change_icons,
            update_theme_cache=hooks.update_theme_cache,
            theme_dir_exists=hooks.theme_dir_exists,
        )
        return hooks, worker_hooks, log


    def _snapshot_theme_job(self, kind: str) -> dict:
        state = self._ensure_state()
        mask = settings_from_dict(self._mask.to_dict())
        store = load_choices(state) if state.exists() else {"selections": {}}
        gui_hooks, worker_hooks, log = self._refresh_hooks_pair()
        if self._iso:
            cache_home = Path(self._iso["cache_home"]) if self._iso.get("cache_home") else (self._data_home() / "cache")
        else:
            cache_home = None
        return {
            "kind": kind,
            "mask": mask,
            "modules": dict(self._modules or {}),
            "selections": dict(store.get("selections") or {}),
            "apps": list(self._apps),
            "themes": self._themes,
            "strategy": self._strategy,
            "build_dir": state / "build" / THEME_INTERNAL_NAME,
            "dest_dir": self._data_home() / "icons" / THEME_INTERNAL_NAME,
            "current_theme": self.current_theme_name(),
            "allow_bounce": (kind in {"install", "install_existing"} and self._using_adapt()),
            "icon_dirs": self._icon_dirs(),
            "cache_home": str(cache_home) if cache_home is not None else None,
            "refresh_hooks": worker_hooks,
            "refresh_log": log,
            "_gui_hooks": gui_hooks,
        }

    def _start_theme_job(self, kind: str) -> str:
        conflict = self._theme_job_conflict()
        if conflict:
            self.errorMessage.emit(conflict)
            return json.dumps({"ok": False, "started": False, "error": conflict}, ensure_ascii=False)
        if kind in {"install", "generate", "install_existing"} and self._strategy is None:
            err = "还没有扫描到图标主题，请稍后再试。"
            self.errorMessage.emit(err)
            return json.dumps({"ok": False, "started": False, "error": err}, ensure_ascii=False)
        if kind in {"install", "generate", "install_existing"}:
            try:
                validate_settings(self._mask)
            except MaskError as exc:
                self.errorMessage.emit(str(exc))
                return json.dumps({"ok": False, "started": False, "error": str(exc)}, ensure_ascii=False)
        if kind == "uninstall":
            dest = self._data_home() / "icons" / THEME_INTERNAL_NAME
            build = self.state_dir() / "build" / THEME_INTERNAL_NAME
            if not dest.exists() and not build.exists():
                self.statusMessage.emit("没有已安装的本工具主题可卸载。")
                return json.dumps({"ok": True, "started": False, "messages": [], "refresh": None}, ensure_ascii=False)
        snapshot = self._snapshot_theme_job(kind)
        self._job_hooks_gui = snapshot.pop("_gui_hooks")
        self._job_using_adapt = bool(snapshot.get("allow_bounce") or self._using_adapt())
        self._job_busy = True
        self._job_kind = kind
        self._job_stage = "validate"
        self._job_done = 0
        self._job_total = 0
        start_labels = {
            "install": "正在安装主题…",
            "install_existing": "正在安装主题…",
            "generate": "正在生成预览主题…",
            "uninstall": "正在卸载主题…",
        }
        self._job_label = start_labels.get(kind, "正在处理…")
        self.jobProgress.emit()
        worker = ThemeBuildWorker(snapshot)
        thread = QThread()
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.progress.connect(self._on_theme_job_progress)
        worker.finished.connect(self._on_theme_job_finished)
        worker.finished.connect(thread.quit, Qt.ConnectionType.DirectConnection)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(lambda th=thread: self._forget_thread(th))
        self._job_thread = thread
        self._job_worker = worker
        self._live_threads.append(thread)
        thread.start()
        if kind == "generate":
            self.statusMessage.emit("正在生成预览主题，窗口可以继续使用。")
        elif kind == "uninstall":
            self.statusMessage.emit("正在卸载主题，窗口可以继续使用。")
        else:
            self.statusMessage.emit("正在安装主题，窗口可以继续使用。")
        return json.dumps({"ok": True, "started": True, "kind": kind}, ensure_ascii=False)

    @pyqtSlot(str)
    def _on_theme_job_progress(self, raw: str) -> None:
        try:
            payload = json.loads(raw) if raw else {}
        except ValueError:
            payload = {}
        self._job_stage = str(payload.get("stage") or self._job_stage)
        if "done" in payload:
            try:
                self._job_done = int(payload.get("done") or 0)
            except (TypeError, ValueError):
                pass
        if "total" in payload:
            try:
                self._job_total = int(payload.get("total") or 0)
            except (TypeError, ValueError):
                pass
        message = str(payload.get("message") or "").strip()
        if message:
            if self._job_total > 0:
                self._job_label = f"{message}（{self._job_done} / {self._job_total}）" if " / " not in message and "共 " not in message else message
            else:
                self._job_label = message
        self.jobProgress.emit()

    @pyqtSlot(str)
    def _on_theme_job_finished(self, raw: str) -> None:
        try:
            result = json.loads(raw) if raw else {}
        except ValueError:
            result = {"ok": False, "error": "无法解析任务结果"}
        kind = result.get("kind") or self._job_kind
        if result.get("ok") and result.get("refresh") and result["refresh"].get("pending_icon_changed"):
            self._emit_icon_changed_on_gui(result["refresh"])
            result["refresh"]["pending_icon_changed"] = False
        self._job_busy = False
        self._job_kind = ""
        self._job_stage = "done" if result.get("ok") else ""
        self._job_worker = None
        self._job_thread = None
        issues = result.get("source_issue_summary") or ""
        if result.get("cancelled"):
            self._job_label = ""
            self.jobProgress.emit()
            self.errorMessage.emit(result.get("error") or "已取消")
        elif not result.get("ok"):
            self._job_label = ""
            self.jobProgress.emit()
            self.errorMessage.emit(result.get("error") or "操作失败")
        else:
            extra = "已套用统一外形。" if result.get("mask_enabled") and kind in {"install", "install_existing"} else ""
            if kind == "generate":
                msg = "已生成预览主题，还没有安装，系统图标不会改变。" + issues
            elif kind == "uninstall":
                if self._job_using_adapt:
                    msg = "已卸载本工具安装的主题，并已刷新桌面图标。电脑里原来的图标包都还在。"
                else:
                    msg = "已卸载本工具安装的主题。电脑里原来的图标包都还在。"
            elif self._job_using_adapt:
                msg = "主题已经装好，并已刷新桌面图标。" + extra + issues
            else:
                msg = (
                    f"主题已经装好。{extra}请打开系统设置 → 外观 → 图标，手动选择「{THEME_DISPLAY_NAME}」。"
                    "本工具不会自动切换。" + issues
                )
            self._job_label = ""
            self.jobProgress.emit()
            self.statusMessage.emit(msg)
        self._job_hooks_gui = None
        self.jobFinished.emit(json.dumps(result, ensure_ascii=False))

    def _emit_icon_changed_on_gui(self, refresh: dict) -> None:
        """KIconLoader DBus signals must be sent from the GUI thread."""
        hooks = self._job_hooks_gui
        if hooks is None or hooks.emit_icon_changed is None:
            return
        signals = list(refresh.get("signals") or [])
        for group in KICONLOADER_GROUPS:
            if group in signals:
                continue
            try:
                hooks.emit_icon_changed(group)
                signals.append(group)
            except Exception as exc:  # noqa: BLE001
                warnings = list(refresh.get("warnings") or [])
                warnings.append(f"发送 iconChanged({group}) 失败：{exc}")
                refresh["warnings"] = warnings
                break
        refresh["signals"] = signals

    @pyqtSlot(result=str)
    def cancelThemeJob(self) -> str:
        """Request cancel at the next safe boundary. Does not interrupt atomic replace."""
        if not self._job_busy or self._job_worker is None:
            return json.dumps({"ok": True, "cancelled": False, "error": "当前没有正在进行的任务"}, ensure_ascii=False)
        self._job_worker.request_cancel()
        self._job_label = "正在取消…将在当前步骤安全结束后停止"
        self.jobProgress.emit()
        return json.dumps({"ok": True, "started": False, "cancelling": True}, ensure_ascii=False)

    @pyqtSlot(result=str)
    def generateTheme(self) -> str:
        """Write theme into the tool build dir. Does not install or activate it."""
        return self._start_theme_job("generate")

    @pyqtSlot(result=str)
    def installUserTheme(self) -> str:
        """Copy/build into the user icon directory. Does not change the active theme."""
        return self._start_theme_job("install_existing")

    @pyqtSlot(result=str)
    def installTheme(self) -> str:
        """Generate then install into the user icon directory. Does not switch the active theme."""
        return self._start_theme_job("install")

    @pyqtSlot(result=str)
    def uninstallOutputs(self) -> str:
        return self._start_theme_job("uninstall")


    @pyqtSlot(str, result=str)
    def planApplyToApp(self, desktop_id: str) -> str:
        """Preflight for applying the current selection to one app. No writes."""
        plan = plan_apply_one(**self._apply_ctx(desktop_id))
        return json.dumps(plan, ensure_ascii=False)

    @pyqtSlot(str, result=str)
    def applyToApp(self, desktop_id: str) -> str:
        """Export and install only this app's selected icon; fix its launcher if needed."""
        conflict = self._theme_job_conflict()
        if conflict:
            self.errorMessage.emit(conflict)
            return json.dumps({"ok": False, "status": "failed", "error": conflict}, ensure_ascii=False)
        ctx = self._apply_ctx(desktop_id)
        try:
            result = apply_one(**ctx)
        except SingleAppError as exc:
            self.errorMessage.emit(str(exc))
            return json.dumps({"ok": False, "status": "failed", "error": str(exc), "message": str(exc)}, ensure_ascii=False)
        if result.get("ok") and result.get("status") == "applied":
            dest = ctx["theme_dir"]
            result["refresh"] = self._refresh_icons(theme_dir=dest, allow_bounce=self._using_adapt())
            self.refresh(silent=True)
            self.statusMessage.emit(result.get("message") or "已应用到这个应用。")
        elif result.get("ok") and result.get("status") == "unchanged":
            self.statusMessage.emit(result.get("message") or "无需修改。")
        else:
            self.errorMessage.emit(result.get("error") or result.get("message") or "未能应用到这个应用。")
        return json.dumps(result, ensure_ascii=False)

    @pyqtSlot(str, result=str)
    def rollbackApplyToApp(self, desktop_id: str) -> str:
        conflict = self._theme_job_conflict()
        if conflict:
            self.errorMessage.emit(conflict)
            return json.dumps({"ok": False, "error": conflict, "message": conflict}, ensure_ascii=False)
        result = rollback_one(
            desktop_id=desktop_id,
            state_dir=self._ensure_state(),
            theme_dir=self._installed_theme_dir(),
        )
        if result.get("ok"):
            dest = self._installed_theme_dir()
            if dest.exists() and self._using_adapt():
                result["refresh"] = self._refresh_icons(theme_dir=dest, allow_bounce=True)
            self.refresh(silent=True)
            store = load_choices(self.state_dir()) if self.state_dir().exists() else {}
            kept = bool((store.get("selections") or {}).get(desktop_id))
            if kept:
                result["message"] = (result.get("message") or "已恢复原图标。") + "已选候选仍保留，可再次应用到这个应用。"
                result["selection_kept"] = True
            else:
                result["selection_kept"] = False
            self.statusMessage.emit(result.get("message") or "已恢复原图标。")
        else:
            self.errorMessage.emit(result.get("error") or result.get("message") or "恢复原图标失败。")
        return json.dumps(result, ensure_ascii=False)

    @pyqtSlot(str, result=str)
    def planDesktopOverride(self, desktop_id: str) -> str:
        """Compatibility alias; the UI should call planApplyToApp."""
        return self.planApplyToApp(desktop_id)

    @pyqtSlot(str, result=str)
    def applyDesktopOverride(self, desktop_id: str) -> str:
        """Compatibility alias; the UI should call applyToApp."""
        return self.applyToApp(desktop_id)

    @pyqtSlot(str, result=str)
    def rollbackDesktopOverride(self, desktop_id: str) -> str:
        """Compatibility alias; the UI should call rollbackApplyToApp."""
        return self.rollbackApplyToApp(desktop_id)

    @pyqtSlot(result=str)
    def selfLauncherStatus(self) -> str:
        return json.dumps(
            launcher_status(applications_dir=self._applications_dir(), state_dir=self.state_dir()),
            ensure_ascii=False,
        )

    @pyqtSlot(result=str)
    def installSelfLauncher(self) -> str:
        try:
            result = install_self_launcher(
                applications_dir=self._applications_dir(),
                state_dir=self._ensure_state(),
                run_sycoca=self._run_sycoca_if_allowed,
            )
        except SelfLaunchError as exc:
            self.errorMessage.emit(str(exc))
            return json.dumps({"ok": False, "error": str(exc), "message": str(exc)}, ensure_ascii=False)
        self.statusMessage.emit(result.get("message") or "已添加到应用菜单。")
        return json.dumps(result, ensure_ascii=False)

    @pyqtSlot(result=str)
    def removeSelfLauncher(self) -> str:
        try:
            result = remove_self_launcher(
                applications_dir=self._applications_dir(),
                state_dir=self._ensure_state(),
                run_sycoca=self._run_sycoca_if_allowed,
            )
        except SelfLaunchError as exc:
            self.errorMessage.emit(str(exc))
            return json.dumps({"ok": False, "error": str(exc), "message": str(exc)}, ensure_ascii=False)
        self.statusMessage.emit(result.get("message") or "已从应用菜单移除。")
        return json.dumps(result, ensure_ascii=False)

    @pyqtSlot(result=str)
    def versionInfo(self) -> str:
        return json.dumps({"tool": TOOL_ID, "version": TOOL_VERSION, "theme": THEME_INTERNAL_NAME}, ensure_ascii=False)

    @pyqtSlot(str)
    def openPath(self, path: str) -> None:
        if path:
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))
