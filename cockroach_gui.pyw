"""
cockroach_gui.pyw — 自定义光标控制器（Windows GUI）

双击运行进入图形界面，功能:
  • 开关: 一键启用/停用自定义光标（启用后替换系统箭头，退出时自动恢复）
  • 上传: 选择图片，程序自动判断是否适合做光标（pointer_analyzer），
          生成 .ani 光标文件并预览
  • 热点: 点击预览图可设置鼠标"点击点"位置
  • 托盘: 关闭窗口最小化到托盘，右键菜单可打开主界面 / 切换 / 退出

用法:
    pythonw cockroach_gui.pyw       # 正常启动
    python cockroach_gui.pyw --smoke   # 冒烟测试: 3 秒后自动退出
"""

import atexit
import os
import sys
import tempfile
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext

import pystray
from PIL import Image, ImageDraw, ImageTk

from ani_builder import save_ani_file
from pointer_analyzer import Analysis, analyze, auto_remove_background
from cockroach_cursor import CursorManager

# ── 路径 ──────────────────────────────────────────────
# 生成的光标文件写到临时目录（打包版 exe 可能装在只读目录）
TEMP_DIR = os.path.join(tempfile.gettempdir(), "cockroach_cursor_gui")
ANI_PATH = os.path.join(TEMP_DIR, "custom.ani")
CANVAS_SIZE = 48          # 光标画布尺寸
FRAME_MS = 150            # 动画帧时长
PREVIEW_SCALE = 4         # 预览缩放倍率

# 硬性拒绝的错误码（空白/过小/无法抠背景）不允许强制使用
HARD_REJECT = {"EMPTY", "TOO_SMALL", "NO_BG"}


# ── 图像工具 ──────────────────────────────────────────

def _fit_to_canvas(img: Image.Image, size: int) -> tuple[Image.Image, float, int, int]:
    """等比缩放放入 size×size 透明画布并居中。返回 (画布, 缩放比, 偏移x, 偏移y)。"""
    scale = min(size / img.width, size / img.height, 1.0)
    new_w = max(1, round(img.width * scale))
    new_h = max(1, round(img.height * scale))
    scaled = img.resize((new_w, new_h), Image.LANCZOS)
    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    ox, oy = (size - new_w) // 2, (size - new_h) // 2
    canvas.paste(scaled, (ox, oy), scaled)
    return canvas, scale, ox, oy


def _make_placeholder_icon(size: int = 64) -> Image.Image:
    """无图案时的托盘占位图标。"""
    icon = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(icon)
    m = 6
    d.ellipse((m, m, size - m, size - m), fill=(60, 55, 50, 200))
    d.ellipse((size * 0.3, size * 0.3, size * 0.7, size * 0.7), fill=(230, 225, 220, 230))
    return icon


# ── GUI ───────────────────────────────────────────────

