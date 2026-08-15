# 🪳 Cockroach Cursor / 蟑螂光标

**A Windows tool that replaces your system arrow cursor with an animated white cockroach cursor.**
**一个将 Windows 系统箭头光标替换为白色蟑螂动画光标的小工具。**

Programmatically drawn (PIL), pure-Python built `.ani` files, and installed via the Win32 API — no C compiler, no third-party drawing libraries.
程序化绘制（PIL）、纯 Python 手工构建 `.ani` 文件，并通过 Win32 API 注入系统——无需 C 编译器，无第三方绘图库。

---

## ✨ Features / 功能特性

| 中文 | English |
|---|---|
| 🖱️ 用白色蟑螂**动画光标**替换系统默认箭头 | Replaces the default arrow with an **animated** white cockroach cursor |
| 🪳 8 帧三足步态爬行循环（触角摆动、身体浮动） | 8-frame tripod-gait crawling loop (antennae sway, body bob) |
| 🔄 系统托盘驻留，右键菜单随时切换/恢复/退出 | System tray resident; right-click menu to enable / restore / quit |
| 💾 备份原始光标，退出时自动恢复 | Backs up the original cursor and restores it on exit |
| 📦 纯 Python 构建 `.ani`（RIFF/ACON）二进制，帧内嵌 PNG 压缩 `.cur` | `.ani` (RIFF/ACON) built in pure Python, frames embedded as PNG-compressed `.cur` |
| 🎨 形态复刻：以参考照片为蓝本，复刻触角外展、腿部分节、尾须外分、翅缝线、腹节纹（保持白色配色） | Morphology modeled on a reference photo: splayed antennae, segmented legs, spread cerci, wing seam & abdominal segments (white palette kept) |
| 📦 可打包为单文件 exe，资源内嵌，运行零文件产生 | Packable as a single-file exe with embedded resources — writes nothing at runtime |

---

## 📦 Requirements / 环境要求

- **OS:** Windows (Vista+; PNG-compressed `.cur` frames require Vista+)
- **Python:** 3.10+ (for running from source)
- **Dependencies:** see [`requirements.txt`](requirements.txt)

```
pystray>=0.19.5
Pillow>=10.0.0
```

---

## 🚀 Quick Start / 快速开始

### From source / 源码运行

```bash
# 1. Create a virtual environment (optional but recommended)
python -m venv .venv
.venv\Scripts\activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Run (double-click or command line)
pythonw cockroach_cursor.pyw        # no console window
python cockroach_cursor.pyw         # with console
```

> On first run the program generates `resources\cockroach.ani` and `tray_icon.png` next to the script, then replaces the system cursor.
> 首次运行会在脚本旁生成 `resources\cockroach.ani` 和 `tray_icon.png`，随后替换系统光标。

### Pre-built executable / 直接使用打包好的程序

Run `dist\CockroachCursor.exe` (single file, resources embedded, writes nothing to disk).
运行 `dist\CockroachCursor.exe`（单文件版，资源内嵌，运行时不写盘）。

---

## 🖱️ Usage / 使用方法

1. **Run** the program — the white cockroach cursor replaces the arrow immediately.
   **运行程序** —— 白色蟑螂光标立即替换箭头。
2. **Right-click the tray icon** for the menu:
   **右键托盘图标** 弹出菜单：

   | 菜单项 / Menu item | Action / 作用 |
   |---|---|
   | 🪳 启用蟑螂光标 / Enable cockroach cursor | Replace arrow with cockroach (default action) / 替换为蟑螂光标（默认项） |
   | ↩ 恢复默认箭头 / Restore default arrow | Restore the original arrow cursor / 恢复原始箭头光标 |
   | ❌ 退出 / Quit | Restore cursor and exit / 恢复光标并退出 |

3. The original cursor is backed up on startup and automatically restored when the program exits (or when "Restore" is chosen).
   启动时备份原始光标，退出程序或点击“恢复”时自动还原。

---

## 🛠️ Building the executable / 打包可执行文件

```bash
pip install pyinstaller

# Single-file build (uses CockroachCursor.spec)
pyinstaller CockroachCursor.spec --noconfirm

# or folder build (onedir)
pyinstaller --onedir --noconfirm --distpath dist --workpath build\pyi \
  --specpath build --add-data "resources\cockroach.ani;resources" \
  --add-data "resources\tray_icon.png;resources" \
  cockroach_cursor.pyw --name CockroachCursor
```

