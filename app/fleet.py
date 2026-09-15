"""机队多机型公差兼容性分析。

同一套毫米版式下发到不同仓库、不同 DPI 的热敏打印机时，单机预检通过并不
代表版式能承受各机型的走纸/套准偏移与墨点增益。本模块对每个机型：

1. 枚举横/纵向套准偏差端点（mm）× 条纹增益端点（整数点），最多 2³=8 种工况；
2. 每种工况按该机型 DPI 重新点阵化，评估最窄条/空、静区、越界、禁区碰撞；
3. 汇总该机型的最差状态、触发工况、失效条/空元素与各项物理裕量；
4. 给出该机型上的可行缩放区间，并求多机型共同可行缩放区间，
   区间为空时指出互相冲突的上下界及对应机型。

单套版式在某机型上的数据性错误（UNENCODABLE_DATA / CANNOT_FIT）以行内
error 返回，不中断其余组合。
"""

from __future__ import annotations

import itertools
import math

from .barcode_engine import encode_modules, runs_from_bits
from .errors import CannotFitError, PreflightError
from .geometry import (
    aabb_gap,
    dots_to_mm,
    mm_to_dots,
    polygons_intersect,
    rotated_bbox,
    rotated_corners,
)
from .schemas import (
    CommonScaleRange,
    LabelSpec,
    LayoutSpec,
    PrinterProfile,
)

# 检查项状态优先级：fail < warn < pass
_STATUS_ORDER = {"fail": 0, "warn": 1, "pass": 2}
_OVERALL = {0: "fail", 1: "warning", 2: "ok"}

# 几何类检查裕量低于该富余倍数记 warn（与 analyzer.SAFETY_RATIO 一致）
_GEO_WARN_RATIO = 1.2


# ---------------------------------------------------------------- 基础工具


def _element_ref(run: dict, kind: str, reason: str | None = None) -> dict:
    ref = {
        "type": kind,
        "run_index": run["run_index"],
        "start_module": run["start_module"],
        "width_modules": run["modules"],
        "width_dots_nominal": run["dots_nominal"],
        "width_dots_under_gain": run.get("dots_gained", run["dots_nominal"]),
    }
    if reason is not None:
        ref["failure_reason"] = reason
    return ref


def _rasterize(runs: list[dict], ideal_dots: float) -> list[dict]:
    """累计四舍五入把模块游程映射到打印点（与 analyzer 同一光栅化模型）。"""
    out = []
    for r in runs:
        s = round(r["start_module"] * ideal_dots)
        e = round((r["start_module"] + r["modules"]) * ideal_dots)
        out.append({**r, "start_dot": s, "dots_nominal": max(0, e - s)})
    return out


def _build_obstacles(layout: LayoutSpec) -> list[tuple[str, list, str]]:
    """文字块 / 裁切禁区障碍物（旋转后真实角点）。"""
    obstacles = []
    for t in layout.texts:
        tcx, tcy = t.x_mm + t.width_mm / 2, t.y_mm + t.height_mm / 2
        corners = rotated_corners(tcx, tcy, t.width_mm, t.height_mm, t.rotation_deg)
        obstacles.append((t.id or t.content[:12], corners, "text"))
    for z in layout.forbidden_zones:
        zc = [
            (z.x_mm, z.y_mm),
            (z.x_mm + z.width_mm, z.y_mm),
            (z.x_mm + z.width_mm, z.y_mm + z.height_mm),
            (z.x_mm, z.y_mm + z.height_mm),
        ]
        obstacles.append((z.id or "zone", zc, "forbidden"))
    return obstacles


def _shift(corners, dx, dy):
    return [(x + dx, y + dy) for x, y in corners]


def _worst(records: list[dict]) -> dict:
    """同类检查在各工况下取最差一条（状态优先，其次归一化裕量最小）。"""
    return min(
        records,
        key=lambda r: (
            _STATUS_ORDER[r["status"]],
            r["margin"] if r["margin"] is not None else 1e18,
        ),
    )


def _rb(v: float):
    return round(v, 4) if math.isfinite(v) else None


def _is_inner_space(run: dict, modules: int) -> bool:
    return (
        not run["is_bar"]
        and run["start_module"] != 0
        and run["start_module"] + run["modules"] != modules
    )


