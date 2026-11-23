"""Bounded raster cache for preview and export.

Keys are based on the real file identity (resolved device/inode/mtime/size)
plus a digest of the exact payload we rasterized and the output mode.
Alias paths that resolve to the same file reuse the same entry. Symbolic
recolouring and different mask settings never share an entry.

Memory is capped; when a session spill directory is set, evicted or large
images can be stored as PNG there and reloaded. The spill directory must
not be the destination theme directory.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# About 96 MiB of decoded ARGB. A 256x256 image is 256 KiB; this holds
# hundreds of small sizes plus a modest number of large ones.
DEFAULT_MAX_BYTES = 96 * 1024 * 1024


def file_identity(path: Path | str | None) -> tuple:
    """Follow symlinks. Return a stable tuple or empty if unreadable."""
    if not path:
        return ()
    src = Path(path)
    try:
        st = src.stat()
    except OSError:
        return ()
    try:
        resolved = str(src.resolve())
    except OSError:
        resolved = str(src)
    return (resolved, st.st_dev, st.st_ino, int(getattr(st, "st_mtime_ns", int(st.st_mtime * 1e9))), st.st_size)


def payload_digest(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.blake2b(data, digest_size=16).hexdigest()


def mask_signature(settings) -> str:
    if settings is None:
        return "nomask"
    enabled = bool(getattr(settings, "enabled", False))
    shape = str(getattr(settings, "shape", "") or "")
    background = str(getattr(settings, "background", "") or "")
    padding = getattr(settings, "padding", 0)
    try:
        pad = f"{float(padding):.5f}"
    except (TypeError, ValueError):
        pad = "0"
    custom = str(getattr(settings, "custom_path", "") or "")
    custom_id = ""
    if custom:
        ident = file_identity(custom)
        custom_id = ident[0] + ":" + str(ident[3] if ident else "") if ident else custom
    return f"mask:{int(enabled)}:{shape}:{background}:{pad}:{custom_id}"


def palette_signature(pal) -> str:
    if pal is None:
        return "nopalette"
    parts = []
    for name in (
        "text", "background", "highlight", "neutral", "positive", "negative",
        "view_text", "view_background", "button_text", "button_background",
    ):
        parts.append(f"{name}={getattr(pal, name, '') or ''}")
    return "pal:" + "|".join(parts)


def make_key(
    *,
    digest: str,
    size: int,
    role: str,
    identity: tuple = (),
    palette: str = "nopalette",
    mask: str = "nomask",
    process: str = "raw",
) -> tuple:
    return (digest, int(size), role, identity, palette, mask, process)


@dataclass
class CacheStats:
    hits: int = 0
    misses: int = 0
    evictions: int = 0
    spill_writes: int = 0
    spill_reads: int = 0
    unique_puts: int = 0
    bytes_current: int = 0
    bytes_peak: int = 0
    max_bytes: int = DEFAULT_MAX_BYTES

    def as_dict(self) -> dict[str, Any]:
        return {
            "hits": self.hits,
            "misses": self.misses,
            "evictions": self.evictions,
            "spill_writes": self.spill_writes,
            "spill_reads": self.spill_reads,
            "unique_puts": self.unique_puts,
            "bytes_current": self.bytes_current,
            "bytes_peak": self.bytes_peak,
            "max_bytes": self.max_bytes,
            "hit_rate": (self.hits / (self.hits + self.misses)) if (self.hits + self.misses) else None,
        }


class RenderCache:
    def __init__(self, max_bytes: int = DEFAULT_MAX_BYTES) -> None:
        self.max_bytes = max(16 * 16 * 4, int(max_bytes))
        self._lock = threading.Lock()
        self._mem: OrderedDict[tuple, tuple] = OrderedDict()  # key -> (nbytes, image, meta)
        self._bytes = 0
        self.stats = CacheStats(max_bytes=self.max_bytes)
        self._spill_dir: Path | None = None
        self._owns_spill = False

    def begin_session(self, *, spill_dir: Path | None = None, max_bytes: int | None = None) -> None:
        # Fresh counters for this export/preview session. Previous owned spill is dropped.
        old_spill = None
        old_owns = False
        with self._lock:
            old_spill, old_owns = self._spill_dir, self._owns_spill
            self._mem.clear()
            self._bytes = 0
            if max_bytes is not None:
                self.max_bytes = max(16 * 16 * 4, int(max_bytes))
            self.stats = CacheStats(max_bytes=self.max_bytes)
            if spill_dir is not None:
                spill_dir = Path(spill_dir)
                spill_dir.mkdir(parents=True, exist_ok=True)
                self._spill_dir = spill_dir
                self._owns_spill = False
            else:
                self._spill_dir = Path(tempfile.mkdtemp(prefix="iconadapt-rendcache-"))
                self._owns_spill = True
        if old_owns and old_spill is not None and old_spill != self._spill_dir:
            _safe_rmtree_spill(old_spill)

    def end_session(self, *, clear_memory: bool = True, drop_spill: bool = True) -> dict[str, Any]:
        with self._lock:
            snap = self.stats.as_dict()
            spill = self._spill_dir
            owns = self._owns_spill
            if drop_spill:
                self._spill_dir = None
                self._owns_spill = False
            if clear_memory:
                self._mem.clear()
                self._bytes = 0
                self.stats.bytes_current = 0
        if drop_spill and owns and spill is not None:
            _safe_rmtree_spill(spill)
        return snap

    def clear(self) -> None:
        with self._lock:
            self._mem.clear()
            self._bytes = 0
            self.stats.bytes_current = 0

    def get(self, key: tuple):
        entry = self.get_entry(key)
        return None if entry is None else entry[0]

    def get_entry(self, key: tuple):
        """Return (image, meta) or None."""
        with self._lock:
            item = self._mem.get(key)
            if item is not None:
                self._mem.move_to_end(key)
                self.stats.hits += 1
                return item[1], item[2]
            if self._spill_dir is not None:
                path = self._spill_path(key)
                meta_path = path.with_suffix(".json")
                if path.is_file():
                    img = _read_png(path)
                    if img is not None and not img.isNull():
                        meta = _read_meta(meta_path)
                        self.stats.hits += 1
                        self.stats.spill_reads += 1
                        self._insert_unlocked(key, img, nbytes=None, meta=meta)
                        return img, meta
            self.stats.misses += 1
            return None

    def put(self, key: tuple, image, meta: dict | None = None) -> None:
        if image is None or getattr(image, "isNull", lambda: True)():
            return
        nbytes = _image_bytes(image)
        with self._lock:
            if key in self._mem:
                old = self._mem.pop(key)[0]
                self._bytes -= old
            # Never keep a single image larger than the budget in RAM; spill only.
            if nbytes > self.max_bytes:
                self._spill_unlocked(key, image, meta)
                return
            self._insert_unlocked(key, image, nbytes, meta)
            self.stats.unique_puts += 1

    def _insert_unlocked(self, key, image, nbytes: int | None = None, meta: dict | None = None) -> None:
        if nbytes is None:
            nbytes = _image_bytes(image)
        while self._bytes + nbytes > self.max_bytes and self._mem:
            ek, packed = self._mem.popitem(last=False)
            eb = packed[0]
            evicted = packed[1]
            evicted_meta = packed[2] if len(packed) > 2 else None
            self._bytes -= eb
            self.stats.evictions += 1
            if self._spill_dir is not None:
                self._spill_unlocked(ek, evicted, evicted_meta)
        self._mem[key] = (nbytes, image, meta or {})
        self._bytes += nbytes
        self.stats.bytes_current = self._bytes
        if self._bytes > self.stats.bytes_peak:
            self.stats.bytes_peak = self._bytes

    def _spill_unlocked(self, key, image, meta: dict | None = None) -> None:
        if self._spill_dir is None:
            return
        path = self._spill_path(key)
        if path.is_file():
            return
        try:
            if image.save(str(path), "PNG"):
                self.stats.spill_writes += 1
                if meta:
                    _write_meta(path.with_suffix(".json"), meta)
        except Exception:
            pass

    def _spill_path(self, key: tuple) -> Path:
        digest = hashlib.blake2b(repr(key).encode("utf-8"), digest_size=16).hexdigest()
        assert self._spill_dir is not None
        return self._spill_dir / f"{digest}.png"


_CACHE = RenderCache()


def get_cache() -> RenderCache:
    return _CACHE


def begin_render_session(*, spill_dir: Path | None = None, max_bytes: int | None = None) -> None:
    _CACHE.begin_session(spill_dir=spill_dir, max_bytes=max_bytes)


def end_render_session(*, clear_memory: bool = True, drop_spill: bool = True) -> dict[str, Any]:
    return _CACHE.end_session(clear_memory=clear_memory, drop_spill=drop_spill)


def cache_stats() -> dict[str, Any]:
    return _CACHE.stats.as_dict()


def _image_bytes(image) -> int:
    try:
        return max(1, int(image.width()) * int(image.height()) * 4)
    except Exception:
        return 1


def _read_png(path: Path):
    from PyQt6.QtGui import QImage

    img = QImage(str(path))
    if img.isNull():
        return None
    return img


def _write_meta(path: Path, meta: dict) -> None:
    import json

    try:
        path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def _read_meta(path: Path) -> dict:
    import json

    try:
        if path.is_file():
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}
    return {}


def _safe_rmtree_spill(path: Path) -> None:
    """Delete only a spill dir we created under the system temp directory."""
    import shutil

    try:
        resolved = path.resolve()
    except OSError:
        return
    tmp = Path(tempfile.gettempdir()).resolve()
    if tmp not in resolved.parents and resolved.parent != tmp:
        return
    if not resolved.name.startswith("iconadapt-rendcache-"):
        return
    if not resolved.is_dir():
        return
    shutil.rmtree(resolved, ignore_errors=True)


__all__ = [
    "DEFAULT_MAX_BYTES",
    "CacheStats",
    "RenderCache",
    "begin_render_session",
    "cache_stats",
    "end_render_session",
    "file_identity",
    "get_cache",
    "make_key",
    "mask_signature",
    "palette_signature",
    "payload_digest",
]
