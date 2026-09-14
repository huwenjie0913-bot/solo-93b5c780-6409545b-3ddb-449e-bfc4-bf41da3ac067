"""毫米/点阵换算与旋转矩形几何。"""

from __future__ import annotations

import math

MM_PER_INCH = 25.4


def mm_to_dots(mm: float, dpi: int) -> float:
    return mm * dpi / MM_PER_INCH


def dots_to_mm(dots: float, dpi: int) -> float:
    return dots * MM_PER_INCH / dpi


def rotate_point(x: float, y: float, cx: float, cy: float, angle_deg: float):
    """绕 (cx, cy) 顺时针旋转（屏幕坐标系，y 向下）。"""
    rad = math.radians(angle_deg)
    cos_a, sin_a = math.cos(rad), math.sin(rad)
    dx, dy = x - cx, y - cy
    return (cx + dx * cos_a - dy * sin_a, cy + dx * sin_a + dy * cos_a)


def rotated_bbox(cx: float, cy: float, w: float, h: float, angle_deg: float):
    """旋转后矩形的轴对齐包围盒 (x0, y0, x1, y1)。"""
    corners = [
        (cx - w / 2, cy - h / 2),
        (cx + w / 2, cy - h / 2),
        (cx + w / 2, cy + h / 2),
        (cx - w / 2, cy + h / 2),
    ]
    pts = [rotate_point(x, y, cx, cy, angle_deg) for x, y in corners]
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return min(xs), min(ys), max(xs), max(ys)


def aabb_intersects(a, b) -> bool:
    return a[0] < b[2] and a[2] > b[0] and a[1] < b[3] and a[3] > b[1]


def aabb_gap(a, b) -> float:
    """两个 AABB 之间的欧氏间隙，相交时为 0。"""
    dx = max(b[0] - a[2], a[0] - b[2], 0.0)
    dy = max(b[1] - a[3], a[1] - b[3], 0.0)
    return math.hypot(dx, dy)
