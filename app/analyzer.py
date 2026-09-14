"""单套版式的点阵化预检分析。"""

from __future__ import annotations

import math

from .barcode_engine import encode_modules, runs_from_bits
from .errors import CannotFitError
from .geometry import (
    aabb_gap,
    aabb_intersects,
    dots_to_mm,
    mm_to_dots,
    rotated_bbox,
)
from .schemas import LabelSpec, LayoutSpec, PrinterSpec

# 每元素取整偏差超过该比例记 warn（模块边界采用累计四舍五入）
ROUNDING_WARN_PCT = 15.0
# 静区/越界裕量低于该倍数记 warn
SAFETY_RATIO = 1.2


def _rasterize_runs(runs: list[dict], ideal_dots: float) -> list[dict]:
    """按累计四舍五入把模块游程映射到打印点，模拟真实点阵化取整。"""
    out = []
    for r in runs:
        s = round(r["start_module"] * ideal_dots)
        e = round((r["start_module"] + r["modules"]) * ideal_dots)
        out.append({**r, "start_dot": s, "dots": max(0, e - s)})
    return out


def _element_ref(run: dict, kind: str) -> dict:
    return {
        "type": kind,
        "run_index": run["run_index"],
        "start_module": run["start_module"],
        "width_modules": run["modules"],
        "width_dots": run["dots"],
    }


