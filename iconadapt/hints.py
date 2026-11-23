"""Name-mismatch hints only. Never auto-assign an alias."""

from __future__ import annotations

from .constants import HINT_GROUPS
from .lookup import Candidate, collect_exact_candidates
from .theme import IconTheme


def hint_names_for(icon_name: str) -> list[str]:
    """Return alternate names from known groups or case variants. Empty if none."""
    if not icon_name:
        return []
    hints: list[str] = []
    lower = icon_name.lower()
    for group in HINT_GROUPS:
        lows = {n.lower() for n in group}
        if lower in lows:
            for n in group:
                if n != icon_name and n not in hints:
                    hints.append(n)
    return hints


def case_variants_present(icon_name: str, themes: dict[str, IconTheme]) -> list[Candidate]:
    """If a theme has the same stem with different case, report as a hint candidate."""
    if not icon_name:
        return []
    target = icon_name.lower()
    found: list[Candidate] = []
    # We cannot cheaply list all stems; only try common case patterns.
    variants = {icon_name, icon_name.lower(), icon_name.upper()}
    if "." in icon_name:
        parts = icon_name.split(".")
        # last segment capitalizations
        last = parts[-1]
        variants.add(".".join(parts[:-1] + [last[:1].upper() + last[1:]]))
        variants.add(".".join(parts[:-1] + [last.lower()]))
        variants.add(".".join(parts[:-1] + [last[0].upper() + last[1:] if last else last]))
    variants.discard(icon_name)
    seen: set[tuple[str, str]] = set()
    for name in variants:
        if not name or name == icon_name:
            continue
        if name.lower() != target:
            continue
        for cand in collect_exact_candidates(name, themes):
            key = (cand.theme, name)
            if key in seen:
                continue
            seen.add(key)
            found.append(cand)
    return found


def collect_hint_candidates(
    icon_name: str,
    themes: dict[str, IconTheme],
    *,
    preferred_size: int = 48,
) -> list[dict]:
    """Structured hints: {kind, icon_name, theme, path} — not assignments."""
    rows: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    for cand in case_variants_present(icon_name, themes):
        stem = cand.path.stem
        key = ("case", cand.theme, stem)
        if key in seen:
            continue
        seen.add(key)
        rows.append(
            {
                "kind": "case",
                "icon_name": stem,
                "theme": cand.theme,
                "path": str(cand.path),
                "note": "同一名称大小写不同，仅提示，不会自动改用",
            }
        )
    for alt in hint_names_for(icon_name):
        for cand in collect_exact_candidates(alt, themes, preferred_size=preferred_size):
            key = ("alias", cand.theme, alt)
            if key in seen:
                continue
            seen.add(key)
            rows.append(
                {
                    "kind": "alias",
                    "icon_name": alt,
                    "theme": cand.theme,
                    "path": str(cand.path),
                    "note": "已知别名提示，不会自动改用",
                }
            )
    return rows
