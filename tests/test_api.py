import base64

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

BASE = {
    "label": {"width_mm": 100.0, "height_mm": 60.0},
    "printer": {"dpi": 203},
}


def layout(**kw):
    base = {
        "layout_id": "L1",
        "barcode": {
            "symbology": "code128",
            "data": "ABC123",
            "box": {"x_mm": 10, "y_mm": 10, "width_mm": 60, "height_mm": 20},
        },
    }
    base.update(kw)
    return base


def post(layouts):
    return client.post("/v1/preflight", json={**BASE, "layouts": layouts})


def test_ok_layout_passes():
    r = post([layout()])
    assert r.status_code == 200
    res = r.json()["results"][0]
    assert res["status"] in ("ok", "warning")
    assert res["barcode"]["modules"] > 0
    assert res["barcode"]["narrowest_bar_dots"] >= 1
    checks = {c["check"] for c in res["checks"]}
    assert {
        "module_rounding",
        "narrowest_bar",
        "out_of_bounds",
        "quiet_zone",
        "forbidden_zone",
        "text_collision",
    } <= checks
    # PNG 预览可解码
    assert base64.b64decode(res["preview_png_base64"])[:4] == b"\x89PNG"
    # 增粗/收窄模拟存在
    assert set(res["simulations"]) == {"thicken_1dot", "thin_1dot"}
    assert res["suggested_scale_range"]["feasible"]


def test_ean13_check_digit_validated():
    good = layout(barcode={
        "symbology": "ean13", "data": "5901234123457",
        "box": {"x_mm": 10, "y_mm": 10, "width_mm": 40, "height_mm": 20},
    })
    assert post([good]).json()["results"][0]["status"] != "error"
    bad = layout(barcode={
        "symbology": "ean13", "data": "5901234123458",
        "box": {"x_mm": 10, "y_mm": 10, "width_mm": 40, "height_mm": 20},
    })
    res = post([bad]).json()["results"][0]
    assert res["status"] == "error"
    assert res["error"]["code"] == "UNENCODABLE_DATA"
    assert res["error"]["details"]["expected_check_digit"] == 7


def test_code128_illegal_char():
    bad = layout(barcode={
        "symbology": "code128", "data": "中文标签",
        "box": {"x_mm": 10, "y_mm": 10, "width_mm": 60, "height_mm": 20},
    })
    res = post([bad]).json()["results"][0]
    assert res["status"] == "error"
    assert res["error"]["code"] == "UNENCODABLE_DATA"


def test_cannot_fit_box():
    tiny = layout(barcode={
        "symbology": "code128", "data": "A" * 30,
        "box": {"x_mm": 0, "y_mm": 0, "width_mm": 5, "height_mm": 10},
    })
    res = post([tiny]).json()["results"][0]
    assert res["status"] == "error"
    assert res["error"]["code"] == "CANNOT_FIT"
    assert "min_box_width_mm" in res["error"]["details"]


def test_rotation_out_of_bounds():
    rot = layout(barcode={
        "symbology": "code128", "data": "ABC123",
        "box": {"x_mm": 0, "y_mm": 0, "width_mm": 60, "height_mm": 20,
                "rotation_deg": 45},
    })
    res = post([rot]).json()["results"][0]
    oob = next(c for c in res["checks"] if c["check"] == "out_of_bounds")
    assert oob["status"] == "fail"
    oob_risk = next(r for r in res["risks"] if r["code"] == "OUT_OF_BOUNDS")
    assert oob_risk["element"]["type"] == "label_edge"


def test_forbidden_zone_collision():
    z = layout(
        forbidden_zones=[{"id": "cut1", "x_mm": 20, "y_mm": 10,
                          "width_mm": 10, "height_mm": 10}],
    )
    res = post([z]).json()["results"][0]
    fz = next(c for c in res["checks"] if c["check"] == "forbidden_zone")
    assert fz["status"] == "fail"
    assert fz["element"]["zone_ids"] == ["cut1"]


def test_quiet_zone_insufficient():
    # 条码框贴到标签左右边缘，静区必为 0
    q = layout(barcode={
        "symbology": "code128", "data": "ABC123",
        "box": {"x_mm": 0, "y_mm": 10, "width_mm": 100, "height_mm": 20},
    })
    res = post([q]).json()["results"][0]
    qz = next(c for c in res["checks"] if c["check"] == "quiet_zone")
    assert qz["status"] == "fail"


def test_batch_sorted_by_worst_margin():
    good = layout(layout_id="good")
    bad = layout(layout_id="bad", barcode={
        "symbology": "code128", "data": "ABC123",
        "box": {"x_mm": 0, "y_mm": 10, "width_mm": 100, "height_mm": 20},
    })
    err = layout(layout_id="err", barcode={
        "symbology": "ean13", "data": "123",
        "box": {"x_mm": 10, "y_mm": 10, "width_mm": 40, "height_mm": 20},
    })
    results = post([good, bad, err]).json()["results"]
    assert [r["layout_id"] for r in results][0] == "err"  # 错误最前
    rest = [r for r in results if r["status"] != "error"]
    margins = [r["worst_margin"] for r in rest]
    assert margins == sorted(margins)


def test_invalid_params_422():
    r = client.post("/v1/preflight", json={
        "label": {"width_mm": -1, "height_mm": 60},
        "printer": {"dpi": 203},
        "layouts": [layout()],
    })
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "INVALID_PARAMETERS"


def test_300dpi_more_dots():
    res203 = post([layout()]).json()["results"][0]
    r300 = client.post("/v1/preflight", json={
        **BASE, "printer": {"dpi": 300}, "layouts": [layout()],
    }).json()["results"][0]
    assert (r300["barcode"]["module_width_dots_ideal"]
            > res203["barcode"]["module_width_dots_ideal"])
