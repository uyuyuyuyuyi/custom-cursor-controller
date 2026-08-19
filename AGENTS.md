# AGENTS.md — 项目交接与 Agent 工作说明

本文件既是给后续 Codex / agent 的项目说明，也是跨设备交接说明。
最后更新：2026-08-19（仓库迁移：cockroach-pointer 存档老版本，custom-cursor-controller 为活仓库）

## 一、这是什么项目

Windows 桌面应用：上传图片/GIF → 自动抠图/适合度分析 → 生成 `.ani` 动画光标 → 替换系统鼠标指针。

技术栈：Python 3.13 + pywebview（EdgeChromium）+ Vue 3 / Vite；Pillow 图像处理；onnxruntime 可选 AI 抠图（模型懒下载、懒加载）。

## 二、仓库与分支

- 主远端 `standalone`：`https://github.com/uyuyuyuyuyi/custom-cursor-controller.git`（**活仓库**；本地 `main` 跟踪 `standalone/main`，日常 pull/push 走这里）
- 存档远端 `origin`：`https://github.com/uyuyuyuyuyi/cockroach-pointer.git`（**老版本存档**：`main` 停在 `2187322` —— 无 GUI、执行后自动替换白色蟑螂光标的初始版本；只读，不要推它）
- 默认分支：`main`（已包含本说明）
- 历史沿革（2026-08-19 迁移）：全部 GUI / 上传图片生成光标开发（自 `d5faec0` 起）归入 custom-cursor-controller（当前 `52dec57`）；cockroach-pointer 回退到老版本 `2187322`；原 cockroach-pointer 上的 `feature/ai-onnx-cutout`、`feature/custom-cursor-gui` 分支已删除，其内容全部保留在 custom-cursor-controller 的 main 历史中

## 三、在新设备继续工作

```bash
git clone https://github.com/uyuyuyuyuyi/custom-cursor-controller.git
cd custom-cursor-controller
git checkout main
git pull
python -m pip install -r requirements.txt -r requirements-ai.txt
# 仅当需要修改前端时：
cd webui && npm install
```

运行：`python custom_cursor_gui.pyw`

注意：**Codex 对话历史不随 git 同步**。新设备上开新对话时，把本文件指给 agent 即可恢复上下文。

## 四、常用命令

- 运行：`python custom_cursor_gui.pyw [--smoke|--demo|--verify]`
- 后端 API 测试：`PYTHONIOENCODING=utf-8 python test_web_api.py`（118 项；会真实切换系统光标并写注册表，运行后自动恢复）
- AI 抠图测试：`python -m unittest -v test_onnx_cutout.py`
- 其他回归：`python test_image_quality.py`、`python test_path_traversal.py`、`python test_concurrent_save.py`
- 前端构建：`cd webui && npm run build`（产物 `webui/dist` 需提交入库）
- 打包（**用户手动执行**）：`python -m PyInstaller CustomCursorController.spec --noconfirm`
- 旧打包方案（Nuitka）：`python build.py`，产物在 `build/nuitka/` 下

## 五、关键实现与近期改动

### 1. 实际光标大小滑动条（32~256，16px 一档）

- `cursor_manager.replace_cursor()` 必须用 `LoadImageW`（显式目标尺寸）+ `SetSystemCursor`。
- **不要改回 `LoadCursorFromFileW`**：在本机（DPI 150%/175% 缩放）它会把所有尺寸的 `.ani/.cur` 统一缩到系统指针大小（32 或 48px），滑动条因此看起来“无效”。
- `SPI_SETCURSORS` 只重载光标方案、不重算指针缩放；注册表 `CursorBaseSize` / `Accessibility\CursorSize` 仅用于持久化，不能实时生效。
- `LoadImageW` 已验证保留动画帧与热点；`CopyImage` 会把动画压成单帧，不要用。
- 前端在 `webui/src/App.vue`：滑动条绑定 `state.appliedSize`，`@change` 调用 `setAppliedSize()`；不要给该函数传事件对象（会把 `size` 变成 dict 导致后端 `int()` 报错）。

### 2. AI 智能抠图（`onnx_cutout.py`）

- 模型懒下载/懒加载，默认 fp16，许可 Apache-2.0。
- 运行时只依赖 `onnxruntime` + `numpy`；**torch / scipy / pandas / matplotlib 等绝不能打进包**（`CustomCursorController.spec` 的 `excludes` 已配置，否则单文件包会从 ~46MB 膨胀到 ~250MB）。
- AI 结果会刷新图库快照；AI 源帧从原始上传重建，避免在已抠好的小图上二次抠图。

### 3. 图库

- 上传原图与生成的光标都入库（`data/` 运行时生成，已 gitignore）。
- 应用图库光标后，评分和 AI 按钮保留；删除/级联删除会恢复系统光标。

### 4. 打包体积策略

- `CustomCursorController.spec` 已排除 `onnxruntime.transformers/quantization/tools`、`torch`、`tensorflow`、`scipy`、`pandas`、`matplotlib` 等。
- `webui/dist` 提交入库，打包时直接包含，无需在新设备重建前端（除非改了 `webui/src`）。

## 六、已知事项 / 未完成

- 滑动条数值目前按**物理像素**生成位图；若用户希望匹配 Windows“逻辑像素”语义，需要按 `GetDpiForSystem` 缩放（本机 DPI 感知前后分别为 96 / 168，`SM_CXCURSOR` 感知后为 48）。当前实现未做该换算。
- `SetSystemCursor` 对 `.ani` 动画播放存在 Windows 已知限制（外部资料），当前实现已验证帧数据保留，但实际动画表现需在目标机器确认。
- `.test_tmp/`、`test_res/新建 文本文档.txt` 是本地临时文件，**不要提交**。
- `dist/`、`build/` 是构建产物（已 gitignore），打包由用户手动完成。

## 七、工作习惯

- 涉及系统光标 / 注册表的测试会短暂改变用户鼠标指针，跑 `test_web_api.py` 前先说明。
- commit / push / merge 等 Git 写操作需用户明确要求后再执行。
- 用户偏好：打包 exe 前先告知，由用户手动执行，不要替用户自动打包。
