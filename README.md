# future-icon-adapt

[![License: GPL-3.0-or-later](https://img.shields.io/badge/License-GPL--3.0--or--later-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB.svg)](https://www.python.org/)
[![KDE Plasma](https://img.shields.io/badge/Desktop-KDE%20Plasma%206%20%2F%20Kirigami-1D99F3.svg)](https://kde.org/)

面向 **KDE Plasma** 桌面的模块化图标组合与单应用图标适配工具（PyQt6 + QtQuick / Kirigami）。

**`future-icon-adapt`** scans your launchable desktop applications, lets you compose an independent user-level icon theme by category from locally installed icon packs, preview and pick per-app replacement icons, and optionally apply uniform shape masks — without modifying upstream icon themes or automatically changing your system configuration.

---

## 核心特性

- **按模块组合主题（Modular Theme Composition）**
  依据 `index.theme` 的 `Context`（而非硬编码目录名）将本地已安装的图标包拆分为六类独立搭配：
  - **应用图标**（`apps`）
  - **文件夹与位置**（`places`，含设备与文件系统位置）
  - **文件类型**（`mimetypes`）
  - **系统操作**（`actions`）
  - **状态与托盘**（`status`）
  - **其他装饰**（`other`，徽章、表情等，可不选）
  不想替换的模块可保持「**系统原样**」，继续通过 `Inherits` 继承基础主题。预览区严格展示图标包自身真实文件，若当前主题缺失该类图标并回退到父主题，会明确标注来源而非伪造归属。
- **应用图标扫描与逐项手选**
  扫描用户与系统的 `.desktop` 启动器，提供状态筛选（已适配 / 缺少图标 / 自带绝对路径图标 / 备用图标可用）与即时中英文搜索（匹配应用名、Desktop ID、`Icon=`、`GenericName`、`Comment`）。为缺少图标的应用提供保守别名提示（如常见国产桌面应用），但**从不自动瞎猜改名**。
- **应用图标统一外形遮罩（Shape Mask）**
  内置 **圆形**、**圆角方形**、**超椭圆**，也支持自定义 **SVG / PNG** 遮罩，并可调整底板色（ARGB/RGBA 十六进制）与内边距（`0`–`0.45` 画布比例）。遮罩**只作用于导出的应用图标副本**，不修改原图标包，也不裁切文件夹、文件类型或系统图标。
- **单应用快速生效与一键恢复**
  支持仅将选定图标「应用到这个应用」（共用相同 `Icon=` 的应用会自动隔离命名，避免误伤其他应用），并在左侧列表一键「**恢复原图标**」（带备份与回滚）。
- **零侵入与安全边界**
  - **绝不自动修改 `kdeglobals` 或强制切换系统主题**（已在使用 `Icon Adapt` 时仅在安装/卸载后刷新图标缓存）。
  - **旁路临时目录原子切换**：生成与安装均在旁路暂存目录完成后切换，失败保留原有数据，绝不先删后写。
  - **所有权标记**：所有输出目录均带有 `.future-icon-adapt-owned` 标记，卸载只清理工具自身产物。
  - **后台异步任务**：生成与安装主题在后台执行，界面保持流畅响应并实时显示进度阶段。

---

## 环境要求

- **操作系统**：Linux（KDE Plasma 6 / Wayland 或 X11）
- **Python**：3.11 及以上（开发与完整测试基于 Python 3.13）
- **GUI 运行时**：PyQt6（含 `QtQuick` / `QtQml`）与 KDE Kirigami QML 模块（推荐直接使用发行版仓库提供的 Qt6 / Kirigami 包，例如 openSUSE Tumbleweed、Arch Linux、Fedora KDE 等）

> 若仅使用命令行子命令（`scan`、`themes`、`report`、`candidates`、`export`、`uninstall-theme`），核心依赖仅需 Python 标准库（启用遮罩或渲染预览时需 PyQt6）。

---

## 快速开始

### 1. 获取源码并启动图形界面

```bash
git clone https://github.com/IFtiger-hu-hub/future-icon-adapt.git
cd future-icon-adapt

# 启动图形界面
python3 -m iconadapt gui
# 或使用便捷入口
python3 app.py
```

可选以可编辑方式安装 CLI 入口（复用系统已安装的 PyQt6 / Kirigami）：

```bash
pip install --no-build-isolation --no-deps -e .
future-icon-adapt gui
```

### 2. 添加到应用菜单（可选）

在图形界面「高级选项」中可一键安装本工具自己的用户级菜单启动器（`图标适配`），图标使用系统自带的 `preferences-desktop-icons`，随后可在 Plasma 应用菜单中打开或固定到任务栏。如移动了项目目录，重新安装一次启动器即可更新路径。

---

## 图形界面四步使用流程

1. **找应用**：使用顶部搜索框与状态筛选（已适配 / 缺少图标 / 自带图标 / 备用图标可用）定位需要处理的应用。
2. **搭配模块**：为每个模块选择来自本地已装图标包的来源，或保持「系统原样」。点击缩略图可查看图标名称与真实来源路径。
3. **单应用手选图标**：点选候选预览即保存选择（状态显示为「已选择，待应用」，不会立即修改菜单）；逐项手选优先于应用模块的默认来源。
4. **生成与安装主题**：按需开启「统一外形」遮罩，点击「**安装主题**」。若系统当前尚未选用 `Icon Adapt`，请前往 **系统设置 → 外观与视觉风格 → 图标** 手动选择 `Icon Adapt`。

在「高级选项」中，还可以：
- 选择「**只生成预览，不安装**」仅输出到暂存构建目录；
- 使用「**应用到这个应用**」对当前选中应用单独生效并刷新缓存；
- 对已应用覆盖的应用直接点击「**恢复原图标**」。

---

## 命令行用法（CLI）

```bash
# 列出本地已安装图标主题及默认策略解析状态
python3 -m iconadapt themes

# 扫描可启动应用（支持 --query 即时过滤）
python3 -m iconadapt scan
python3 -m iconadapt scan --query 微信

# 输出图标覆盖诊断报告
python3 -m iconadapt report
python3 -m iconadapt report --status missing
python3 -m iconadapt report --query wechat --json

# 按精确图标名检索所有已装主题中的候选文件
python3 -m iconadapt candidates firefox

# 预览导出计划或生成用户级主题（不切换系统主题）
python3 -m iconadapt export --dry-run
python3 -m iconadapt export
python3 -m iconadapt export --module places=Papirus --module status=breeze

# 安全卸载（仅删除带有 .future-icon-adapt-owned 标记的用户级主题目录）
python3 -m iconadapt uninstall-theme
```

---

## 输出目录与安全边界

所有写入均集中在用户目录且带有所有权标记（`.future-icon-adapt-owned`）：

| 用途 | 路径 |
| :--- | :--- |
| 状态、手选记录与遮罩配置 | `~/.local/share/icon-adapt/` |
| 构建中的主题输出 | `~/.local/share/icon-adapt/build/icon-adapt/` |
| 安装后的用户级图标主题 | `~/.local/share/icons/icon-adapt/` |
| `.desktop` 覆盖备份 | `~/.local/share/icon-adapt/desktop-backups/` |

### 默认内置策略说明

未单独指定模块时，工具采用保守的默认策略：`Tela`（应用视觉主体，若已装）→ `Colloid`（飞书精确名补充，若已装）→ `Papirus`（缺名继承）→ `breeze` → `hicolor`。**未安装的主题会如实报告缺失，绝不联网下载，也不会假装齐备。**

### 已知限制与不做的事

- **托盘与系统配色**：部分应用自绘托盘或 SNI 托盘不遵循系统图标主题；`actions` / `status` 符号图标在实际桌面中可能会随系统配色重新着色，与文件本身颜色预览略有差异。
- **遮罩栅格化**：启用应用图标外形遮罩后，导出的应用图标副本会栅格化为多尺寸 PNG（`16` / `22` / `24` / `32` / `48` / `64` / `96` / `128` / `256`），不再保留原矢量；原始主题不会被修改。
- **Qt SVG Tiny 限制**：部分第三方复杂 SVG（如包含 `clipPath`、特定 `pattern` 或缺失引用）在 Qt SVG Tiny 引擎中会部分渲染，工具会在预览与安装摘要中诚实标明，不修补源文件。
- **离线本地运行**：不联网下载图标包、不上传本机任何数据、不公开发布合并第三方图标的资源包、不擅自修改 `kdeglobals`。

---

## 测试与开发验证

运行单元测试套件：

```bash
python3 -m unittest discover -s tests -v
```

运行无头离屏 GUI 启动验证（不修改真实桌面状态）：

```bash
QT_QPA_PLATFORM=offscreen python3 -m iconadapt gui --quit-after-load
```

---

## 开源协议与第三方资源声明

- 本项目源代码基于 **[GNU General Public License v3.0 or later (GPL-3.0-or-later)](LICENSE)** 开源。
- 详细版权与第三方声明见 [NOTICE](NOTICE)，版本历史见 [CHANGELOG.md](CHANGELOG.md)。
- **本仓库不包含也不分发任何第三方图标主题资源**。工具在本地生成的组合主题仅复制或符号链接您本机已安装的图标文件，这些图标资源仍受各自上游协议约束。