# ---------------------------------------------------------------- 单机型评估


def _error_printer(profile: PrinterProfile, exc: PreflightError) -> dict:
    return {
        "printer_id": profile.printer_id,
        "dpi": profile.dpi,
        "status": "error",
        "error": exc.to_dict(),
        "worst_status": None,
        "worst_margin": None,
        "trigger_condition": None,
        "failed_elements": [],
        "checks": [],
        "scale_range": None,
        "combinations_evaluated": 0,
    }


def evaluate_printer(
    label: LabelSpec,
    profile: PrinterProfile,
    layout: LayoutSpec,
    bits: str,
    fullcode: str,
    obstacles: list,
) -> dict:
    """枚举公差端点工况并评估单机型；返回机型兼容性结果 dict。"""
    dpi = profile.dpi
    spec = layout.barcode
    box = spec.box
    modules = len(bits)

    # ---- 1. 按该机型 DPI 重新点阵化（名义状态：无偏移、无增益） ------------
    box_w_dots = mm_to_dots(box.width_mm, dpi)
    box_h_dots = mm_to_dots(box.height_mm, dpi)
    if modules > box_w_dots:
        err = CannotFitError(
            f"机型 {profile.printer_id}（{dpi} DPI）：条码共 {modules} 个模块，"
            f"条码框宽仅 {box_w_dots:.1f} 点，即使每模块 1 点也排不下",
            {
                "printer_id": profile.printer_id,
                "dpi": dpi,
                "modules": modules,
                "box_width_dots": round(box_w_dots, 2),
                "min_box_width_mm": round(dots_to_mm(modules, dpi), 3),
            },
        )
        return _error_printer(profile, err)

    ideal = box_w_dots / modules
    runs = runs_from_bits(bits)
    for i, r in enumerate(runs):
        r["run_index"] = i
    raster = _rasterize(runs, ideal)
    total_dots = raster[-1]["start_dot"] + raster[-1]["dots_nominal"]
    total_mm = dots_to_mm(total_dots, dpi)
    module_mm = dots_to_mm(ideal, dpi)

    qz_req = (
        layout.quiet_zone_mm
        if layout.quiet_zone_mm is not None
        else 10 * module_mm
    )
    req = layout.min_narrow_dots

    # ---- 2. 枚举 2（offset_x 端点）× 2（offset_y 端点）× 2（增益端点） ------
    ox_lo, ox_hi = profile.offset_x_mm.endpoints()
    oy_lo, oy_hi = profile.offset_y_mm.endpoints()
    g_lo, g_hi = profile.gain_dots.endpoints()
    conditions = list(
        itertools.product((ox_lo, ox_hi), (oy_lo, oy_hi), (g_lo, g_hi))
    )

    rec_bar: list[dict] = []
    rec_space: list[dict] = []
    rec_oob: list[dict] = []
    rec_qz: list[dict] = []
    rec_fz: list[dict] = []
    rec_text: list[dict] = []
    failed_elements: dict[tuple, dict] = {}

    cx = box.x_mm + box.width_mm / 2
    cy = box.y_mm + box.height_mm / 2

    for ox, oy, gain in conditions:
        c = {"offset_x_mm": ox, "offset_y_mm": oy, "gain_dots": gain}
        cdesc = f"x {ox:+.2f} mm, y {oy:+.2f} mm, 增益 {gain:+d} 点"

        # -- 2.1 增益作用于点阵：条 +g、空 -g；重算最窄条/空与失效元素 ------
        gained = [
            {**r, "dots_gained": r["dots_nominal"] + (gain if r["is_bar"] else -gain)}
            for r in raster
        ]
        bars = [r for r in gained if r["is_bar"]]
        inner_spaces = [r for r in gained if _is_inner_space(r, modules)]
        nb = min(bars, key=lambda r: r["dots_gained"])
        ns = min(inner_spaces, key=lambda r: r["dots_gained"]) if inner_spaces else None

        # 收集该工况下全部失效条/空（不止最窄的一个）；同元素在更严苛
        # 工况（增益后点数更小）再次出现时覆盖为更差快照
        for r in gained:
            key = (r["run_index"], "bar" if r["is_bar"] else "space")
            if r["is_bar"]:
                if r["dots_gained"] < req:
                    reason = (
                        "narrow_bar_zero_or_lost"
                        if r["dots_gained"] <= 0
                        else "narrow_bar_below_min_dots"
                    )
                    ref = _element_ref(r, "bar", reason)
                    old = failed_elements.get(key)
                    if old is None or ref["width_dots_under_gain"] < old[
                        "width_dots_under_gain"
                    ]:
                        failed_elements[key] = ref
            elif _is_inner_space(r, modules) and r["dots_gained"] <= 0:
                ref = _element_ref(r, "space", "space_closed_by_gain")
                old = failed_elements.get(key)
                if old is None or ref["width_dots_under_gain"] < old[
                    "width_dots_under_gain"
                ]:
                    failed_elements[key] = ref

        nb_dots = nb["dots_gained"]
        rec_bar.append(
            {
                "status": (
                    "fail"
                    if nb_dots < req
                    else "warn"
                    if nb_dots < req + 1
                    else "pass"
                ),
                "margin": nb_dots / req,
                "condition": c,
                "detail": (
                    f"工况（{cdesc}）下最窄条 {nb_dots} 点（要求 ≥{req}），"
                    f"位于第 {nb['run_index']} 个元素"
                    f"（模块偏移 {nb['start_module']}，{nb['modules']} 模块）"
                ),
                "element": _element_ref(nb, "bar"),
            }
        )
        if ns is not None:
            ns_dots = ns["dots_gained"]
            rec_space.append(
                {
                    "status": (
                        "fail" if ns_dots <= 0 else "warn" if ns_dots < 2 else "pass"
                    ),
                    "margin": ns_dots / 1.0,
                    "condition": c,
                    "detail": (
                        f"工况（{cdesc}）下最窄空 {ns_dots} 点，"
                        f"位于第 {ns['run_index']} 个元素"
                        f"（模块偏移 {ns['start_module']}，{ns['modules']} 模块）"
                    ),
                    "element": _element_ref(ns, "space"),
                }
            )

        # -- 2.2 几何：正增益向条纹两侧各吃 grow_mm，构成实体外延 ------------
        grow_mm = dots_to_mm(gain, dpi) if gain > 0 else 0.0
        eff_w = box.width_mm + 2 * grow_mm
        body_corners = _shift(
            rotated_corners(cx, cy, eff_w, box.height_mm, box.rotation_deg), ox, oy
        )
        ex_corners = _shift(
            rotated_corners(
                cx, cy, eff_w + 2 * qz_req, box.height_mm, box.rotation_deg
            ),
            ox,
            oy,
        )
        body_bb = rotated_bbox(cx, cy, eff_w, box.height_mm, box.rotation_deg)
        ex_bb = rotated_bbox(
            cx, cy, eff_w + 2 * qz_req, box.height_mm, box.rotation_deg
        )
        body_bb_t = (
            body_bb[0] + ox, body_bb[1] + oy, body_bb[2] + ox, body_bb[3] + oy
        )
        ex_bb_t = (ex_bb[0] + ox, ex_bb[1] + oy, ex_bb[2] + ox, ex_bb[3] + oy)

        # 越界：实体包围盒距标签四边
        d = {
            "left": body_bb_t[0],
            "top": body_bb_t[1],
            "right": label.width_mm - body_bb_t[2],
            "bottom": label.height_mm - body_bb_t[3],
        }
        side = min(d, key=d.get)
        dist = d[side]
        rec_oob.append(
            {
                "status": "fail" if dist < 0 else "warn" if dist < 1.0 else "pass",
                "margin": 1 + dist / max(qz_req, 1e-6),
                "margin_mm": round(dist, 3),
                "condition": c,
                "detail": (
                    f"工况（{cdesc}）下实体包围盒距标签 {side} 边 {dist:.2f} mm"
                    + ("，已越界" if dist < 0 else "")
                ),
                "element": {
                    "type": "label_edge",
                    "side": side,
                    "distance_mm": round(dist, 3),
                },
            }
        )

        # 静区：扩展包围盒距标签边 + 障碍物侵入
        qd = {
            "left": ex_bb_t[0],
            "top": ex_bb_t[1],
            "right": label.width_mm - ex_bb_t[2],
            "bottom": label.height_mm - ex_bb_t[3],
        }
        qz_side = min(qd, key=qd.get)
        qz_edge = qd[qz_side]
        intrusions = [
            name for name, oc, _ in obstacles if polygons_intersect(ex_corners, oc)
        ]
        if qz_edge < 0 or intrusions:
            st = "fail"
        elif qz_edge < qz_req * (_GEO_WARN_RATIO - 1):
            st = "warn"
        else:
            st = "pass"
        margin_qz = 1 + qz_edge / max(qz_req, 1e-6)
        if intrusions:
            margin_qz = min(margin_qz, 0.5)
        rec_qz.append(
            {
                "status": st,
                "margin": margin_qz,
                "margin_mm": round(qz_edge, 3),
                "condition": c,
                "detail": (
                    f"工况（{cdesc}）下静区扩展包围盒距标签 {qz_side} 边 "
                    f"{qz_edge:.2f} mm（要求静区 {qz_req:.2f} mm）"
                    + (f"；侵入元素：{', '.join(intrusions)}" if intrusions else "")
                ),
                "element": {
                    "type": "quiet_zone",
                    "side": qz_side,
                    "required_mm": round(qz_req, 3),
                    "available_mm": round(qz_req + qz_edge, 3),
                    "intruding_elements": intrusions or None,
                },
            }
        )

        # 禁区：实体精确相交 fail；仅静区延伸进入禁区 warn
        hits = [
            name
            for name, oc, kind in obstacles
            if kind == "forbidden" and polygons_intersect(body_corners, oc)
        ]
        qz_hits = [
            name
            for name, oc, kind in obstacles
            if kind == "forbidden"
            and not polygons_intersect(body_corners, oc)
            and polygons_intersect(ex_corners, oc)
        ]
        if hits:
            st, margin_fz, gap = "fail", 0.0, 0.0
        else:
            gap = min(
                (
                    aabb_gap(
                        body_bb_t,
                        (
                            z.x_mm,
                            z.y_mm,
                            z.x_mm + z.width_mm,
                            z.y_mm + z.height_mm,
                        ),
                    )
                    for z in layout.forbidden_zones
                ),
                default=None,
            )
            margin_fz = 999.0 if gap is None else 1 + gap / max(qz_req, 1e-6)
            st = "warn" if qz_hits else "pass"
        rec_fz.append(
            {
                "status": st,
                "margin": margin_fz,
                "margin_mm": round(gap, 3) if gap is not None else None,
                "condition": c,
                "detail": (
                    f"工况（{cdesc}）下"
                    + (
                        f"条码实体与裁切禁区 {', '.join(hits)} 相交"
                        if hits
                        else (
                            f"静区延伸进入禁区 {', '.join(qz_hits)}"
                            if qz_hits
                            else "与裁切禁区无碰撞"
                        )
                    )
                ),
                "element": {
                    "type": "forbidden_zone",
                    "zone_ids": hits or qz_hits or None,
                },
            }
        )

        # 文字碰撞：偏移后实体精确相交即 fail（与 analyzer 一致）
        text_hits = [
            name
            for name, oc, kind in obstacles
            if kind == "text" and polygons_intersect(body_corners, oc)
        ]
        rec_text.append(
            {
                "status": "fail" if text_hits else "pass",
                "margin": 0.0 if text_hits else 999.0,
                "margin_mm": None,
                "condition": c,
                "detail": (
                    f"工况（{cdesc}）下文字块 {', '.join(text_hits)} 压在条码上"
                    if text_hits
                    else f"工况（{cdesc}）下与文字无重叠"
                ),
                "element": {"type": "text", "ids": text_hits or None},
            }
        )

    # ---- 3. 各类检查取最差工况 ---------------------------------------------
    checks = []
    for name, recs in (
        ("narrowest_bar", rec_bar),
        ("narrowest_space", rec_space),
        ("quiet_zone", rec_qz),
        ("out_of_bounds", rec_oob),
        ("forbidden_zone", rec_fz),
        ("text_collision", rec_text),
    ):
        if not recs:
            continue
        w = _worst(recs)
        checks.append(
            {
                "check": name,
                "worst_status": w["status"],
                "trigger_condition": w["condition"],
                "margin": round(w["margin"], 4),
                "margin_mm": w.get("margin_mm"),
                "detail": w["detail"],
                "element": w["element"],
            }
        )

    worst_check = min(
        checks,
        key=lambda c: (
            _STATUS_ORDER[c["worst_status"]],
            c["margin"] if c["margin"] is not None else 1e18,
        ),
    )
    worst_level = min(
        _STATUS_ORDER[c["worst_status"]] for c in checks
    ) if checks else 2
    printer_status = _OVERALL[worst_level]

    # ---- 4. 该机型上的可行缩放区间 ------------------------------------------
    scale_range = _printer_scale_range(
        profile=profile,
        layout=layout,
        label=label,
        raster=raster,
        modules=modules,
        ideal=ideal,
        box_w_dots=box_w_dots,
        total_dots=total_dots,
        qz_req=qz_req,
        g_lo=g_lo,
        g_hi=g_hi,
        ox_lo=ox_lo,
        ox_hi=ox_hi,
        oy_lo=oy_lo,
        oy_hi=oy_hi,
        cx=cx,
        cy=cy,
        req=req,
    )

    return {
        "printer_id": profile.printer_id,
        "dpi": dpi,
        "status": printer_status,
        "error": None,
        "worst_status": printer_status,
        "worst_margin": worst_check["margin"],
        "trigger_condition": worst_check["trigger_condition"],
        "failed_elements": list(failed_elements.values()),
        "checks": checks,
        "scale_range": scale_range,
        "combinations_evaluated": len(conditions),
        "barcode": {
            "symbology": spec.symbology,
            "data": spec.data,
            "fullcode": fullcode,
            "modules": modules,
            "module_width_dots_ideal": round(ideal, 4),
            "total_width_dots": total_dots,
            "total_width_mm": round(total_mm, 3),
            "quiet_zone_required_mm": round(qz_req, 3),
            "height_dots": round(box_h_dots, 1),
        },
    }


