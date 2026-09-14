"""请求/响应模型（Pydantic v2）。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


# ---------------------------------------------------------------- 请求


class PrinterSpec(BaseModel):
    dpi: int = Field(..., gt=0, le=1200, description="打印机分辨率，常见 203 / 300")


class LabelSpec(BaseModel):
    width_mm: float = Field(..., gt=0, le=1000)
    height_mm: float = Field(..., gt=0, le=1000)


class RectMM(BaseModel):
    id: str | None = None
    x_mm: float = Field(..., ge=0)
    y_mm: float = Field(..., ge=0)
    width_mm: float = Field(..., gt=0)
    height_mm: float = Field(..., gt=0)


class BarcodeBox(RectMM):
    rotation_deg: float = Field(
        0.0, ge=-360.0, le=360.0, description="绕条码框中心旋转，正值为顺时针"
    )


class BarcodeSpec(BaseModel):
    symbology: Literal["code128", "ean13"]
    data: str = Field(..., min_length=1, max_length=80)
    box: BarcodeBox


class TextBlock(BaseModel):
    id: str | None = None
    content: str = Field(..., min_length=1, max_length=200)
    x_mm: float = Field(..., ge=0)
    y_mm: float = Field(..., ge=0)
    width_mm: float = Field(..., gt=0)
    height_mm: float = Field(..., gt=0)
    rotation_deg: float = Field(0.0, ge=-360.0, le=360.0)


class LayoutSpec(BaseModel):
    layout_id: str = Field(..., min_length=1, max_length=64)
    barcode: BarcodeSpec
    quiet_zone_mm: float | None = Field(
        None, ge=0, description="静区阈值（mm）。缺省按 10 个模块宽计算"
    )
    min_narrow_dots: int = Field(
        1, ge=1, le=10, description="最窄条可接受的最小点数，低于即判 fail"
    )
    texts: list[TextBlock] = Field(default_factory=list)
    forbidden_zones: list[RectMM] = Field(
        default_factory=list, description="裁切禁区，条码及其静区不得进入"
    )

    @field_validator("barcode")
    @classmethod
    def _box_not_degenerate(cls, v: BarcodeSpec) -> BarcodeSpec:
        if v.box.width_mm <= 0 or v.box.height_mm <= 0:
            raise ValueError("条码框尺寸必须为正")
        return v


class PreflightRequest(BaseModel):
    label: LabelSpec
    printer: PrinterSpec
    layouts: list[LayoutSpec] = Field(..., min_length=1, max_length=50)


# ---------------------------------------------------------------- 响应


class ErrorBody(BaseModel):
    code: str
    message: str
    details: dict[str, Any] = {}


class CheckResult(BaseModel):
    check: str
    status: Literal["pass", "warn", "fail"]
    margin: float | None = Field(
        None, description="归一化裕量：available/required，>=1 为达标"
    )
    detail: str
    element: dict[str, Any] | None = None


class Risk(BaseModel):
    code: str
    message: str
    element: dict[str, Any] | None = None
    suggestion: str | None = None


class LayoutResult(BaseModel):
    layout_id: str
    status: Literal["ok", "warning", "fail", "error"]
    error: ErrorBody | None = None
    barcode: dict[str, Any] | None = None
    checks: list[CheckResult] = []
    simulations: dict[str, Any] = {}
    risks: list[Risk] = []
    suggested_scale_range: dict[str, Any] | None = None
    worst_margin: float | None = None
    preview_png_base64: str | None = None


class PreflightResponse(BaseModel):
    results: list[LayoutResult]
    summary: dict[str, Any]
