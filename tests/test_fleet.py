"""POST /v1/fleet-compatibility 端到端测试。"""

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def printer(pid, dpi, ox=(-1.0, 1.0), oy=(-1.0, 1.0), gain=(-1, 1)):
    return {
        "printer_id": pid,
        "dpi": dpi,
        "offset_x_mm": {"min_mm": ox[0], "max_mm": ox[1]},
        "offset_y_mm": {"min_mm": oy[0], "max_mm": oy[1]},
        "gain_dots": {"min_dots": gain[0], "max_dots": gain[1]},
    }


def layout(layout_id="A", **kw):
    base = {
        "layout_id": layout_id,
        "barcode": {
            "symbology": "code128",
            "data": "ABC123",
            "box": {"x_mm": 10, "y_mm": 10, "width_mm": 60, "height_mm": 20},
        },
    }
    base.update(kw)
    return base


def post(printers, layouts, label=None):
    body = {
        "label": label or {"width_mm": 100.0, "height_mm": 60.0},
        "printers": printers,
        "layouts": layouts,
    }
    return client.post("/v1/fleet-compatibility", json=body)


P203 = printer("P203", 203)
P300 = printer("P300", 300, ox=(-0.8, 0.8), oy=(-0.8, 0.8), gain=(0, 1))


def test_basic_fleet_result_per_printer_worst_case():
    r = post([P203, P300], [layout()])
    assert r.status_code == 200
    res = r.json()["results"][0]
    assert res["layout_id"] == "A"
    assert res["status"] in ("ok", "warning")
    by_id = {p["printer_id"]: p for p in res["printers"]}
    assert set(by_id) == {"P203", "P300"}
    for p in res["printers"]:
        assert p["status"] != "error"
        # 2^3 = 8 种公差端点组合全部重新评估
        assert p["combinations_evaluated"] == 8
        assert p["trigger_condition"] is not None
        cond = p["trigger_condition"]
        assert set(cond) == {"offset_x_mm", "offset_y_mm", "gain_dots"}
        check_names = {c["check"] for c in p["checks"]}
        assert {
            "narrowest_bar",
            "narrowest_space",
            "quiet_zone",
            "out_of_bounds",
            "forbidden_zone",
        } <= check_names
        # 每个检查的触发工况必须是声明过的端点
        for c in p["checks"]:
            tc = c["trigger_condition"]
            prof = next(q for q in (P203, P300) if q["printer_id"] == p["printer_id"])
            assert tc["offset_x_mm"] in (
                prof["offset_x_mm"]["min_mm"],
                prof["offset_x_mm"]["max_mm"],
            )
            assert tc["offset_y_mm"] in (
                prof["offset_y_mm"]["min_mm"],
                prof["offset_y_mm"]["max_mm"],
            )
            assert tc["gain_dots"] in (
                prof["gain_dots"]["min_dots"],
                prof["gain_dots"]["max_dots"],
            )
            assert c["worst_status"] in ("pass", "warn", "fail")
        # 300 DPI 模块点宽必然大于 203
    assert (
        by_id["P300"]["barcode"]["module_width_dots_ideal"]
        > by_id["P203"]["barcode"]["module_width_dots_ideal"]
    )
    # 共同可行缩放区间存在
    csr = res["common_scale_range"]
    assert csr["feasible"]
    assert csr["min"] <= csr["max"]
    assert csr["conflicts"] == []
    # 共同区间不能宽于任一机型自身区间
    for p in res["printers"]:
        sr = p["scale_range"]
        assert csr["min"] >= sr["min"] - 1e-6
        assert csr["max"] <= sr["max"] + 1e-6


def test_gain_extreme_closes_space_and_lists_failed_element():
    # 窄条码 + 大幅增粗：203 机型最窄空被 +2 点吃掉
    narrow = layout(barcode={
        "symbology": "code128", "data": "ABC123",
        "box": {"x_mm": 20, "y_mm": 10, "width_mm": 16, "height_mm": 20},
    })
    p203 = printer("P203", 203, gain=(0, 2))
    r = post([p203, P300], [narrow])
    res = r.json()["results"][0]
    p = next(p for p in res["printers"] if p["printer_id"] == "P203")
    assert p["status"] == "fail"
    assert p["failed_elements"], "增益端点下应报告失效条/空元素"
    for fe in p["failed_elements"]:
        assert fe["failure_reason"] in (
            "space_closed_by_gain",
            "narrow_bar_below_min_dots",
            "narrow_bar_zero_or_lost",
        )
        assert {"run_index", "start_module", "width_modules",
                "width_dots_nominal", "width_dots_under_gain"} <= fe.keys()
    # 触发工况取增益上界
    worst_space = next(c for c in p["checks"] if c["check"] == "narrowest_space")
    assert worst_space["worst_status"] == "fail"
    assert worst_space["trigger_condition"]["gain_dots"] == 2
    # 该机型自身缩放区间因此不可行，或共同区间被其下界顶起
    assert p["scale_range"]["min"] > 1.0