def _printer_scale_range(
    *,
    profile: PrinterProfile,
    layout: LayoutSpec,
    label: LabelSpec,
    raster: list[dict],
    modules: int,
    ideal: float,
    box_w_dots: float,
    total_dots: int,
    qz_req: float,
    g_lo: int,
    g_hi: int,
    ox_lo: float,
    ox_hi: float,
    oy_lo: float,
    oy_hi: float,
    cx: float,
    cy: float,
    req: int,
) -> dict:
    """s 为相对当前名义点阵化的宽度缩放系数（框高固定，沿用 analyzer 模型）。

    下界：最窄条在最大收窄后仍 ≥ req、每模块在最大增粗后仍 ≥1 点、
          最窄内部空在最大增粗后仍 ≥1 点；
    上界：框宽（含正增益外延）、旋转包围盒越界、静区留界，
          各方向取最不利偏移端点（左/上用 min 端点，右/下用 max 端点）。
    """
    pid, dpi = profile.printer_id, profile.dpi
    box = layout.barcode.box
    w2, h2 = box.width_mm / 2, box.height_mm / 2
    rad = math.radians(box.rotation_deg)
    cos_a, sin_a = abs(math.cos(rad)), abs(math.sin(rad))
    grow_hi = dots_to_mm(max(0, g_hi), dpi)

    def bnd(kind, value, constraint):
        return {
            "kind": kind,
            "value": value,
            "printer_id": pid,
            "dpi": dpi,
            "constraint": constraint,
        }

    lowers: list[dict] = []
    nb_nom = max(1, min(r["dots_nominal"] for r in raster if r["is_bar"]))
    # 条随 g 增减：最不利为最小增益端点 g_lo（最细）
    lowers.append(bnd("lower", (req - g_lo) / nb_nom, "narrowest_bar_dots"))
    # 每模块至少 1 点（名义点阵；收窄风险已由 narrowest_bar 的 g_lo 端点覆盖）
    lowers.append(bnd("lower", 1.0 / ideal, "module_min_1dot"))
    inner = [r for r in raster if _is_inner_space(r, modules)]
    if inner:
        ns_nom = max(1, min(r["dots_nominal"] for r in inner))
        lowers.append(
            bnd(
                "lower",
                (1 + max(0, g_hi)) / ns_nom,
                "narrowest_space_survives_gain",
            )
        )

    uppers: list[dict] = []
    # 框宽：正增益让条纹两端各外延 g 点（内部条空抵消，总宽 +2g）
    uppers.append(
        bnd(
            "upper",
            (box_w_dots - 2 * max(0, g_hi)) / max(total_dots, 1),
            "box_width",
        )
    )

    # 旋转包围盒半幅（宽度随 s，高度固定，增益/静区外延为固定 mm）：
    # hx(s) = (w*s/2 + g)*cos + h/2*sin；hy(s) 把 cos/sin 对调
    # 偏移后中心 (cx+ox, cy+oy)；左/上边最不利为偏移 min 端点，右/下为 max
    x_lim = (cx + ox_lo, label.width_mm - cx - ox_hi)
    y_lim = (cy + oy_lo, label.height_mm - cy - oy_hi)
    axis_specs = [
        (w2 * cos_a, cos_a, sin_a, h2, x_lim, ("left", "right")),
        (w2 * sin_a, sin_a, cos_a, h2, y_lim, ("top", "bottom")),
    ]
    for denom, along, across, hhalf, (lim_lo, lim_hi), (s_lo, s_hi) in axis_specs:
        if along <= 1e-9:
            continue
        for lim, side in ((lim_lo, s_lo), (lim_hi, s_hi)):
            # 实体留界：(w*s/2 + g)*along + hhalf*across <= lim
            num = lim - grow_hi * along - hhalf * across
            uppers.append(
                bnd("upper", max(0.0, num) / denom, f"bounds_{side}")
            )
            # 静区留界：宽向再退 qz，高向外扩 qz
            num_q = lim - (grow_hi + qz_req) * along - (hhalf + qz_req) * across
            uppers.append(
                bnd("upper", max(0.0, num_q) / denom, f"quiet_zone_{side}")
            )

    # 禁区碰撞：缩放后的实体（含正增益外延、最不利偏移端点）必须离开每个
    # 裁切禁区。沿 4 条 SAT 轴（实体边法向 2 条 + 标签/禁区 AABB 边 2 条）
    # 的分离条件关于 s 为线性，逐“禁区 × 偏移组合”求可分离的临界 s，
    # 再取最保守上界；任何 s>0 都分离不了（如禁区覆盖整标签）时上界为 0，
    # 区间即不可行。每条轴携带 (轴向量, 宽度方向系数, 高度方向系数)。
    if layout.forbidden_zones:
        rad = math.radians(box.rotation_deg)
        ca, sa = abs(math.cos(rad)), abs(math.sin(rad))
        # 每条轴：(法向量, 条码宽度方向投影系数, 条码高度方向投影系数)
        # 实体自身边法向：局部 x/y 轴；其余两条为禁区 AABB 边法向（世界轴）
        axes = [
            ((ca, sa), 1.0, 0.0),     # 沿条码宽度方向的边法向
            ((sa, ca), 0.0, 1.0),     # 沿条码高度方向的边法向（宽度缩放无效）
            ((1.0, 0.0), ca, sa),     # 禁区水平边法向
            ((0.0, 1.0), sa, ca),     # 禁区垂直边法向
        ]
        for z in layout.forbidden_zones:
            zcx = z.x_mm + z.width_mm / 2
            zcy = z.y_mm + z.height_mm / 2
            combo_bounds = []
            for ox in (ox_lo, ox_hi):
                for oy in (oy_lo, oy_hi):
                    dx = cx + ox - zcx
                    dy = cy + oy - zcy
                    best = -math.inf
                    for (ax, ay), kw, kh in axes:
                        denom = w2 * kw
                        if denom <= 1e-12:
                            continue
                        # |d·n| > (w*s/2 + grow)*kw + h2*kh + 禁区半幅
                        zone_half = (
                            z.width_mm / 2 * ax + z.height_mm / 2 * ay
                        )
                        proj = abs(dx * ax + dy * ay)
                        bound = (
                            proj - zone_half - grow_hi * kw - h2 * kh
                        ) / denom
                        best = max(best, bound)
                    combo_bounds.append(best)
            zone_upper = min(combo_bounds)
            uppers.append(
                bnd(
                    "upper",
                    max(0.0, zone_upper),
                    f"forbidden_zone_{z.id or 'zone'}",
                )
            )

    s_min = max(b["value"] for b in lowers)
    s_max = min(b["value"] for b in uppers)
    return {
        "min": _rb(s_min),
        "max": _rb(s_max),
        "feasible": s_min <= s_max,
        "bounds": {
            "lower": [{**b, "value": _rb(b["value"])} for b in lowers],
            "upper": [{**b, "value": _rb(b["value"])} for b in uppers],
        },
        "basis": "相对当前名义点阵化结果的宽度缩放系数；已计入最差套准偏移端点、"
        "增益端点、最窄条/空、框宽、静区与标签边界",
    }