class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.mgr = CursorManager()
        self.mgr.backup_original()

        self.frames: list[Image.Image] = []       # 已处理的 48×48 帧
        self.hotspot = (CANVAS_SIZE // 2, CANVAS_SIZE // 2)
        self.enabled = False                      # 当前是否已替换系统光标
        self.last_verdict: Analysis | None = None
        self._preview_img: ImageTk.PhotoImage | None = None
        self._tray_icon = None

        self._build_ui()
        try:
            self._setup_tray()
        except Exception:
            # 托盘不可用时（如无托盘环境），关闭窗口 = 直接退出
            self._tray_icon = None

        # 退出时恢复系统光标
        atexit.register(self._cleanup)

    # ── 界面 ──────────────────────────────────────────

    def _build_ui(self) -> None:
        r = self.root
        r.title("自定义光标控制器")
        r.geometry("640x640")
        r.minsize(560, 560)

        try:
            from tkinter import font as tkfont
            tkfont.nametofont("TkDefaultFont").configure(family="Microsoft YaHei UI", size=10)
        except Exception:
            pass

        pad = {"padx": 12, "pady": 6}

        # 开关
        self.toggle_var = tk.BooleanVar(value=False)
        self.toggle_btn = tk.Checkbutton(
            r, text="启用自定义光标", font=("Microsoft YaHei UI", 13, "bold"),
            variable=self.toggle_var, command=self._on_toggle, padx=8, pady=4,
        )
        self.toggle_btn.pack(anchor="w", **pad)

        self.status_lbl = tk.Label(r, text="● 未启用 — 系统默认光标", fg="#888888",
                                   font=("Microsoft YaHei UI", 10))
        self.status_lbl.pack(anchor="w", **pad)

        # 预览区（棋盘格底 + 图案 + 热点标记）
        preview_frame = tk.LabelFrame(r, text=" 预览（点击可设置热点位置） ", padx=8, pady=8)
        preview_frame.pack(fill="x", **pad)
        self.canvas = tk.Canvas(preview_frame, width=CANVAS_SIZE * PREVIEW_SCALE,
                                height=CANVAS_SIZE * PREVIEW_SCALE,
                                bg="#c8c8c8", highlightthickness=1,
                                highlightbackground="#999999")
        self.canvas.pack()
        self.canvas.bind("<Button-1>", self._on_canvas_click)
        self.info_lbl = tk.Label(preview_frame, text="尚未上传图片", fg="#666666")
        self.info_lbl.pack(anchor="w", pady=(6, 0))

        # 操作按钮
        btn_row = tk.Frame(r)
        btn_row.pack(fill="x", **pad)
        self.upload_btn = tk.Button(btn_row, text="上传图片并生成光标…",
                                    command=self._upload, padx=10, pady=4)
        self.upload_btn.pack(side="left")
        self.restore_btn = tk.Button(btn_row, text="恢复默认光标",
                                     command=self._restore, padx=10, pady=4)
        self.restore_btn.pack(side="left", padx=(10, 0))

        # 分析报告
        report_frame = tk.LabelFrame(r, text=" 图片适合度分析 ", padx=8, pady=8)
        report_frame.pack(fill="both", expand=True, **pad)
        self.report = scrolledtext.ScrolledText(report_frame, height=8, width=70,
                                                state="disabled", wrap="word",
                                                font=("Consolas", 9))
        self.report.pack(fill="both", expand=True)

        # 底部说明
        tk.Label(r, text="提示：支持 PNG/JPG/BMP/GIF/WebP；选择多张图片可生成动画光标。"
                         "程序退出时会自动恢复系统默认光标。",
                 fg="#777777", justify="left").pack(anchor="w", **pad)

        self._draw_preview()

    def _set_report(self, text: str) -> None:
        self.report.configure(state="normal")
        self.report.delete("1.0", "end")
        self.report.insert("1.0", text)
        self.report.configure(state="disabled")

    def _set_status(self, text: str, color: str) -> None:
        self.status_lbl.configure(text=text, fg=color)

    # ── 预览绘制 ──────────────────────────────────────

    def _draw_preview(self) -> None:
        """绘制预览: 棋盘格底 + 当前帧(4倍) + 热点十字。"""
        c = self.canvas
        c.delete("all")
        s = PREVIEW_SCALE
        n = CANVAS_SIZE * s
        # 棋盘格
        cell = 12
        for y in range(0, n, cell):
            for x in range(0, n, cell):
                color = "#e8e8e8" if ((x // cell) + (y // cell)) % 2 == 0 else "#ffffff"
                c.create_rectangle(x, y, x + cell, y + cell, fill=color, outline="")
        # 图案
        if self.frames:
            img = self.frames[0].resize((n, n), Image.NEAREST)
            self._preview_img = ImageTk.PhotoImage(img)
            c.create_image(0, 0, image=self._preview_img, anchor="nw")
        # 热点十字
        hx, hy = self.hotspot[0] * s, self.hotspot[1] * s
        c.create_line(hx - 8, hy, hx + 8, hy, fill="#ff3030", width=2)
        c.create_line(hx, hy - 8, hx, hy + 8, fill="#ff3030", width=2)
        c.create_oval(hx - 3, hy - 3, hx + 3, hy + 3, outline="#ff3030", width=1)

        if self.frames:
            anim = f"动画 {len(self.frames)} 帧 · " if len(self.frames) > 1 else ""
            self.info_lbl.configure(
                text=f"{anim}热点 ({self.hotspot[0]}, {self.hotspot[1]}) — "
                     f"点击预览可调整")
        else:
            self.info_lbl.configure(text="尚未上传图片")

    def _on_canvas_click(self, event: tk.Event) -> None:
        if not self.frames:
            return
        x = min(CANVAS_SIZE - 1, max(0, event.x // PREVIEW_SCALE))
        y = min(CANVAS_SIZE - 1, max(0, event.y // PREVIEW_SCALE))
        self.hotspot = (x, y)
        self._draw_preview()
        self._rebuild_ani()
        if self.enabled:
            self._apply_cursor()

    # ── 图片上传与判断 ────────────────────────────────

    def _upload(self) -> None:
        paths = filedialog.askopenfilenames(
            title="选择图片（多选=动画帧）",
            filetypes=[("图片", "*.png *.jpg *.jpeg *.bmp *.gif *.webp"),
                       ("所有文件", "*.*")])
        if not paths:
            return

        # 1) 逐张处理: 加载 → 转 RGBA → 无透明通道则自动抠背景
        processed: list[tuple[Image.Image, bool]] = []   # (图, 背景是否OK)
        for p in paths:
            try:
                img = Image.open(p)
            except Exception as e:
                messagebox.showerror("无法打开", f"无法打开 {os.path.basename(p)}:\n{e}")
                return
            if img.mode != "RGBA":
                img = img.convert("RGBA")
            has_alpha = img.getchannel("A").getextrema()[0] < 250
            if has_alpha:
                processed.append((img, True))
            else:
                out, ok = auto_remove_background(img)
                processed.append((out, ok))

        # 2) 逐张分析，取最差结论
        verdicts: list[Analysis] = []
        for img, bg_ok in processed:
            has_alpha = img.getchannel("A").getextrema()[0] < 250
            removal = None if has_alpha else bg_ok   # None=自带透明 / True=抠图成功 / False=失败
            verdicts.append(analyze(img, removal_ok=removal))

        worst = min(verdicts, key=lambda a: a.score)
        self.last_verdict = worst

        # 3) 判断结果展示与拦截
        report_lines = [f"共 {len(paths)} 张图片，综合结论: {worst.verdict}"
                        f"（评分 {worst.score}/100）", ""]
        for idx, v in enumerate(verdicts):
            report_lines.append(f"--- {os.path.basename(paths[idx])} "
                                f"[{v.verdict} {v.score}分]")
            report_lines.append(v.issues_text())
        report = "\n".join(report_lines)
        self._set_report(report)

        hard_errors = [it for it in worst.issues
                       if it.level == "error" and it.code in HARD_REJECT]
        if worst.verdict == "不适合":
            if hard_errors:
                messagebox.showerror(
                    "图片不适合做光标",
                    "这张图片不适合用作鼠标指针：\n\n" +
                    "\n".join(f"• {it.message}" for it in hard_errors) +
                    "\n\n请换一张图案清晰、有透明背景（或纯色背景）的图片。")
                return
            if not messagebox.askyesno(
                    "图片不适合做光标",
                    "分析认为这张图片不适合用作鼠标指针：\n\n" +
                    "\n".join(f"• {it.message}" for it in worst.issues) +
                    "\n\n仍然生成并使用吗？"):
                return
        elif worst.verdict == "有风险":
            if not messagebox.askyesno(
                    "图片可能不适合",
                    "这张图片存在以下风险：\n\n" +
                    "\n".join(f"• {it.message}" for it in worst.issues) +
                    "\n\n仍然继续吗？"):
                return

        # 4) 统一缩放到 48×48 画布
        frames = []
        for img, _bg in processed:
            canvas, _scale, _ox, _oy = _fit_to_canvas(img, CANVAS_SIZE)
            frames.append(canvas)
        self.frames = frames

        # 热点默认 = 画布上图案 bbox 中心（用户可在预览上点击调整）
        self.hotspot = analyze(frames[0]).hotspot

        self._draw_preview()
        self._rebuild_ani()
        self._update_tray_icon()

        # 5) 自动启用
        if not self.enabled:
            self.toggle_var.set(True)
            self._on_toggle()
        else:
            self._apply_cursor()

    # ── 光标应用/恢复 ─────────────────────────────────

    def _rebuild_ani(self) -> None:
        """把当前帧 + 热点写入 .ani 文件。"""
        os.makedirs(TEMP_DIR, exist_ok=True)
        save_ani_file(
            ANI_PATH,
            frames=self.frames,
            hotspots=[self.hotspot] * len(self.frames),
            frame_durations_ms=[FRAME_MS] * len(self.frames),
            title="Custom Cursor",
            author="Custom Cursor GUI",
        )

    def _apply_cursor(self) -> None:
        try:
            self.mgr.replace_with_cockroach(ANI_PATH)  # 加载用户生成的光标并替换系统光标
            self.enabled = True
            self._set_status("● 已启用 — 自定义光标生效中", "#2e7d32")
        except Exception as e:
            self.enabled = False
            self.toggle_var.set(False)
            self._set_status("● 启用失败", "#c62828")
            messagebox.showerror("启用失败", str(e))

    def _restore(self) -> None:
        try:
            self.mgr.restore_original()
        except Exception as e:
            try:
                self.mgr.reload_system_defaults()
            except Exception:
                messagebox.showerror("恢复失败", str(e))
                return
        self.enabled = False
        self.toggle_var.set(False)
        self._set_status("● 未启用 — 系统默认光标", "#888888")

    def _on_toggle(self) -> None:
        if self.toggle_var.get():
            if not self.frames:
                self.toggle_var.set(False)
                messagebox.showinfo("请先上传图片", "请先点击「上传图片并生成光标」。")
                return
            self._apply_cursor()
        else:
            self._restore()

    # ── 托盘 ──────────────────────────────────────────

    def _setup_tray(self) -> None:
        def _open_window(_icon=None, _item=None):
            self.root.after(0, self._show_window)

        def _toggle_from_tray(_icon=None, _item=None):
            self.root.after(0, lambda: self.toggle_var.set(not self.toggle_var.get()))
            self.root.after(50, self._on_toggle)

        def _restore_from_tray(_icon=None, _item=None):
            self.root.after(0, self._restore)

        def _quit(_icon=None, _item=None):
            self.root.after(0, self.root.destroy)

        self._tray_icon = pystray.Icon(
            name="cockroach_cursor_gui",
            title="自定义光标控制器",
            icon=_make_placeholder_icon(64),
            menu=pystray.Menu(
                pystray.MenuItem("打开主界面", _open_window, default=True),
                pystray.MenuItem("启用/停用自定义光标", _toggle_from_tray),
                pystray.MenuItem("恢复默认光标", _restore_from_tray),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("退出（恢复光标）", _quit),
            ),
        )
        threading.Thread(target=self._tray_icon.run, daemon=True).start()

    def _update_tray_icon(self) -> None:
        try:
            from make_custom_cursor import _make_tray_icon as tray_from_frame
            img = tray_from_frame(self.frames[0], 64) if self.frames else _make_placeholder_icon(64)
        except Exception:
            img = _make_placeholder_icon(64)
        if self._tray_icon is not None:
            try:
                self._tray_icon.icon = img
            except Exception:
                pass

    def _show_window(self) -> None:
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    # ── 关闭与清理 ────────────────────────────────────

    def on_close(self) -> None:
        """关闭窗口: 光标生效中 → 最小化到托盘；未生效 → 直接退出。"""
        if self.enabled:
            self.root.withdraw()
            if self._tray_icon is not None:
                try:
                    self._tray_icon.notify("程序仍在后台运行，右键托盘图标可退出",
                                           title="自定义光标控制器")
                except Exception:
                    pass
        else:
            self.root.destroy()

    def _cleanup(self) -> None:
        """退出时恢复系统光标并释放资源。"""
        try:
            if self.enabled:
                self.mgr.restore_original()
        except Exception:
            try:
                self.mgr.reload_system_defaults()
            except Exception:
                pass
        self.mgr.cleanup()


def main() -> None:
    root = tk.Tk()
    app = App(root)
    root.protocol("WM_DELETE_WINDOW", app.on_close)

    if "--smoke" in sys.argv:
        root.after(3000, root.destroy)

    root.mainloop()


if __name__ == "__main__":
    main()