def test_offset_extreme_triggers_out_of_bounds():
    # 条码贴右缘放置；向右偏移 5mm 的端点必然越界
    tight = layout(barcode={
        "symbology": "code128", "data": "ABC123",
        "box": {"x_mm": 55, "y_mm": 10, "width_mm": 42, "height_mm": 20},
    })
    p = printer("W203", 203, ox=(0.0, 5.0), oy=(0.0, 0.0), gain=(0, 0))
    r = post([p], [tight])
    res = r.json()["results"][0]
    pr = res["printers"][0]
    oob = next(c for c in pr["checks"] if c["check"] == "out_of_bounds")
    assert oob["worst_status"] == "fail"
    assert oob["trigger_condition"]["offset_x_mm"] == 5.0
    assert oob["element"]["side"] == "right"
    assert oob["margin_mm"] < 0


def test_forbidden_zone_collision_only_under_worst_offset():
    # 名义实体不碰禁区，向左偏移端点后实体压入禁区
    lay = layout(forbidden_zones=[
        {"id": "cut", "x_mm": 5, "y_mm": 10, "width_mm": 6, "height_mm": 20}
    ])
    p = printer("F203", 203, ox=(-2.5, 0.0), oy=(0.0, 0.0), gain=(0, 0))
    r = post([p], [lay])
    pr = r.json()["results"][0]["printers"][0]
    fz = next(c for c in pr["checks"] if c["check"] == "forbidden_zone")
    assert fz["worst_status"] == "fail"
    assert fz["element"]["zone_ids"] == ["cut"]
    assert fz["trigger_condition"]["offset_x_mm"] == -2.5


def test_common_range_conflict_names_models_and_bounds():
    # 203：窄条码 + 允许收窄 1 点 -> 最窄条要求放大到 2 倍（下界）
    # 300：套准向左偏 -12mm 端点 -> 左侧静区迫使缩小（上界 < 2）
    p203 = printer("P203", 203, ox=(-1, 1), oy=(-1, 1), gain=(-1, 0))
    p300 = printer("P300", 300, ox=(-12, 2), oy=(-1, 1), gain=(0, 0))
    lay = layout(barcode={
        "symbology": "code128", "data": "ABC123",
        "box": {"x_mm": 12, "y_mm": 10, "width_mm": 14, "height_mm": 20},
    })
    r = post([p203, p300], [lay])
    res = r.json()["results"][0]
    csr = res["common_scale_range"]
    assert csr["feasible"] is False
    assert csr["min"] > csr["max"]
    lowers = [c for c in csr["conflicts"] if c["kind"] == "lower"]
    uppers = [c for c in csr["conflicts"] if c["kind"] == "upper"]
    assert lowers and uppers
    lo, hi = lowers[0], uppers[0]
    assert lo["value"] == 2.0
    assert lo["printer_id"] == "P203"
    assert lo["constraint"] == "narrowest_bar_dots"
    assert hi["value"] < lo["value"]
    assert hi["printer_id"] == "P300"
    # 所有上下界都带来源机型
    for b in csr["lower_bounds"] + csr["upper_bounds"]:
        assert b["printer_id"] in ("P203", "P300")
        assert b["dpi"] in (203, 300)
        assert b["constraint"]


def test_unencodable_layout_inline_error_does_not_stop_batch():
    bad = layout(layout_id="BAD", barcode={
        "symbology": "ean13", "data": "123",
        "box": {"x_mm": 10, "y_mm": 10, "width_mm": 40, "height_mm": 20},
    })
    good = layout(layout_id="GOOD")
    r = post([P203, P300], [bad, good])
    assert r.status_code == 200
    by_layout = {x["layout_id"]: x for x in r.json()["results"]}
    b = by_layout["BAD"]
    assert b["status"] == "error"
    assert b["error"]["code"] == "UNENCODABLE_DATA"
    # 每个机型行内也带错误，但不影响另一套版式
    assert all(p["status"] == "error" for p in b["printers"])
    assert b["common_scale_range"] is None
    g = by_layout["GOOD"]
    assert g["status"] != "error"
    assert len(g["printers"]) == 2


