"""
make_custom_cursor.py — 用你自己的 PNG 图案生成自定义光标

把任意 PNG（或一组 PNG 作为动画帧）转换成程序运行时加载的
resources\\cockroach.ani（动画光标）+ resources\\tray_icon.png（托盘图标），
从而把蟑螂图案替换成你自己的图案，无需改动任何绘制代码。

用法:
    # 静态光标（一张图）
    python make_custom_cursor.py my_pattern.png

    # 动画光标（多张图按命令行顺序作为帧）
    python make_custom_cursor.py frame0.png frame1.png frame2.png ...

常用选项:
    --size 48          光标画布尺寸（默认 48×48，图像按比例缩放并居中）
    --hotspot X Y      点击热点坐标，默认画布中心 (size//2, size//2)
    --duration-ms 150  动画每帧时长（毫秒，默认 150）
    --out PATH         .ani 输出路径（默认 resources/cockroach.ani）
    --tray-out PATH    托盘图标输出路径（默认 resources/tray_icon.png）
    --tray-size N      托盘图标尺寸（默认 64）

生成后直接运行 cockroach_cursor.pyw 即可使用新光标。
注意:
    1. 程序只在 resources\\cockroach.ani 不存在时才自动生成，
       因此本脚本会"覆盖"写入这两个文件，让程序使用你的图案。
    2. 若使用打包好的 exe（dist\\CockroachCursor.exe），资源是内嵌的，
       需重新打包: pyinstaller CockroachCursor.spec --noconfirm
       或直接用源码运行 cockroach_cursor.pyw。
"""

import argparse
import os
import sys

from PIL import Image, ImageDraw

from ani_builder import save_ani_file

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_ANI = os.path.join(BASE_DIR, "resources", "cockroach.ani")
DEFAULT_TRAY = os.path.join(BASE_DIR, "resources", "tray_icon.png")


# ── 图像处理 ──────────────────────────────────────────

def _load_image(path: str) -> Image.Image:
    """加载图片并转为 RGBA。"""
    if not os.path.exists(path):
        sys.exit(f"错误: 找不到图片文件: {path}")
    img = Image.open(path)
    if img.mode != "RGBA":
        img = img.convert("RGBA")
    return img


def _fit_to_canvas(img: Image.Image, size: int) -> Image.Image:
    """
    按比例缩放图片使其完整放入 size×size 透明画布，并居中。

    返回: 归一化后的 size×size 透明画布图像
    """
    scale = min(size / img.width, size / img.height, 1.0)
    new_w = max(1, round(img.width * scale))
    new_h = max(1, round(img.height * scale))
    scaled = img.resize((new_w, new_h), Image.LANCZOS)

    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    offset_x = (size - new_w) // 2
    offset_y = (size - new_h) // 2
    canvas.paste(scaled, (offset_x, offset_y), scaled)
    return canvas


def _make_tray_icon(frame: Image.Image, size: int) -> Image.Image:
    """生成托盘图标：淡色圆形背景 + 居中图案（与 cursor_drawer 同风格）。"""
    icon = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    scaled = frame.resize((int(size * 0.85), int(size * 0.85)), Image.LANCZOS)
    offset_x = (size - scaled.width) // 2
    offset_y = (size - scaled.height) // 2

    bg_draw = ImageDraw.Draw(icon)
    margin = 3
    bg_draw.ellipse(
        (margin, margin, size - margin, size - margin),
        fill=(60, 55, 50, 200),
    )
    icon.paste(scaled, (offset_x, offset_y), scaled)
    return icon


# ── 主流程 ────────────────────────────────────────────

def main() -> None:
    # 兼容 Windows GBK 控制台：统一以 UTF-8 输出，避免 UnicodeEncodeError
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    parser = argparse.ArgumentParser(
        description="用 PNG 图案生成自定义光标 (.ani) 和托盘图标",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("images", nargs="+", metavar="PNG",
                        help="一张 PNG = 静态光标；多张 PNG = 动画帧（按顺序）")
    parser.add_argument("--size", type=int, default=48,
                        help="光标画布尺寸（像素）")
    parser.add_argument("--hotspot", nargs=2, type=int, metavar=("X", "Y"),
                        default=None, help="点击热点坐标（默认画布中心）")
    parser.add_argument("--duration-ms", type=int, default=150,
                        help="动画每帧时长（毫秒）")
    parser.add_argument("--out", default=DEFAULT_ANI,
                        help=".ani 输出路径（将覆盖已有文件）")
    parser.add_argument("--tray-out", default=DEFAULT_TRAY,
                        help="托盘图标输出路径（将覆盖已有文件）")
    parser.add_argument("--tray-size", type=int, default=64,
                        help="托盘图标尺寸（像素）")
    args = parser.parse_args()

    if args.size <= 0 or args.tray_size <= 0:
        sys.exit("错误: --size / --tray-size 必须为正整数")

    # 1) 加载并归一化所有帧到统一画布
    frames = []
    for path in args.images:
        frames.append(_fit_to_canvas(_load_image(path), args.size))
    print(f"已加载 {len(frames)} 帧 -> 统一为 {args.size}×{args.size} 画布")

    # 2) 热点
    if args.hotspot is not None:
        hotspot = tuple(args.hotspot)
    else:
        hotspot = (args.size // 2, args.size // 2)
        print(f"提示: 未指定热点，默认使用画布中心 {hotspot}；"
              f"箭头类图案建议设到尖端，如 --hotspot {args.size // 4} {args.size // 4}")
    hotspots = [hotspot] * len(frames)
    durations = [args.duration_ms] * len(frames)

    # 3) 构建 .ani
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    save_ani_file(
        args.out,
        frames=frames,
        hotspots=hotspots,
        frame_durations_ms=durations,
        title="Custom Cursor",
        author="Custom",
    )
    ani_size = os.path.getsize(args.out)
    print(f"已生成动画光标: {args.out} ({ani_size} bytes, "
          f"{len(frames)} 帧, 热点 {hotspot})")

    # 4) 生成托盘图标
    os.makedirs(os.path.dirname(os.path.abspath(args.tray_out)), exist_ok=True)
    _make_tray_icon(frames[0], args.tray_size).save(args.tray_out)
    print(f"已生成托盘图标: {args.tray_out}")

    # 5) 校验 .ani 头
    with open(args.out, "rb") as f:
        header = f.read(12)
    assert header[:4] == b"RIFF", "生成的 .ani 不是合法 RIFF 文件!"
    assert header[8:12] == b"ACON", "生成的 .ani 不是合法 ACON 文件!"
    print("RIFF/ACON 校验: 通过")

    # 6) 后续提示
    print("\n完成！下一步：")
    print("  - 源码运行: 直接运行 cockroach_cursor.pyw")
    print("  - 打包版:   先重新打包 pyinstaller CockroachCursor.spec --noconfirm，"
          "再运行 dist\\CockroachCursor.exe")


if __name__ == "__main__":
    main()
