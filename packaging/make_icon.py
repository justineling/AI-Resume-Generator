#!/usr/bin/env python3
"""生成桌面应用图标（1024x1024 PNG + 多尺寸 ICO）。

图形语言与前端 favicon / Ark UI 保持一致：近黑底 ``#080a0b`` + 青色 ``#18d1ff``
三根柱状（简历 / 数据 / 信号），方角、无圆角。

用法：
    .venv/Scripts/python.exe packaging/make_icon.py [输出目录]

默认输出到 ``packaging/build/``（该目录已在 .gitignore 中），产出：
    packaging/build/icon-1024.png   供 `npx tauri icon` 生成全套平台图标
    packaging/build/icon.ico        供 PyInstaller 作为 exe 图标
"""
from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw

#: 画布尺寸（Tauri / macOS 图标要求至少 1024）。
SIZE = 1024

#: Ark 语义色（与 backend/app/static/css/app.css 的 :root 一致）。
INK = (0x08, 0x0a, 0x0b, 255)      # 近黑底
SIGNAL = (0x18, 0xd1, 0xff, 255)   # 青色信号色

#: 三根柱子的几何参数：(x, 顶部 y, 宽度, 不透明度)，按 favicon 的 12/20/15 比例放大。
BAR_WIDTH = 160
BAR_GAP = 80
BAR_BOTTOM = 800
BARS: tuple[tuple[int, int, int, float], ...] = (
    (192, 416, BAR_WIDTH, 0.55),   # 12/32 * 1024 ≈ 384 高
    (432, 160, BAR_WIDTH, 1.00),   # 20/32 * 1024 = 640 高
    (672, 320, BAR_WIDTH, 0.75),   # 15/32 * 1024 ≈ 480 高
)

#: 边框粗细与不透明度（细线是 Ark 家族的标志性元素）。
BORDER_WIDTH = 10
BORDER_ALPHA = 0.18

#: ICO 需要包含的尺寸。
ICO_SIZES = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]


def _with_alpha(color: tuple[int, int, int, int], alpha: float) -> tuple[int, int, int, int]:
    """按不透明度合成颜色（直接改 alpha 通道即可，合成时再叠加到底色上）。"""
    return (color[0], color[1], color[2], int(round(255 * max(0.0, min(1.0, alpha)))))


def build_icon(size: int = SIZE) -> Image.Image:
    """绘制图标并返回 RGBA 图像。"""
    canvas = Image.new("RGBA", (size, size), INK)
    overlay = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    # 外框：1px 细线的放大版，方角
    half = BORDER_WIDTH // 2
    draw.rectangle(
        [(half, half), (size - 1 - half, size - 1 - half)],
        outline=_with_alpha(SIGNAL, BORDER_ALPHA),
        width=BORDER_WIDTH,
    )

    # 三根柱状
    for left, top, width, alpha in BARS:
        draw.rectangle(
            [(left, top), (left + width - 1, BAR_BOTTOM - 1)],
            fill=_with_alpha(SIGNAL, alpha),
        )

    # 底部基线：把三根柱子「坐」在同一条线上
    draw.rectangle(
        [(BARS[0][0], BAR_BOTTOM), (BARS[-1][0] + BAR_WIDTH - 1, BAR_BOTTOM + BORDER_WIDTH - 1)],
        fill=_with_alpha(SIGNAL, 0.9),
    )

    return Image.alpha_composite(canvas, overlay)


def main(argv: list[str]) -> int:
    out_dir = Path(argv[1]).resolve() if len(argv) > 1 else Path(__file__).resolve().parent / "build"
    out_dir.mkdir(parents=True, exist_ok=True)

    icon = build_icon()
    png_path = out_dir / "icon-1024.png"
    icon.save(png_path, format="PNG")
    print(f"[make_icon] PNG  -> {png_path}")

    ico_path = out_dir / "icon.ico"
    icon.save(ico_path, format="ICO", sizes=ICO_SIZES)
    print(f"[make_icon] ICO  -> {ico_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