def test_cannot_fit_is_per_printer_inline_error():
    # "ABCDE" 共 90 模块：8mm 框在 203 DPI（64 点）排不下，300 DPI（94 点）可以
    lay = layout(layout_id="T", barcode={
        "symbology": "code128", "data": "ABCDE",
        "box": {"x_mm": 10, "y_mm": 10, "width_mm": 8, "height_mm": 10},
    })
    r = post([P203, P300], [lay])
    res = r.json()["results"][0]
    by_id = {p["printer_id"]: p for p in res["printers"]}
    assert by_id["P203"]["status"] == "error"
    assert by_id["P203"]["error"]["code"] == "CANNOT_FIT"
    assert by_id["P203"]["error"]["details"]["printer_id"] == "P203"
    assert by_id["P203"]["combinations_evaluated"] == 0
    assert by_id["P300"]["status"] != "error"
    # 任一机型无法排入时共同区间 blocked_by 指向该机型
    assert res["common_scale_range"]["feasible"] is False
    assert res["common_scale_range"]["blocked_by"]["printer_id"] == "P203"


def test_results_sorted_error_first_then_worst_margin():
    good = layout(layout_id="good", barcode={
        "symbology": "code128", "data": "ABC123",
        "box": {"x_mm": 20, "y_mm": 10, "width_mm": 30, "height_mm": 20},
    })
    risky = layout(layout_id="risky", barcode={
        "symbology": "code128", "data": "ABC123",
        "box": {"x_mm": 55, "y_mm": 10, "width_mm": 42, "height_mm": 20},
    })
    err = layout(layout_id="err", barcode={
        "symbology": "ean13", "data": "123",
        "box": {"x_mm": 10, "y_mm": 10, "width_mm": 40, "height_mm": 20},
    })
    p = printer("S", 203, ox=(0, 5), oy=(0, 0), gain=(0, 0))
    r = post([p], [good, risky, err])
    ids = [x["layout_id"] for x in r.json()["results"]]
    assert ids[0] == "err"
    rest = [x for x in r.json()["results"] if x["status"] != "error"]
    margins = [x["worst_margin"] for x in rest]
    assert margins == sorted(margins)


@pytest.mark.parametrize(
    "body_mod",
    [
        # 打印机 ID 重复
        {"printers": [P203, printer("P203", 300)], "layouts": [layout()]},
        # 版式 ID 重复
        {"printers": [P203], "layouts": [layout("DUP"), layout("DUP")]},
        # 偏差范围反向
        {
            "printers": [
                printer("BAD", 203, ox=(2.0, -2.0))
            ],
            "layouts": [layout()],
        },
    ],
)
def test_invalid_fleet_params_422(body_mod):
    body = {"label": {"width_mm": 100.0, "height_mm": 60.0}, **body_mod}
    r = client.post("/v1/fleet-compatibility", json=body)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "INVALID_PARAMETERS"


def test_preflight_endpoint_unchanged():
    """原有 /v1/preflight 的请求、响应与排序行为保持不变。"""
    body = {
        "label": {"width_mm": 100.0, "height_mm": 60.0},
        "printer": {"dpi": 203},
        "layouts": [
            layout(layout_id="ok", barcode={
                "symbology": "code128", "data": "ABC123",
                "box": {"x_mm": 10, "y_mm": 10, "width_mm": 60, "height_mm": 20},
            }),
            layout(layout_id="bad", barcode={
                "symbology": "code128", "data": "ABC123",
                "box": {"x_mm": 0, "y_mm": 10, "width_mm": 100, "height_mm": 20},
            }),
        ],
    }
    d = client.post("/v1/preflight", json=body).json()
    assert [r["layout_id"] for r in d["results"]] == ["bad", "ok"]
    ok = next(r for r in d["results"] if r["layout_id"] == "ok")
    assert ok["preview_png_base64"]
    assert set(ok["simulations"]) == {"thicken_1dot", "thin_1dot"}
    assert d["summary"]["dpi"] == 203
