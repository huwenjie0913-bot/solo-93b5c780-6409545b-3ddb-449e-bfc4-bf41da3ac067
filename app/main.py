"""FastAPI 入口：标签点阵化预检 API。"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .analyzer import analyze_layout
from .errors import PreflightError
from .fleet import evaluate_fleet
from .render import render_preview
from .schemas import (
    FleetCompatibilityRequest,
    FleetCompatibilityResponse,
    PreflightRequest,
    PreflightResponse,
)

app = FastAPI(
    title="标签点阵化预检 API",
    version="1.1.0",
    description=(
        "把毫米尺寸的标签版式按打印机 DPI 点阵化，预检 Code 128 / EAN-13 "
        "条码的取整误差、最窄条点数、静区、越界与裁切禁区碰撞，"
        "并模拟条纹 ±1 点后的可扫描性。/v1/fleet-compatibility 进一步枚举"
        "多机型的套准偏移与墨点增益端点，做跨机型公差兼容性与共同缩放区间分析。"
    ),
)


@app.exception_handler(RequestValidationError)
async def validation_handler(request: Request, exc: RequestValidationError):
    """参数问题 -> 422 INVALID_PARAMETERS。"""
    return JSONResponse(
        status_code=422,
        content=jsonable_encoder(
            {
                "error": {
                    "code": "INVALID_PARAMETERS",
                    "message": "请求参数校验失败",
                    "details": exc.errors(),
                }
            }
        ),
    )


@app.exception_handler(PreflightError)
async def preflight_error_handler(request: Request, exc: PreflightError):
    return JSONResponse(
        status_code=exc.http_status, content={"error": exc.to_dict()}
    )


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.post("/v1/preflight", response_model=PreflightResponse)
def preflight(req: PreflightRequest):
    """一次提交多套版式；结果按最差裕量升序（风险最高在前）。

    单套版式的数据性问题不中断整批，以行内 error 返回：
    - UNENCODABLE_DATA：字符非法 / EAN-13 校验位错误
    - CANNOT_FIT：每模块 1 点也排不进给定条码框
    """
    results = []
    for layout in req.layouts:
        try:
            analysis = analyze_layout(req.label, req.printer, layout)
            analysis["preview_png_base64"] = render_preview(
                req.label, req.printer, layout, analysis
            )
            analysis.pop("_render", None)
            results.append(analysis)
        except PreflightError as exc:
            results.append(
                {
                    "layout_id": layout.layout_id,
                    "status": "error",
                    "error": exc.to_dict(),
                    "checks": [],
                    "simulations": {},
                    "risks": [],
                    "suggested_scale_range": None,
                    "worst_margin": None,
                    "preview_png_base64": None,
                }
            )

    # 按最差裕量排序：错误最前，其后按裕量升序
    def sort_key(r):
        if r["status"] == "error":
            return (0, 0.0)
        return (1, r["worst_margin"] if r["worst_margin"] is not None else 999.0)

    results.sort(key=sort_key)
    summary = {
        "total": len(results),
        "ok": sum(1 for r in results if r["status"] == "ok"),
        "warning": sum(1 for r in results if r["status"] == "warning"),
        "fail": sum(1 for r in results if r["status"] == "fail"),
        "error": sum(1 for r in results if r["status"] == "error"),
        "dpi": req.printer.dpi,
        "label_mm": [req.label.width_mm, req.label.height_mm],
    }
    return {"results": results, "summary": summary}


@app.post(
    "/v1/fleet-compatibility",
    response_model=FleetCompatibilityResponse,
)
def fleet_compatibility(req: FleetCompatibilityRequest):
    """同一批版式下发到多个机型：枚举公差端点工况，逐机型给最差状态与
    触发工况、失效条/空元素、静区/越界/禁区碰撞裕量，再求跨机型共同
    可行缩放区间；区间为空时给出互相冲突的上下界及对应机型。

    单套版式无法编码（UNENCODABLE_DATA）或某机型排不进框（CANNOT_FIT）
    均为行内 error，不中断其余版式 × 机型组合。结果排序沿用 /v1/preflight：
    错误版式最前，其后按跨机型最差裕量升序。
    """
    results = evaluate_fleet(req.label, req.printers, req.layouts)

    def sort_key(r):
        if r["status"] == "error":
            return (0, 0.0)
        return (
            1,
            r["worst_margin"] if r["worst_margin"] is not None else 999.0,
        )

    results.sort(key=sort_key)
    summary = {
        "total": len(results),
        "ok": sum(1 for r in results if r["status"] == "ok"),
        "warning": sum(1 for r in results if r["status"] == "warning"),
        "fail": sum(1 for r in results if r["status"] == "fail"),
        "error": sum(1 for r in results if r["status"] == "error"),
        "printers": [
            {"printer_id": p.printer_id, "dpi": p.dpi} for p in req.printers
        ],
        "common_scale_feasible": sum(
            1
            for r in results
            if r["common_scale_range"] and r["common_scale_range"]["feasible"]
        ),
        "label_mm": [req.label.width_mm, req.label.height_mm],
    }
    return {"results": results, "summary": summary}
