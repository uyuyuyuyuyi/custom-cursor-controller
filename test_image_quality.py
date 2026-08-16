"""真实素材抠图质量、后处理回退与 alpha 缩放回归测试。

此测试不实例化 CursorApp，因此不会替换系统光标。运行：
    python -m unittest -v test_image_quality.py
"""
from __future__ import annotations

import io
import unittest
from pathlib import Path

from PIL import Image, ImageDraw

from gui_server import CursorApp
from pointer_analyzer import auto_remove_background, estimate_background_color


ROOT = Path(__file__).resolve().parent
TEST_RES = ROOT / "test_res"


class ImageQualityTests(unittest.TestCase):
    STATIC_EXPECTED = {
        "colored_cockroach.png": "medium",
        "屏幕截图 2026-08-16 181619.png": "high",
        "屏幕截图 2026-08-16 190310.png": "low",
        "微信图片_20260816163204_118_2.jpg": "low",
        "微信图片_20260816163247_119_2.jpg": "low",
        "1.png": "high",
        "2.png": "medium",
    }

    @staticmethod
    def _remove(path: Path, size: int):
        with Image.open(path) as src:
            frame = src.convert("RGBA")
        bg = estimate_background_color(frame)
        return auto_remove_background(
            frame,
            keep_existing_alpha=True,
            bg_hint=bg,
            quality_size=size,
            return_diagnostics=True,
        )

    def test_real_static_confidence_at_all_cursor_sizes(self):
        """真实好图保持自动应用；已知难例必须降级，不能再全部判适合。"""
        for name, expected in self.STATIC_EXPECTED.items():
            for size in (48, 64, 96):
                with self.subTest(name=name, size=size):
                    _out, ok, diag = self._remove(TEST_RES / name, size)
                    self.assertTrue(ok)
                    self.assertEqual(diag.confidence, expected, diag.as_dict())
                    self.assertEqual(diag.auto_apply, expected == "high")

    def test_real_gif_frames_remain_high_confidence(self):
        """简单白底动画不能因主体较小或合法孔洞被误降级。"""
        path = TEST_RES / "cockroach-dancing.gif"
        with Image.open(path) as src:
            src.seek(0)
            bg = estimate_background_color(src.convert("RGBA"))
            for index in range(src.n_frames):
                src.seek(index)
                frame = src.convert("RGBA")
                _out, ok, diag = auto_remove_background(
                    frame,
                    keep_existing_alpha=True,
                    bg_hint=bg,
                    allow_mostly_background=True,
                    quality_size=64,
                    return_diagnostics=True,
                )
                with self.subTest(frame=index):
                    self.assertTrue(ok)
                    self.assertEqual(diag.confidence, "high", diag.as_dict())
                    self.assertTrue(diag.auto_apply)

    def test_aggressive_postfill_rolls_back_on_known_damage(self):
        for name in (
            "colored_cockroach.png",
            "屏幕截图 2026-08-16 190310.png",
            "微信图片_20260816163247_119_2.jpg",
            "2.png",
        ):
            with self.subTest(name=name):
                _out, ok, diag = self._remove(TEST_RES / name, 64)
                self.assertTrue(ok)
                self.assertNotEqual(diag.selected_stage, "aggressive")
                self.assertGreater(diag.postfill_removed_ratio, 0.08)
                self.assertTrue(diag.fallback_used)

    def test_legacy_two_value_contract_is_preserved(self):
        with Image.open(TEST_RES / "1.png") as src:
            result = auto_remove_background(src.convert("RGBA"))
        self.assertEqual(len(result), 2)

    def test_half_transparent_color_is_not_alpha_squared(self):
        source = Image.new("RGBA", (4, 4), (255, 0, 0, 128))
        fitted = CursorApp._fit_to_canvas(source, 8)
        self.assertEqual(fitted.getpixel((3, 3)), (255, 0, 0, 128))

    def test_antialiased_white_edge_has_no_dark_fringe(self):
        # 先在大图绘制并缩小，制造真实的半透明抗锯齿边缘。
        large = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
        draw = ImageDraw.Draw(large)
        draw.ellipse((24, 24, 232, 232), fill=(255, 255, 255, 255))
        source = large.resize((96, 96), Image.Resampling.LANCZOS)
        fitted = CursorApp._fit_to_canvas(source, 64)
        pixel_data = (fitted.get_flattened_data()
                      if hasattr(fitted, "get_flattened_data") else fitted.getdata())
        edge_pixels = [
            (r, g, b, a) for r, g, b, a in pixel_data
            if 0 < a < 255
        ]
        self.assertTrue(edge_pixels)
        self.assertTrue(
            all(min(r, g, b) >= 250 for r, g, b, _a in edge_pixels),
            min(edge_pixels),
        )

    def test_backend_quality_summary_controls_auto_apply(self):
        # 绕过 __init__，避免创建 CursorManager/数据目录；_process_files 只需要 canvas_size。
        app = object.__new__(CursorApp)
        app.canvas_size = 64
        cases = {
            "1.png": ("适合", True, "high"),
            "2.png": ("有风险", False, "medium"),
            "微信图片_20260816163204_118_2.jpg": ("不适合", False, "low"),
        }
        for name, expected in cases.items():
            data = (TEST_RES / name).read_bytes()
            processed = app._process_files([(name, data)])
            _frames, _durations, _info, _verdicts, worst, diags = processed
            summary = app._quality_summary(worst, diags)
            with self.subTest(name=name):
                self.assertEqual(summary["verdict"], expected[0])
                self.assertEqual(summary["auto_apply"], expected[1])
                self.assertEqual(summary["removal_confidence"], expected[2])


if __name__ == "__main__":
    unittest.main(verbosity=2)
