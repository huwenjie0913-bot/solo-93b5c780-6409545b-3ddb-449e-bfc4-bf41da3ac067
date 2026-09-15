"""按打印机 DPI 生成标签 PNG 预览（真实点阵条纹）。"""

from __future__ import annotations

import base64
import io

from PIL import Image, ImageDraw, ImageFont

from .geometry import mm_to_dots
from .schemas import LabelSpec, LayoutSpec, PrinterSpec

MAX_PIXELS = 4_000_000


def render_preview(
    label: LabelSpec,
    printer: PrinterSpec,
    layout: LayoutSpec,
    analysis: dict,
) -> str:
    printer_scale = printer.dpi / 25.4  # 打印点/mm
    shrink = 1.0
    w_px = max(1, round(label.width_mm * printer_scale))
    h_px = max(1, round(label.height_mm * printer_scale))
    if w_px * h_px > MAX_PIXELS:
        shrink = (MAX_PIXELS / (w_px * h_px)) ** 0.5
    # 背景、目标框、条纹共用同一缩放比例
    scale = printer_scale * shrink
    w_px = max(1, round(label.width_mm * scale))
    h_px = max(1, round(label.height_mm * scale))

    img = Image.new("RGB", (w_px, h_px), "white")
    draw = ImageDraw.Draw(img, "RGBA")
    draw.rectangle([0, 0, w_px - 1, h_px - 1], outline="black")

    def rect_mm(bb, **kw):
        draw.rectangle(
            [bb[0] * scale, bb[1] * scale, bb[2] * scale, bb[3] * scale], **kw
        )

    # 裁切禁区：红色半透明
    for z in layout.forbidden_zones:
        rect_mm(
            (z.x_mm, z.y_mm, z.x_mm + z.width_mm, z.y_mm + z.height_mm),
            fill=(255, 0, 0, 60),
            outline=(200, 0, 0, 200),
        )

    rd = analysis["_render"]
    # 静区范围：黄色半透明
    rect_mm(rd["expanded_bbox"], fill=(255, 220, 0, 40))
    # 条码框：蓝色描边
    rect_mm(rd["bbox"], outline=(0, 80, 255, 220), width=2)

    # 文字块
    try:
        font = ImageFont.load_default()
    except Exception:
        font = None
    for t in layout.texts:
        rect_mm(
            (t.x_mm, t.y_mm, t.x_mm + t.width_mm, t.y_mm + t.height_mm),
            outline=(120, 120, 120, 255),
        )
        if font:
            draw.text((t.x_mm * scale + 2, t.y_mm * scale + 2),
                      t.content[:24], fill=(90, 90, 90), font=font)

    # 条码条纹：先按打印机原始分辨率绘制真实点阵，旋转后统一收缩到预览比例
    raster = rd["raster"]
    box = layout.barcode.box
    total_dots = raster[-1]["start_dot"] + raster[-1]["dots"] if raster else 1
    bar_h = max(1, round(box.height_mm * printer_scale))
    overlay = Image.new("RGBA", (max(1, total_dots), bar_h), (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    min_dots = min(r["dots"] for r in raster if r["is_bar"] and r["dots"] > 0)
    for r in raster:
        if not r["is_bar"] or r["dots"] <= 0:
            continue
        od.rectangle([r["start_dot"], 0, r["start_dot"] + r["dots"] - 1, bar_h - 1],
                     fill=(0, 0, 0, 255))
        if r["dots"] == min_dots:  # 高亮最窄条
            od.rectangle(
                [r["start_dot"], 0, r["start_dot"] + r["dots"] - 1, bar_h - 1],
                outline=(255, 0, 0, 255),
            )
    angle = box.rotation_deg
    if angle % 360:
        # PIL 逆时针为正；本系统顺时针为正
        overlay = overlay.rotate(-angle, expand=True, resample=Image.NEAREST,
                                 fillcolor=(0, 0, 0, 0))
    if shrink != 1.0:
        overlay = overlay.resize(
            (max(1, round(overlay.width * shrink)),
             max(1, round(overlay.height * shrink))),
            resample=Image.NEAREST,
        )
    bb = rd["bbox"]
    img.paste(overlay, (round(bb[0] * scale), round(bb[1] * scale)), overlay)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")
