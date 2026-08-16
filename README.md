# 🖱️ Custom Cursor Controller / 自定义光标控制器

**A Windows tool that replaces your system arrow cursor with a cursor made from YOUR OWN images — upload, judge, generate, manage.**
**一个将 Windows 系统箭头光标替换为"你自己的图片生成的光标"的小工具——上传、判断、生成、管理一站式。**

The project started as the 🪳 white animated cockroach cursor, and grew into a full custom cursor controller: upload any image (or GIF), get an automatic suitability analysis, generate the `.ani` cursor, toggle it on/off, and manage everything from a modern Vue GUI with a built-in gallery.

本项目起源于 🪳 白色蟑螂动画光标，现已成长为一个完整的自定义光标控制器：上传任意图片（或 GIF）→ 自动适合度分析 → 生成 `.ani` 光标 → 一键开关 → Vue 现代化界面 + 图库管理。

Programmatically built `.ani` files (pure Python), image analysis & background removal via PIL, installed via the Win32 API — no C compiler, no third-party drawing libraries.
纯 Python 手工构建 `.ani` 文件，PIL 完成图片分析与抠背景，并通过 Win32 API 注入系统——无需 C 编译器，无第三方绘图库。

---

## ✨ Features / 功能特性

| 中文 | English |
|---|---|
| 🖱️ **上传任意图片生成光标**：拖拽/点击上传，自动判断**适不适合做光标**（空白/低对比/过多细节/无法抠背景等被拦截或警告，可强制使用） | **Upload any image** to generate a cursor; automatic suitability check (blank / low contrast / too complex / background can't be removed → blocked or warned, force-use allowed) |
| 🎞️ **GIF 动画支持**：多帧提取、保留原始帧时长、动画预览轮播 | **Animated GIF support**: multi-frame extraction, original frame durations kept, live preview loop |
| 🔄 **自动替换**：上传新图片自动生成新光标并替换系统光标 + 弹窗提示 | **Auto-swap**: a new upload auto-generates and replaces the cursor, with a popup notice |
| 🎯 点击预览设置**热点**；↺/↻ **旋转 90°**；**光标大小** 48/64/96 可选 | Click-to-set **hotspot**; ↺/↻ **rotate 90°**; **cursor size** 48/64/96 selectable |
| 🗂 **图库**：所有上传图片与生成光标存于 `C:\Program Files\custom-cursor-controller\data\`，可查看 / 应用 / 重命名 / 归类 / 删除 | **Gallery**: all uploads & cursors stored in `C:\Program Files\custom-cursor-controller\data\`, view / apply / rename / categorize / delete |
| 🔍 **内容查重**（SHA-256）：重复上传相同内容时提示，可选择取消（自动清理） | **Content dedup** (SHA-256): duplicate uploads are flagged; cancel auto-cleans the new copies |
| 🪳 经典蟑螂光标是项目的起源（现已演化为通用控制器） | The classic cockroach cursor is where the project started (now a general-purpose controller) |
| 💾 备份原始光标，退出时自动恢复 | Backs up the original cursor and restores it on exit |
| 📦 纯 Python 构建 `.ani`（RIFF/ACON），帧内嵌 PNG 压缩 `.cur` | `.ani` (RIFF/ACON) built in pure Python, frames embedded as PNG-compressed `.cur` |

---

## 📦 Requirements / 环境要求

- **OS:** Windows (Vista+; PNG-compressed `.cur` frames require Vista+; Web GUI needs WebView2 runtime, preinstalled on Win10/11)
- **Python:** 3.10+ (for running from source)
- **Node.js:** 18+ (only needed to rebuild the Vue frontend)
- **Dependencies:** see [`requirements.txt`](requirements.txt)

```
pystray>=0.19.5
Pillow>=10.0.0
pywebview>=5.0
pythonnet>=3.0
```

---

## 🚀 Quick Start / 快速开始

### Web GUI（推荐） / Recommended

```bash
# 1. Create a virtual environment (optional but recommended)
python -m venv .venv
.venv\Scripts\activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Run the GUI (pywebview window hosting the Vue frontend)
python custom_cursor_gui.pyw
```

> 前端已构建好的版本在 `webui/dist`；如需重新构建：`cd webui && npm install && npm run build`。

### Pre-built executable / 直接使用打包好的程序

Run `dist\CustomCursorController.exe` — double-click to enter the GUI.
运行 `dist\CustomCursorController.exe` —— 双击进入图形界面。

---

## 🖱️ Usage / 使用方法（Web GUI）

1. **上传图片**：拖拽到上传区或点击选择（PNG/JPG/BMP/GIF/WebP，多选 = 动画帧；单个 GIF = 动画）。
2. **适合度判断**：程序分析并给出"适合 / 有风险 / 不适合"结论与原因；有风险或不适合时弹窗确认（硬性不合格如空白图不可强制）。
3. **自动生效**：判断通过后自动生成新光标并替换系统箭头，弹窗提示"已自动替换为新光标「名称」"。
4. **调整**：点击预览图设置热点（点击点位置）；↺/↻ 旋转 90°；光标大小 48/64/96 即时切换（已启用时立即重新生效）。
5. **开关**：勾选"启用自定义光标"生效，取消勾选恢复默认；"恢复默认光标"随时还原。
6. **图库**：切到「图库」页签查看所有上传的图片与生成的光标——可**应用**（恢复该光标并替换系统）、**重命名**、**归类**（自由文本分类）、**删除**。
7. **查重**：上传与已存图片/光标内容相同（SHA-256）时会提示，选择"确定"生成副本或"取消"（自动清理本次内容）。
8. **退出**：关闭窗口最小化到托盘（光标保持生效）；托盘菜单或「退出」按钮退出并自动恢复系统光标。

用户数据默认保存在 `C:\Program Files\custom-cursor-controller\data\`（uploads=上传原图+缩略图，cursors=光标快照 .ani/预览/帧 PNG，index.json=元数据索引）；该位置不可写（如非管理员运行）时依次回退到程序目录 `data/`、系统临时目录。

---

## 🛠️ Building the executables / 打包可执行文件

```bash
pip install pyinstaller

# Web GUI / Web 版（推荐）
pyinstaller CustomCursorController.spec --noconfirm        # → dist\CustomCursorController.exe
```

> Web 版打包前需先构建前端：`cd webui && npm install && npm run build`（产物在 `webui/dist`，已随打包内嵌）。

---

## 📁 Project structure / 项目结构

```
custom-cursor-controller/
├── custom_cursor_gui.pyw  # Web GUI entry: pywebview window + Vue frontend
│                          # 图形界面入口：pywebview 窗口承载 Vue 前端
├── gui_server.py          # Local HTTP API server (upload/analyze/apply/gallery/dedup)
│                          # 本地 HTTP API 后端（上传/分析/应用/图库/查重）
├── cursor_manager.py      # Win32 cursor backup/replace/restore (CursorManager)
│                          # Win32 光标管理（备份/替换/恢复）
├── webui/                 # Vue 3 + Vite frontend source (build output in webui/dist)
│                          # Vue 3 前端源码（构建产物 webui/dist 由后端托管）
├── pointer_analyzer.py    # Suitability scoring + auto background removal (with multi-frame
│                          # consistency for GIFs) 图片适合度分析 + 自动抠背景（含 GIF 帧一致性）
├── ani_builder.py         # Pure-Python RIFF/ACON (.ani) binary builder
│                          # 纯 Python 的 RIFF/ACON (.ani) 二进制构建器
├── test_web_api.py        # End-to-end API tests (95 checks, incl. real cursor swap)
│                          # API 端到端测试（95 项，含真实光标替换）
├── test_image_quality.py  # Real-image mask/alpha quality regressions (no cursor swap)
│                          # 真实图片抠图/Alpha 质量回归（不会替换系统光标）
├── CustomCursorController.spec  # PyInstaller spec (embeds webui/dist)
│                          # 打包配置（内嵌前端）
├── requirements.txt       # pystray + Pillow + pywebview + pythonnet
├── data/                  # User data (created at runtime): uploads/ + cursors/ + index.json
│                          # 用户数据（运行时创建，默认位于 C:\Program Files\custom-cursor-controller\data\）
└── dist/                  # Build output: CustomCursorController.exe
                           # 打包产物
```

---

## 🔧 How it works / 工作原理

1. **Analysis / 分析** — `pointer_analyzer.py` scores each image and auto-removes simple backgrounds via flood-fill. It keeps conservative/island/aggressive mask candidates, rolls back destructive post-processing, and measures fragments, holes, edge leakage, and foreground coverage at the actual cursor size. Low-confidence results require confirmation instead of being auto-applied. Animated files share one background color across frames.
   `pointer_analyzer.py` 对图片逐项评分并用洪水填充去除简单背景；同时保留保守/孤岛/激进三档掩码，补抠破坏主体时自动回退，并在实际光标尺寸检查碎片、孔洞、边框残留与主体占比。低可信结果只预览、需用户确认，不再静默自动应用；动画帧仍共享统一背景色。

2. **Format / 格式** — `ani_builder.py` hand-writes the RIFF `ACON` structure: `anih` header, `rate` chunk (ms → jiffies at 1/60 s), `fram` LIST with one PNG-compressed `.cur` per frame, optional `INFO` metadata, with WORD alignment.
   `ani_builder.py` 手工拼写 RIFF `ACON` 结构：`anih` 头、`rate` 块（毫秒换算为 1/60s 的 jiffy）、`fram` LIST（每帧一个 PNG 压缩的 `.cur`）、可选 `INFO` 元数据，含 WORD 对齐。

3. **Install / 注入** — `LoadCursorFromFileW` → `SetSystemCursor` swaps `OCR_NORMAL`; the original cursor is backed up via `CopyImage` and restored with `SetSystemCursor` + a `SPI_SETCURSORS`/`SPIF_SENDCHANGE` broadcast so every window (including Explorer) reloads the cursor scheme.
   通过 `LoadCursorFromFileW` → `SetSystemCursor` 替换 `OCR_NORMAL`；原始光标用 `CopyImage` 备份，恢复时执行 `SetSystemCursor` 并广播 `SPI_SETCURSORS`/`SPIF_SENDCHANGE`，让所有窗口（含资源管理器）重新加载光标方案。

4. **Storage / 存储** — every upload and generated cursor is snapshotted into `data/` (originals + thumbs + `.ani` + per-frame PNGs + `index.json`), enabling the gallery (view/apply/rename/categorize/delete) and SHA-256 content dedup.
   每次上传的图片与生成的光标都会快照到 `data/`（原图+缩略图+.ani+每帧 PNG+index.json），支撑图库（查看/应用/重命名/归类/删除）与 SHA-256 内容查重。

---

## ⚠️ Notes / 注意事项

- **Restore reliability / 恢复可靠性** — `SetSystemCursor` alone may leave Explorer caching the old cursor; the app therefore always broadcasts `SPI_SETCURSORS` after restoring. If the cursor still looks wrong, log off/on or restart Explorer.
  仅调用 `SetSystemCursor` 时资源管理器可能缓存旧光标；程序恢复后总会广播 `SPI_SETCURSORS`。若光标仍异常，注销重登或重启资源管理器。
- **Administrator rights / 管理员权限** — normal user rights are sufficient for the current user's cursor; no elevation needed.
  替换当前用户的光标无需管理员权限。
- **Cursor size & DPI / 光标大小与缩放** — the GUI offers 48/64/96 px; on high-DPI displays pick a larger size for better visibility.
  GUI 提供 48/64/96 可选；高 DPI 屏幕建议选更大尺寸。
- **Suitability check is heuristic / 适合度判断是启发式** — it reliably rejects obviously unsuitable images but cannot judge aesthetics; the user keeps the final say (force-use allowed).
  适合度判断能可靠拦截明显不合适的图片，但无法评判美观性——用户保留最终决定权（可强制使用）。
- **Onefile startup / 单文件版启动** — the single-file exe unpacks runtime DLLs to a temp dir on start; if an antivirus blocks it, use the folder build.
  单文件版启动时会把运行库解压到临时目录；若被杀毒软件拦截，请使用目录版。

---

## 🧪 Tests / 自测脚本

- `python test_web_api.py` — end-to-end API tests: upload/analyze/apply/restore, rotate & size, gallery CRUD, content dedup, GIF multi-frame + white-background consistency (95 checks, briefly swaps the real cursor).
  端到端 API 测试：上传/分析/应用/恢复、旋转与尺寸、图库 CRUD、内容查重、GIF 多帧与白底一致性（95 项，会短暂替换真实光标）。
- `python -m unittest -v test_image_quality.py` — real `test_res` confidence routing, destructive-postfill rollback, and premultiplied-alpha regressions; does not touch the system cursor.
  真实 `test_res` 可信度路由、破坏性补抠回退与预乘 Alpha 回归测试；不会替换系统光标。
- `python -m ani_builder` — writes a 2-frame test cursor `test_cursor.ani`.
  生成两帧测试光标 `test_cursor.ani`。

---

## 📄 License / 许可证

[MIT](LICENSE) © 2026 Liu Chong