- Output: `dist\CockroachCursor.exe` (single file) or `dist\CockroachCursor\` (folder).
  产物：`dist\CockroachCursor.exe`（单文件）或 `dist\CockroachCursor\`（目录版）。
- Both variants **embed** `cockroach.ani` + `tray_icon.png` into the exe — no `resources` folder is created at runtime.
  两个版本都把 `cockroach.ani` + `tray_icon.png` **内嵌**进 exe —— 运行时不再产生 `resources` 文件夹。
- Regenerate resources before packing if you changed the artwork:
  若改动了绘制代码，打包前重新生成资源：

```bash
python build_resources.py
```

---

## 🎨 Custom pattern / 自定义图案

### 方式一：Web 图形界面（推荐，Vue 3 + pywebview）

`dist\CockroachCursorGUIWeb.exe` 是 **Vue 3 现代化界面**的桌面窗口版（pywebview/WebView2 渲染，不再是原生控件样式），双击进入 GUI，可以：
- **上传图片**（拖拽或点击，支持 GIF 动画）→ 程序自动判断图片**适不适合做光标**（空白/对比度低/细节过多/无法抠背景等会被拦截或警告，用户可强制使用）
- **上传新图片自动替换光标**并弹窗提示
- **一键开关**"自定义光标是否生效"
- 点击预览图设置热点（点击点位置）；多选图片生成**动画光标**（预览实时轮播）
- **光标大小选择**（48 / 64 / 96，切换即生效）
- **图库页签**：查看所有上传的图片与生成的光标（存于程序目录 `data/`），支持**应用 / 重命名 / 归类 / 删除**
- 关闭窗口最小化到托盘（光标保持生效），退出时自动恢复系统光标

架构：Vue 3 前端（`webui/`）由 Python 本地 HTTP 服务托管，复用全部现有核心逻辑（分析/抠背景/生成 .ani/替换光标）。用户数据存储在程序目录 `data/`（uploads=上传原图+缩略图，cursors=光标快照 .ani/预览/帧 PNG，index.json=元数据索引）；程序目录不可写时自动回退到系统临时目录。

源码运行：`python cockroach_gui_web.pyw`；前端构建：`cd webui && npm install && npm run build`；打包：`pyinstaller CockroachCursorGUIWeb.spec --noconfirm`

### 方式二：tkinter 图形界面（旧版）

`dist\CockroachCursorGUI.exe` 是 tkinter 原生控件版，功能相同、界面朴素。源码运行：`python cockroach_gui.pyw`；打包：`pyinstaller CockroachCursorGUI.spec --noconfirm`

### 方式三：命令行脚本

```bash
# Static cursor from one image / 一张图 = 静态光标
python make_custom_cursor.py my_pattern.png

# Animated cursor from several frames / 多张图 = 动画光标
python make_custom_cursor.py frame0.png frame1.png frame2.png ...

# Useful options / 常用选项
python make_custom_cursor.py my_pattern.png \
    --size 48 --hotspot 12 12 --duration-ms 150
```

- The script **overwrites** `resources\cockroach.ani` + `resources\tray_icon.png`, which the program loads on start (it only auto-generates them when missing).
  脚本会**覆盖** `resources\cockroach.ani` 和 `resources\tray_icon.png`，程序启动时直接加载（程序只在文件不存在时才自动生成）。
- `--hotspot X Y` sets the click point — put it at the "tip" of arrow-like patterns, or the shape's center.
  `--hotspot X Y` 设置点击热点——箭头类图案请设在尖端，其他图案可设中心。
- The packaged exe embeds resources: re-run `pyinstaller CockroachCursor.spec --noconfirm` after changing, or run from source.
  打包版 exe 资源内嵌：改图后需重新打包 `pyinstaller CockroachCursor.spec --noconfirm`，或直接用源码运行。

---

## 📁 Project structure / 项目结构

```
cockroach/
├── cockroach_cursor.pyw   # Main entry: tray app + Win32 cursor management (main program)
│                          # 主入口：托盘程序 + Win32 光标管理
├── cockroach_gui.pyw      # GUI app: upload image → suitability check → toggle cursor on/off
│                          # 图形界面（tkinter 版）：上传图片→适合度判断→生成光标→开关
├── cockroach_gui_web.pyw  # Web GUI entry: pywebview window + Vue frontend
│                          # 图形界面（Web 版）入口：pywebview 窗口承载 Vue 前端
├── gui_server.py          # Local HTTP API server for the Vue frontend
│                          # 为 Vue 前端提供本地 HTTP API 的后端服务
├── webui/                 # Vue 3 + Vite frontend source (build output served by gui_server)
│                          # Vue 3 前端源码（构建产物由 gui_server 托管）
├── pointer_analyzer.py    # Image suitability scoring (blank/contrast/complexity/bg removal)
│                          # 图片适合度分析（空白/对比度/复杂度/抠背景）
├── cursor_drawer.py       # Drawing engine: 48×48 white cockroach, 8-frame animation
│                          # 绘图引擎：48×48 白色蟑螂，8 帧动画
├── make_custom_cursor.py  # Build a .ani from YOUR OWN PNGs (replace the cockroach)
│                          # 用你自己的 PNG 生成自定义光标（替换蟑螂图案）
├── ani_builder.py         # Pure-Python RIFF/ACON (.ani) binary builder
│                          # 纯 Python 的 RIFF/ACON (.ani) 二进制构建器
├── build_resources.py     # One-shot script: generate resources/ + validate RIFF header
│                          # 一次性脚本：生成 resources/ 并校验 RIFF 头
├── diagnose_ani.py        # Check whether Windows can load the .ani (LoadCursorFromFileW)
│                          # 检查 Windows 能否加载该 .ani
├── diagnose_frames.py     # Deep-dive: parse internal fram LIST, dump each frame PNG
│                          # 深度诊断：解析内部 fram LIST，导出每帧 PNG
├── CockroachCursor.spec   # PyInstaller spec (single-file, resources embedded)
│                          # PyInstaller 配置（单文件，资源内嵌）
├── CockroachCursorGUI.spec  # PyInstaller spec for the tkinter GUI version
│                          # tkinter 版打包配置
├── CockroachCursorGUIWeb.spec # PyInstaller spec for the Web GUI version
│                          # Web 版打包配置（内嵌 webui/dist 前端）
├── requirements.txt       # pystray + Pillow
├── colored_cockroach.png  # Reference photo for morphology (not packaged)
│                          # 形态参考照片（不参与打包）
└── dist/                  # Build output: CockroachCursor.exe / CockroachCursor/
                           #   CockroachCursorGUI.exe (GUI version)
                           # 打包产物