# ---------------------------------------------------------------- 共同区间


def _common_scale_range(printer_results: list[dict]) -> dict:
    """交集各机型缩放区间；为空时指出互相冲突的上下界及对应机型。"""
    active = [p for p in printer_results if p["status"] != "error"]
    errored = [p for p in printer_results if p["status"] == "error"]

    if errored:
        e = errored[0]
        return CommonScaleRange(
            min=None,
            max=None,
            feasible=False,
            lower_bounds=[],
            upper_bounds=[],
            conflicts=[],
            blocked_by={
                "printer_id": e["printer_id"],
                "dpi": e["dpi"],
                "error": e["error"],
            },
            basis="存在机型连名义点阵化都无法排入条码框，共同区间无法成立",
        ).model_dump()

    lowers: list[dict] = []
    uppers: list[dict] = []
    for p in active:
        sr = p["scale_range"]
        lowers.extend(sr["bounds"]["lower"])
        uppers.extend(sr["bounds"]["upper"])

    lo_val = max(b["value"] for b in lowers)
    hi_val = min(b["value"] for b in uppers)
    feasible = lo_val <= hi_val

    conflicts = []
    if not feasible:
        # 互相冲突：达到最大下界 / 最小上界的全部约束（带来源机型）
        conflicts.extend(
            {**b, "value": _rb(b["value"])}
            for b in lowers
            if b["value"] >= lo_val - 1e-9
        )
        conflicts.extend(
            {**b, "value": _rb(b["value"])}
            for b in uppers
            if b["value"] <= hi_val + 1e-9
        )

    return CommonScaleRange(
        min=_rb(lo_val),
        max=_rb(hi_val),
        feasible=feasible,
        lower_bounds=[{**b, "value": _rb(b["value"])} for b in lowers],
        upper_bounds=[{**b, "value": _rb(b["value"])} for b in uppers],
        conflicts=conflicts,
        blocked_by=None,
        basis="各机型可行缩放区间（含最差套准/增益端点）的交集",
    ).model_dump()


