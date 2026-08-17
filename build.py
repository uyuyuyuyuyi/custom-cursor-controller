"""build.py — 受保护发布构建：Nuitka 将全部核心代码编译为原生二进制。

用法:
    python build.py                # standalone 目录版（推荐，稳定）
    python build.py --onefile      # 单文件版（启动时解压，可能被杀软误报）

说明:
    - 需要 C 编译器：自动检测 VS Build Tools / MSVC；没有时可追加
      --mingw64 让 Nuitka 自动下载 MinGW。
    - 构建产物默认在 build/nuitka/ 下，全部已由 .gitignore 排除。
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def main() -> int:
    # 缓存放到项目内，避免写入用户目录
    cache_dir = ROOT / "build" / "nuitka-cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    os.environ["NUITKA_CACHE_DIR"] = str(cache_dir)

    venv_python = ROOT / ".venv" / "Scripts" / "python.exe"
    python = str(venv_python) if venv_python.exists() else shutil.which("python")

    cmd = [
        python, "-m", "nuitka",
        "--standalone",
        "--assume-yes-for-downloads",
        "--disable-plugin=pywebview",
        "--output-dir=build/nuitka",
        "--output-filename=CustomCursorController.exe",
        "--windows-console-mode=disable",
        "--windows-icon-from-ico=assets/app.ico",
        "--include-data-dir=webui/dist=webui/dist",
        "--include-module=webview.platforms.winforms",
        "--include-module=webview.platforms.win32",
        "--include-module=webview.platforms.edgechromium",
        "--include-module=webview.platforms.mshtml",
        "--include-package=clr_loader",
        "--include-package=pythonnet",
        "--include-package=pystray",
        "--include-package=pystray._win32",
        "--include-package-data=webview",
        "--include-package-data=clr_loader",
        "--include-package-data=pythonnet",
        "--include-package-data=pystray",
        *sys.argv[1:],
        "custom_cursor_gui.pyw",
    ]
    print("执行:", " ".join(cmd))
    result = subprocess.run(cmd, cwd=ROOT)
    if result.returncode != 0:
        print(
            "\n构建失败。常见原因: 缺少 C 编译器（可追加 --mingw64），"
            "或某个依赖包未被完整跟随。"
        )
        return result.returncode

    standalone_exe = ROOT / "build" / "nuitka" / "custom_cursor_gui.dist" / "CustomCursorController.exe"
    onefile_exe = ROOT / "build" / "nuitka" / "CustomCursorController.exe"
    if standalone_exe.exists():
        print(f"\n完成: {standalone_exe}")
        print("分发整个目录:", standalone_exe.parent)
        return 0
    if onefile_exe.exists():
        print(f"\n完成: {onefile_exe}")
        return 0

    print("\n构建命令已结束，但未找到预期产物，请检查输出目录。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