```

---

## 🔧 How it works / 工作原理

1. **Drawing / 绘图** — `cursor_drawer.py` builds each frame with `PIL.ImageDraw`: body ellipse, head, pronotum, eyes, 6 two-segment legs (femur + tibia, tripod gait with phase offset 0.5), antennae as segmented arcs, cerci, wing-seam line and abdominal segment marks.
   `cursor_drawer.py` 用 `PIL.ImageDraw` 逐帧绘制：身体椭圆、头部、前胸背板、眼睛、6 条两段式腿（股节+胫节，相位差 0.5 的三足步态）、分段弧形触角、尾须、翅缝线与腹节纹。

2. **Format / 格式** — `ani_builder.py` hand-writes the RIFF `ACON` structure: `anih` header, `rate` chunk (ms → jiffies at 1/60 s), `fram` LIST with one PNG-compressed `.cur` per frame, optional `INFO` metadata, with WORD alignment.
   `ani_builder.py` 手工拼写 RIFF `ACON` 结构：`anih` 头、`rate` 块（毫秒换算为 1/60s 的 jiffy）、`fram` LIST（每帧一个 PNG 压缩的 `.cur`）、可选 `INFO` 元数据，含 WORD 对齐。

3. **Install / 注入** — `cockroach_cursor.pyw` uses `LoadCursorFromFileW` → `SetSystemCursor` to swap `OCR_NORMAL`; the original cursor is backed up via `CopyImage` and restored with `SetSystemCursor` + a `SPI_SETCURSORS`/`SPIF_SENDCHANGE` broadcast so every window (including Explorer) reloads the cursor scheme.
   `cockroach_cursor.pyw` 通过 `LoadCursorFromFileW` → `SetSystemCursor` 替换 `OCR_NORMAL`；原始光标用 `CopyImage` 备份，恢复时执行 `SetSystemCursor` 并广播 `SPI_SETCURSORS`/`SPIF_SENDCHANGE`，让所有窗口（含资源管理器）重新加载光标方案。

---

## ⚠️ Notes / 注意事项

- **Restore reliability / 恢复可靠性** — `SetSystemCursor` alone may leave Explorer caching the old cursor; the app therefore always broadcasts `SPI_SETCURSORS` after restoring. If the cursor still looks wrong, log off/on or restart Explorer.
  仅调用 `SetSystemCursor` 时资源管理器可能缓存旧光标；程序恢复后总会广播 `SPI_SETCURSORS`。若光标仍异常，注销重登或重启资源管理器。
- **Administrator rights / 管理员权限** — normal user rights are sufficient for the current user's cursor; no elevation needed.
  替换当前用户的光标无需管理员权限。
- **DPI / 缩放** — the cursor is fixed at 48×48 px; on high-DPI displays it may appear smaller. Regenerate via `cursor_drawer.py` constants (`SIZE`) if needed.
  光标固定 48×48 px；高 DPI 屏幕上可能偏小，可通过 `cursor_drawer.py` 的 `SIZE` 常量重新生成。
- **Onefile startup / 单文件版启动** — the single-file exe unpacks runtime DLLs to a temp dir on start; if an antivirus blocks it, use the folder build (`dist\CockroachCursor\`).
  单文件版启动时会把运行库解压到临时目录；若被杀毒软件拦截，请使用目录版（`dist\CockroachCursor\`）。

---

## 🧪 Tests / 自测脚本

- `python -m ani_builder` — writes a 2-frame test cursor `test_cursor.ani`.
  生成两帧测试光标 `test_cursor.ani`。
- `python diagnose_ani.py` — verifies the built `.ani` loads via `LoadCursorFromFileW`.
  验证生成的 `.ani` 可被 Windows 加载。
- `python diagnose_frames.py` — parses the `fram` LIST and exports each frame PNG to `resources/frame_*.png`.
  解析 `fram` LIST 并导出每帧 PNG 到 `resources/frame_*.png`。

---

## 📄 License / 许可证

[MIT](LICENSE) © 2026 Liu Chong
