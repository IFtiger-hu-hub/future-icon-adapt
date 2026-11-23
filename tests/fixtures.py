"""Temporary XDG/theme/desktop fixtures. Isolated from the real desktop."""

from __future__ import annotations

import os
from pathlib import Path


def write(p: Path, text: str) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def write_bytes(p: Path, data: bytes) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


# Minimal valid 1x1 PNG (same for tests that only need a file to exist)
PNG_1x1 = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000a49444154789c63000100000500010d0a2db40000000049454e44ae426082"
)


def _guess_index_size(rel: str) -> tuple[int, str]:
    parts = rel.replace("\\", "/").split("/")
    typ = "Fixed"
    size = 48
    for raw in parts:
        token = raw.split("@", 1)[0]
        if token.startswith("scalable"):
            return 48, "Scalable"
        if "x" in token:
            try:
                size = int(token.split("x", 1)[0])
                typ = "Fixed"
            except ValueError:
                pass
        elif token.isdigit():
            size = int(token)
            typ = "Fixed"
    return size, typ


def _guess_index_context(rel: str, override: str | None = None) -> str:
    if override:
        return override
    low = rel.replace("\\", "/").lower()
    if "actions" in low:
        return "Actions"
    if "status" in low or "panel" in low or "applets" in low or "applet" in low:
        return "Status"
    if "mimetypes" in low or "/mime/" in low or low.endswith("/mime"):
        return "MimeTypes"
    if "places" in low or "filesystems" in low:
        return "Places"
    if "devices" in low:
        return "Devices"
    if "emblem" in low:
        return "Emblems"
    if "emote" in low:
        return "Emotes"
    return "Applications"


def mini_index(
    name: str,
    inherits: str,
    dirs: str = "48x48/apps,scalable/apps",
    contexts: dict[str, str] | None = None,
) -> str:
    parts = []
    parts.append("[Icon Theme]")
    parts.append(f"Name={name}")
    parts.append("Comment=test")
    parts.append(f"Inherits={inherits}")
    parts.append(f"Directories={dirs}")
    parts.append("")
    for d in dirs.split(","):
        d = d.strip()
        if not d:
            continue
        size, typ = _guess_index_size(d)
        ctx = _guess_index_context(d, (contexts or {}).get(d))
        parts += [f"[{d}]", f"Size={size}", f"Context={ctx}", f"Type={typ}"]
        if typ == "Scalable":
            parts += ["MinSize=1", "MaxSize=256"]
        parts.append("")
    return "\n".join(parts) + "\n"


def make_theme(
    root: Path,
    internal: str,
    inherits: str,
    icons: dict[str, list[str]],
    extra_dirs: str = "",
    contexts: dict[str, str] | None = None,
) -> Path:
    """icons: {icon_name: ['48x48/apps', 'scalable/apps']}"""
    tdir = root / internal
    dirs = []
    for rels in icons.values():
        dirs.extend(rels)
    dirstr = ",".join(dict.fromkeys(list(dirs) + ([extra_dirs] if extra_dirs else [])))
    if not dirstr:
        dirstr = "48x48/apps"
    write(tdir / "index.theme", mini_index(internal, inherits, dirstr, contexts=contexts))
    for name, rels in icons.items():
        for rel in rels:
            write_bytes(tdir / rel / f"{name}.png", PNG_1x1)
    return tdir


def make_desktop(dir: Path, filename: str, *, name: str, icon: str, typ: str = "Application", extra: str = "") -> Path:
    body = "\n".join([
        "[Desktop Entry]",
        "Type=" + typ,
        f"Name={name}",
        f"Icon={icon}",
        "Exec=true",
        extra,
        "",
    ])
    return write(dir / filename, body)


def isolated_env(tmp: Path) -> dict:
    """Return path dict for tests; does not mutate process env unless applied."""
    data = tmp / "data"
    cfg = tmp / "config"
    (data / "applications").mkdir(parents=True)
    (data / "icons").mkdir(parents=True)
    (cfg).mkdir(parents=True)
    return {
        "data_home": data,
        "config_home": cfg,
        "app_dirs": [data / "applications"],
        "icon_dirs": [data / "icons"],
        "current_theme": "breeze",
        "current_desktop": "KDE",
    }