def analyze_layout(
    label: LabelSpec, printer: PrinterSpec, layout: LayoutSpec
) -> dict:
    dpi = printer.dpi
    spec = layout.barcode
    box = spec.box

    # ---- 1. 编码 + 模块游程 ------------------------------------------------
    bits, fullcode = encode_modules(spec.symbology, spec.data)
    runs = runs_from_bits(bits)
    for i, r in enumerate(runs):
        r["run_index"] = i
    modules = len(bits)

    # ---- 2. 点阵化：模块 -> 打印点 ----------------------------------------
    box_w_dots = mm_to_dots(box.width_mm, dpi)
    box_h_dots = mm_to_dots(box.height_mm, dpi)
    if modules > box_w_dots:
        raise CannotFitError(
            f"条码共 {modules} 个模块，条码框宽仅 {box_w_dots:.1f} 点："
            "即使每模块 1 点也排不下",
            {
                "modules": modules,
                "box_width_dots": round(box_w_dots, 2),
                "min_box_width_mm": round(dots_to_mm(modules, dpi), 3),
            },
        )
    ideal = box_w_dots / modules  # 每模块理想点数（浮点）
    raster = _rasterize_runs(runs, ideal)
    total_dots = raster[-1]["start_dot"] + raster[-1]["dots"] if raster else 0
    total_mm = dots_to_mm(total_dots, dpi)
    module_mm = dots_to_mm(ideal, dpi)

    bars = [r for r in raster if r["is_bar"] and r["dots"] > 0]
    spaces = [r for r in raster if not r["is_bar"] and r["dots"] > 0]
    narrow_bar = min(bars, key=lambda r: r["dots"])
    narrow_space = min(spaces, key=lambda r: r["dots"]) if spaces else None

    # 每游程取整偏差
    worst_dev = max(
        abs(r["dots"] - r["modules"] * ideal) / (r["modules"] * ideal)
        for r in raster
        if r["modules"] > 0
    )

    checks: list[dict] = []
    risks: list[dict] = []

    def add(check, status, margin, detail, element=None):
        checks.append(
            {
                "check": check,
                "status": status,
                "margin": round(margin, 4) if margin is not None else None,
                "detail": detail,
                "element": element,
            }
        )

    # ---- 3. 模块取整误差 ---------------------------------------------------
    dev_pct = worst_dev * 100
    status = "warn" if dev_pct > ROUNDING_WARN_PCT else "pass"
    add(
        "module_rounding",
        status,
        ROUNDING_WARN_PCT / max(dev_pct, 1e-9),
        f"模块理想宽 {ideal:.3f} 点，累计取整后单元素最大偏差 {dev_pct:.1f}%"
        f"（总宽 {total_dots} 点 / {total_mm:.3f} mm）",
    )
    if status == "warn":
        risks.append(
            {
                "code": "MODULE_ROUNDING",
                "message": f"模块取整偏差 {dev_pct:.1f}% 超过 {ROUNDING_WARN_PCT:.0f}%，"
                "宽窄比失真，可能降低解码余量",
                "element": None,
                "suggestion": "调整条码框宽度，使每模块点数接近整数",
            }
        )

    # ---- 4. 最窄条点数 -----------------------------------------------------
    nb = narrow_bar["dots"]
    req = layout.min_narrow_dots
    if nb < req:
        st = "fail"
    elif nb < req + 1:
        st = "warn"
    else:
        st = "pass"
    add(
        "narrowest_bar",
        st,
        nb / req,
        f"最窄条 {nb} 点（要求 ≥{req} 点，{dots_to_mm(nb, dpi):.3f} mm），"
        f"位于第 {narrow_bar['run_index']} 个元素"
        f"（模块偏移 {narrow_bar['start_module']}，{narrow_bar['modules']} 模块）",
        _element_ref(narrow_bar, "bar"),
    )
    if st != "pass":
        risks.append(
            {
                "code": "NARROW_BAR",
                "message": f"最窄条仅 {nb} 点，{dpi} dpi 下易因油墨扩散/对位误差断裂",
                "element": _element_ref(narrow_bar, "bar"),
                "suggestion": f"放大条码使最窄条 ≥{req + 1} 点，"
                f"即缩放 ≥{(req + 1) / nb:.2f} 倍",
            }
        )

    # ---- 5. 旋转后的越界检查 ----------------------------------------------
    cx = box.x_mm + box.width_mm / 2
    cy = box.y_mm + box.height_mm / 2
    bb = rotated_bbox(cx, cy, box.width_mm, box.height_mm, box.rotation_deg)
    dists = {
        "left": bb[0],
        "top": bb[1],
        "right": label.width_mm - bb[2],
        "bottom": label.height_mm - bb[3],
    }
    min_side = min(dists, key=dists.get)
    min_dist = dists[min_side]
    qz_req = (
        layout.quiet_zone_mm
        if layout.quiet_zone_mm is not None
        else 10 * module_mm
    )
    oob_margin = 1 + min_dist / max(qz_req, 1e-6)
    if min_dist < 0:
        st = "fail"
    elif min_dist < 1.0:
        st = "warn"
    else:
        st = "pass"
    add(
        "out_of_bounds",
        st,
        oob_margin,
        f"旋转 {box.rotation_deg}° 后包围盒 "
        f"({bb[0]:.2f},{bb[1]:.2f})-({bb[2]:.2f},{bb[3]:.2f}) mm，"
        f"距标签{min_side}边 {min_dist:.2f} mm",
        {"type": "label_edge", "side": min_side, "distance_mm": round(min_dist, 3)},
    )
    if st == "fail":
        risks.append(
            {
                "code": "OUT_OF_BOUNDS",
                "message": f"条码旋转后超出标签{min_side}边 {-min_dist:.2f} mm，"
                "打印时被裁掉",
                "element": {
                    "type": "label_edge",
                    "side": min_side,
                    "overshoot_mm": round(-min_dist, 3),
                },
                "suggestion": "缩小条码、减小旋转角或移动条码框中心",
            }
        )

    # ---- 6. 静区 -----------------------------------------------------------
    # 在条码本地坐标系左右各扩 qz_req，再旋转取包围盒
    ex_bb = rotated_bbox(
        cx,
        cy,
        box.width_mm + 2 * qz_req,
        box.height_mm,
        box.rotation_deg,
    )
    qz_dists = {
        "left": ex_bb[0],
        "top": ex_bb[1],
        "right": label.width_mm - ex_bb[2],
        "bottom": label.height_mm - ex_bb[3],
    }
    qz_side = min(qz_dists, key=qz_dists.get)
    qz_edge = qz_dists[qz_side]

    # 障碍物（文字块）侵入静区检测
    obstacles = []
    for t in layout.texts:
        tcx, tcy = t.x_mm + t.width_mm / 2, t.y_mm + t.height_mm / 2
        tb = rotated_bbox(tcx, tcy, t.width_mm, t.height_mm, t.rotation_deg)
        obstacles.append((t.id or t.content[:12], tb, "text"))
    for z in layout.forbidden_zones:
        obstacles.append((z.id or "zone", (z.x_mm, z.y_mm,
                          z.x_mm + z.width_mm, z.y_mm + z.height_mm), "forbidden"))

    intrusions = [name for name, ob, _ in obstacles if aabb_intersects(ex_bb, ob)]
    if qz_edge < 0 or intrusions:
        st = "fail"
    elif qz_edge < qz_req * (SAFETY_RATIO - 1):
        st = "warn"
    else:
        st = "pass"
    qz_margin = 1 + qz_edge / max(qz_req, 1e-6)
    if intrusions:
        qz_margin = min(qz_margin, 0.5)
    add(
        "quiet_zone",
        st,
        qz_margin,
        f"要求静区 {qz_req:.2f} mm（左右各），扩展包围盒距标签{qz_side}边 "
        f"{qz_edge:.2f} mm" + (f"；侵入元素：{', '.join(intrusions)}" if intrusions else ""),
        {"type": "quiet_zone", "side": qz_side, "required_mm": round(qz_req, 3),
         "available_mm": round(qz_req + qz_edge, 3)},
    )
    if st != "pass":
        risks.append(
            {
                "code": "QUIET_ZONE",
                "message": (
                    f"静区不足：{qz_side} 侧可用 {qz_req + qz_edge:.2f} mm "
                    f"< 要求 {qz_req:.2f} mm" if qz_edge < 0 else
                    f"静区被 {', '.join(intrusions)} 侵入"
                ),
                "element": {"type": "quiet_zone", "side": qz_side},
                "suggestion": "缩小条码或移开相邻元素，保证左右静区",
            }
        )

    # ---- 7. 裁切禁区碰撞 ---------------------------------------------------
    hits = []
    qz_zone_hits = []
    for z in layout.forbidden_zones:
        zb = (z.x_mm, z.y_mm, z.x_mm + z.width_mm, z.y_mm + z.height_mm)
        if aabb_intersects(bb, zb):
            hits.append(z.id or "zone")
        elif aabb_intersects(ex_bb, zb):
            qz_zone_hits.append(z.id or "zone")
    if hits:
        st, margin = "fail", 0.0
    else:
        gap = min(
            (aabb_gap(bb, (z.x_mm, z.y_mm, z.x_mm + z.width_mm,
                           z.y_mm + z.height_mm)) for z in layout.forbidden_zones),
            default=None,
        )
        margin = 999.0 if gap is None else 1 + gap / max(qz_req, 1e-6)
        st = "warn" if qz_zone_hits else "pass"
    add(
        "forbidden_zone",
        st,
        margin,
        (
            f"条码与裁切禁区 {', '.join(hits)} 相交"
            if hits
            else (
                f"静区延伸进入禁区 {', '.join(qz_zone_hits)}"
                if qz_zone_hits
                else "与裁切禁区无碰撞"
            )
        ),
        {"type": "forbidden_zone", "zone_ids": hits or qz_zone_hits or None},
    )
    if hits:
        risks.append(
            {
                "code": "FORBIDDEN_ZONE",
                "message": f"条码实体落入裁切禁区 {', '.join(hits)}，会被裁掉",
                "element": {"type": "forbidden_zone", "zone_ids": hits},
                "suggestion": "移动条码框或调整禁区范围",
            }
        )

    # ---- 8. 文字碰撞 -------------------------------------------------------
    text_hits = [
        name for name, ob, kind in obstacles
        if kind == "text" and aabb_intersects(bb, ob)
    ]
    add(
        "text_collision",
        "fail" if text_hits else "pass",
        0.0 if text_hits else 999.0,
        f"条码与文字块 {', '.join(text_hits)} 重叠" if text_hits else "与文字无重叠",
        {"type": "text", "ids": text_hits or None},
    )
    if text_hits:
        risks.append(
            {
                "code": "TEXT_COLLISION",
                "message": f"文字块 {', '.join(text_hits)} 压在条码上",
                "element": {"type": "text", "ids": text_hits},
                "suggestion": "移动文字块或条码框",
            }
        )

    # ---- 9. 增粗/收窄 1 点模拟（油墨增益 BWA） ------------------------------
    def simulate(delta: int) -> dict:
        """条 ±delta 点、空 ∓delta 点，总宽不变，模拟印刷增益。"""
        sim = []
        for r in raster:
            d = r["dots"] + (delta if r["is_bar"] else -delta)
            sim.append({**r, "dots": d})
        bars_s = [r for r in sim if r["is_bar"]]
        spaces_s = [r for r in sim if not r["is_bar"] and r["start_module"] not in (0,)
                    and r["start_module"] + r["modules"] != modules]
        worst_bar = min(bars_s, key=lambda r: r["dots"])
        inner_spaces = [r for r in spaces_s]
        worst_space = min(inner_spaces, key=lambda r: r["dots"]) if inner_spaces else None
        broken = [r for r in sim if r["dots"] <= 0]
        ok = not broken
        return {
            "delta_dots": delta,
            "ok": ok,
            "narrowest_bar_dots": worst_bar["dots"],
            "narrowest_space_dots": worst_space["dots"] if worst_space else None,
            "broken_elements": [
                _element_ref(r, "bar" if r["is_bar"] else "space") for r in broken
            ],
            "limiting_element": _element_ref(
                worst_bar if delta < 0 else (worst_space or worst_bar),
                "bar" if delta < 0 else "space",
            ),
            "verdict": (
                "可扫描余量保持"
                if ok
                else f"{'条' if delta < 0 else '空'}消失 {len(broken)} 处，必然无法扫描"
            ),
        }

    simulations = {
        "thicken_1dot": simulate(+1),
        "thin_1dot": simulate(-1),
    }
    for name, sim in simulations.items():
        if not sim["ok"]:
            risks.append(
                {
                    "code": "GAIN_FRAGILE",
                    "message": f"条纹{'增粗' if name == 'thicken_1dot' else '收窄'} 1 点后"
                    f"有元素消失（{sim['verdict']}）",
                    "element": sim["broken_elements"][0] if sim["broken_elements"] else None,
                    "suggestion": "放大条码，使最窄条/空 ≥2 点以吸收 ±1 点印刷波动",
                }
            )

    # ---- 10. 建议缩放范围 ---------------------------------------------------
    # s 作用于当前点阵化结果（相对缩放）
    s_min = req / nb
    s_min = max(s_min, 1.0 / ideal)  # 每模块至少 1 点
    # 宽度约束：总宽不超过框
    s_max_w = box_w_dots / total_dots if total_dots else 0
    # 静区约束：左右剩余空间
    spare_qz = max(0.0, qz_edge)  # qz_edge>=0 时才有富余
    s_max_qz = (
        (total_mm + 2 * spare_qz) / total_mm if total_mm > 0 and qz_edge > 0 else s_max_w
    )
    # 标签边界约束：旋转包围盒须留在标签内
    rad = math.radians(box.rotation_deg)
    cos_a, sin_a = abs(math.cos(rad)), abs(math.sin(rad))
    s_max_bounds = math.inf
    if cos_a > 1e-9:
        # extent_x = (w*s*cos + h*sin)/2 <= min(cx, W-cx)
        lim = min(cx, label.width_mm - cx)
        s_max_bounds = min(
            s_max_bounds,
            (2 * lim - box.height_mm * sin_a) / (box.width_mm * cos_a),
        )
    if sin_a > 1e-9:
        lim = min(cy, label.height_mm - cy)
        s_max_bounds = min(
            s_max_bounds,
            (2 * lim - box.height_mm * cos_a) / (box.width_mm * sin_a),
        )
    s_max = min(s_max_w, s_max_qz, s_max_bounds)
    feasible = s_min <= s_max
    scale_range = {
        "min": round(s_min, 4),
        "max": round(s_max, 4) if math.isfinite(s_max) else None,
        "feasible": feasible,
        "basis": "相对当前点阵化结果的缩放系数；约束：最窄条点数、条码框宽、静区、标签边界",
    }
    if not feasible:
        risks.append(
            {
                "code": "NO_FEASIBLE_SCALE",
                "message": f"不存在可行缩放：需要 ≥{s_min:.2f} 倍才能满足最窄条，"
                f"但 ≥{s_max:.2f} 倍即越界/超框",
                "element": None,
                "suggestion": "加宽条码框或缩短数据内容",
            }
        )

    # ---- 11. 汇总 -----------------------------------------------------------
    margins = [c["margin"] for c in checks if c["margin"] is not None]
    worst = min(margins) if margins else None
    has_fail = any(c["status"] == "fail" for c in checks)
    has_warn = any(c["status"] == "warn" for c in checks)
    status = "fail" if has_fail else ("warning" if has_warn else "ok")

    return {
        "layout_id": layout.layout_id,
        "status": status,
        "error": None,
        "barcode": {
            "symbology": spec.symbology,
            "data": spec.data,
            "fullcode": fullcode,
            "modules": modules,
            "module_width_dots_ideal": round(ideal, 4),
            "module_width_mm": round(module_mm, 4),
            "total_width_dots": total_dots,
            "total_width_mm": round(total_mm, 3),
            "height_dots": round(box_h_dots, 1),
            "rounding_max_deviation_pct": round(dev_pct, 2),
            "narrowest_bar_dots": nb,
            "narrowest_space_dots": narrow_space["dots"] if narrow_space else None,
            "narrowest_bar_element": _element_ref(narrow_bar, "bar"),
            "rotation_deg": box.rotation_deg,
            "bbox_mm": [round(v, 3) for v in bb],
        },
        "checks": checks,
        "simulations": simulations,
        "risks": risks,
        "suggested_scale_range": scale_range,
        "worst_margin": round(worst, 4) if worst is not None else None,
        # 供预览渲染使用的内部数据（响应中剔除）
        "_render": {
            "raster": raster,
            "expanded_bbox": ex_bb,
            "bbox": bb,
            "qz_req_mm": qz_req,
        },
    }
