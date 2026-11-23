"""Default candidate strategy with honest handling of missing themes."""

from __future__ import annotations

from dataclasses import dataclass, field

from .constants import (
    HICOLOR,
    STRATEGY_BREEZE,
    STRATEGY_COLLID,
    STRATEGY_PAPIRUS,
    STRATEGY_TELA,
)
from .hints import hint_names_for
from .theme import IconTheme


@dataclass
class StrategyStatus:
    tela: str | None
    colloid: str | None
    papirus: str | None
    breeze: str | None
    missing: list[str] = field(default_factory=list)
    inherit: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "tela": self.tela,
            "colloid": self.colloid,
            "papirus": self.papirus,
            "breeze": self.breeze,
            "missing": self.missing,
            "inherit": self.inherit,
            "notes": self.notes,
        }


def _first_installed(names: tuple[str, ...], themes: dict[str, IconTheme], prefer: str | None = None) -> str | None:
    if prefer and prefer in themes and prefer in names:
        return prefer
    # If prefer is a dark variant, try matching dark first
    if prefer:
        pl = prefer.lower()
        if "dark" in pl:
            for n in names:
                if "dark" in n.lower() and n in themes:
                    return n
        if "light" in pl:
            for n in names:
                if "light" in n.lower() and n in themes:
                    return n
    for n in names:
        if n in themes:
            return n
    return None


def resolve_strategy(
    themes: dict[str, IconTheme],
    *,
    current_theme: str | None = None,
) -> StrategyStatus:
    tela = _first_installed(STRATEGY_TELA, themes, current_theme)
    colloid = _first_installed(STRATEGY_COLLID, themes, current_theme)
    papirus = _first_installed(STRATEGY_PAPIRUS, themes, current_theme)
    breeze = _first_installed(STRATEGY_BREEZE, themes, current_theme)
    missing: list[str] = []
    notes: list[str] = []
    if not tela:
        missing.append("Tela")
        notes.append("未安装 Tela（或 Tela-dark / Tela-light）。不会下载，也不会假装已具备 Tela 应用图标。")
    if not colloid:
        missing.append("Colloid")
        notes.append("未安装 Colloid。飞书相关补名将不可自动来自 Colloid。")
    if not papirus:
        missing.append("Papirus")
        notes.append("未安装 Papirus。缺名回退链将缩短，仅使用已安装的 Breeze/hicolor。")
    if not breeze:
        missing.append("breeze")
        notes.append("未安装 breeze。Plasma 系统图标兜底可能不完整。")
    inherit: list[str] = []
    if papirus:
        inherit.append(papirus)
    if breeze:
        inherit.append(breeze)
    if HICOLOR not in inherit:
        inherit.append(HICOLOR)
    # Recursion correctness: do NOT put Tela/Colloid in Inherits after papirus/breeze
    # if we want Tela/Colloid app icons to win — those go into the generated
    # theme's *own* files. Inherits is only Papirus → breeze → hicolor.
    notes.append(
        "生成主题只收录应用图标；系统 action/status 不从多套主题混拷，交给 Inherits 中的 Papirus/Breeze。"
    )
    if tela:
        notes.append(f"应用图标默认优先从已安装的 {tela} 取精确同名文件。")
    if colloid:
        notes.append(f"飞书相关精确名可从已安装的 {colloid} 补充。")
    return StrategyStatus(
        tela=tela,
        colloid=colloid,
        papirus=papirus,
        breeze=breeze,
        missing=missing,
        inherit=inherit,
        notes=notes,
    )


def is_feishu_related(icon_name: str) -> bool:
    if not icon_name:
        return False
    names = {icon_name.lower(), *[h.lower() for h in hint_names_for(icon_name)]}
    keys = ("feishu", "lark", "bytedance-feishu", "cn.feishu", "larksuite")
    return any(any(k in n for k in keys) for n in names) or icon_name.lower() in {
        "feishu",
        "lark",
        "bytedance-feishu",
        "cn.feishu.Feishu".lower(),
        "cn.feishu.deepin",
        "com.larksuite.suite",
    }


def default_source_theme_for_icon(icon_name: str, status: StrategyStatus) -> str | None:
    """Theme to copy from for a default (non-manual) assignment. Exact name only.

    Returns None if nothing should be copied (rely on Inherits).
    """
    if not icon_name:
        return None
    # Preference: Tela (all apps), then Colloid for feishu names only.
    # Papirus/Breeze are inherited, not copied, to avoid mixing action/status
    # and to keep the overlay thin.
    if status.tela:
        return "tela_if_has"  # resolved later when we know if the file exists
    if status.colloid and is_feishu_related(icon_name):
        return "colloid_if_has"
    return None