# ---------------------------------------------------------------- 整批入口


def evaluate_fleet(
    label: LabelSpec,
    profiles: list[PrinterProfile],
    layouts: list[LayoutSpec],
) -> list[dict]:
    """逐版式 × 逐机型评估；单版式编码失败仅该版式行内 error。"""
    results = []
    for layout in layouts:
        # 编码只依赖码制与数据，与机型无关；失败则该版式整体行内错误
        try:
            bits, fullcode = encode_modules(
                layout.barcode.symbology, layout.barcode.data
            )
        except PreflightError as exc:
            results.append(
                {
                    "layout_id": layout.layout_id,
                    "status": "error",
                    "error": exc.to_dict(),
                    "printers": [_error_printer(p, exc) for p in profiles],
                    "common_scale_range": None,
                    "worst_margin": None,
                }
            )
            continue

        obstacles = _build_obstacles(layout)
        printer_results = [
            evaluate_printer(label, p, layout, bits, fullcode, obstacles)
            for p in profiles
        ]
        common = _common_scale_range(printer_results)

        if any(p["status"] in ("fail", "error") for p in printer_results):
            status = "fail"
        elif any(p["status"] == "warning" for p in printer_results):
            status = "warning"
        else:
            status = "ok"
        if not common["feasible"] and status == "ok":
            status = "warning"
        margins = [
            p["worst_margin"]
            for p in printer_results
            if p["worst_margin"] is not None
        ]
        results.append(
            {
                "layout_id": layout.layout_id,
                "status": status,
                "error": None,
                "printers": printer_results,
                "common_scale_range": common,
                "worst_margin": round(min(margins), 4) if margins else None,
            }
        )
    return results
