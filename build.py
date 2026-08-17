"""build.py — 受保护发布构建：PyArmor 混淆全部核心脚本，再经现有 spec 打包。

用法:
    python build.py                # 默认构建
    python build.py --mix-str      # 追加 PyArmor 加密选项（需正式许可证）

注意:
    PyArmor 试用版无法混淆本项目的大脚本（gui_server.py / pointer_analyzer.py），
    会报 out of license。请先注册正式许可证：
        pyarmor reg -p non-profits pyarmor-regcode-xxx.txt
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SPEC = "CustomCursorController.spec"
SCRIPTS = [
    "custom_cursor_gui.pyw",
    "gui_server.py",
    "pointer_analyzer.py",
    "ani_builder.py",
    "cursor_manager.py",
]


def main() -> int:
    # 把 PyArmor 的配置/缓存隔离到项目内，避免写入用户主目录
    os.environ.setdefault("PYARMOR_HOME", str(ROOT / ".pyarmor_home"))

    pyarmor = shutil.which("pyarmor")
    if pyarmor is None:
        fallback = ROOT / ".venv" / "Scripts" / "pyarmor.exe"
        if fallback.exists():
            pyarmor = str(fallback)
        else:
            print("未找到 pyarmor，请先执行: pip install -r requirements-build.txt")
            return 1

    cmd = [pyarmor, "gen", "--pack", SPEC, "-r", *SCRIPTS, *sys.argv[1:]]
    print("执行:", " ".join(cmd))
    result = subprocess.run(cmd, cwd=ROOT)
    if result.returncode != 0:
        print(
            "\n构建失败。若提示 out of license，说明当前是 PyArmor 试用版，"
            "无法混淆大脚本；请先注册正式许可证后重试。"
        )
        return result.returncode

    print("\n完成: dist\\CustomCursorController.exe")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
