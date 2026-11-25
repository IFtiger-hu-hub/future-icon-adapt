"""Tool identity, default strategy names, and conservative alias hints."""

from __future__ import annotations

TOOL_ID = "future-icon-adapt"
TOOL_VERSION = "0.2.11"
THEME_INTERNAL_NAME = "icon-adapt"
THEME_DISPLAY_NAME = "Icon Adapt"
OWNED_MARKER = ".future-icon-adapt-owned"
STATE_DIRNAME = "icon-adapt"
CHOICES_FILENAME = "choices.json"
SETTINGS_FILENAME = "settings.json"
PROVENANCE_FILENAME = "provenance.json"
OVERRIDE_MANIFEST = "desktop-overrides.json"
SINGLE_APP_PATCHES = "single-app-patches.json"
SINGLE_APP_ICON_PREFIX = "icon-adapt.app."
LAUNCHER_DESKTOP_FILENAME = "future-icon-adapt.desktop"
LAUNCHER_DESKTOP_ID = "future-icon-adapt"
LAUNCHER_MENU_NAME = "图标适配"
LAUNCHER_ICON_NAME = "preferences-desktop-icons"
LAUNCHER_OWNED_KEY = "X-Future-Icon-Adapt"
BUILD_SUBDIR = "build"

# Default visual/fill strategy. These are *internal directory names*.
# Presence is always checked at runtime; missing themes are reported, never invented.
STRATEGY_TELA = ("Tela", "Tela-dark", "Tela-light")
STRATEGY_COLLID = ("Colloid", "Colloid-dark", "Colloid-light")
STRATEGY_PAPIRUS = ("Papirus", "Papirus-Dark", "Papirus-Light")
STRATEGY_BREEZE = ("breeze", "breeze-dark")
HICOLOR = "hicolor"

# Icon name groups used only as *hints* when the exact desktop Icon= is not found.
# Never auto-assigned.
HINT_GROUPS: tuple[tuple[str, ...], ...] = (
    ("wechat", "weixin", "com.tencent.WeChat", "com.tencent.wechat", "wewechat",
     "electronic-wechat", "com.qq.weixin.deepin", "deepin.com.wechat"),
    ("qq", "linuxqq", "com.qq.QQ", "deepinwine-qq", "apps.com.qq.im",
     "deepin.com.qq.im", "im-qq"),
    ("dingtalk", "com.dingtalk.DingTalk", "com.alibabainc.dingtalk",
     "apps.com.dingtalk.im", "com.dingtalk.deepin"),
    ("feishu", "lark", "bytedance-feishu", "cn.feishu.Feishu",
     "cn.feishu.deepin", "com.larksuite.suite"),
    ("wxwork", "wecom", "weixin-work", "com.qq.weixin.work.deepin",
     "deepin.com.weixin.work"),
    ("wemeet", "wemeetapp", "com.tencent.wemeet"),
    ("baidunetdisk", "com.baidu.NetDisk", "deepin.com.baidu.pan"),
    ("netease-cloud-music", "com.netease.CloudMusic", "netease-cloud-music-gtk"),
    ("wps-office-wps", "wps-office-wpsmain", "wps-office2019-wpsmain",
     "wps-office2023-wpsmain", "com.wps.Office.wpsmain"),
    ("wps-office-et", "wps-office-etmain", "wps-office2019-etmain",
     "wps-office2023-etmain"),
    ("wps-office-wpp", "wps-office-wppmain", "wps-office2019-wppmain",
     "wps-office2023-wppmain"),
)

# Built-in preview/export mask shapes (custom SVG/PNG is separate).
MASK_SHAPES = ("circle", "rounded-square", "squircle")
MASK_COMMON_SIZES = (16, 22, 24, 32, 48, 64, 96, 128, 256)

# Composition modules. Directory names are not assumed; classification uses
# index.theme Context, then path tokens.
MODULE_APPS = "apps"
MODULE_PLACES = "places"
MODULE_MIMETYPES = "mimetypes"
MODULE_ACTIONS = "actions"
MODULE_STATUS = "status"
MODULE_OTHER = "other"
MODULE_IDS: tuple[str, ...] = (
    MODULE_APPS,
    MODULE_PLACES,
    MODULE_MIMETYPES,
    MODULE_ACTIONS,
    MODULE_STATUS,
    MODULE_OTHER,
)
# Do not copy this module; leftover icons come from Inherits / the base theme.
MODULE_SOURCE_SYSTEM = "__system__"

