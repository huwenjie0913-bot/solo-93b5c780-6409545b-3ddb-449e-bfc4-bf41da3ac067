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


# ---------------------------------------------------------------- 机队请求


class OffsetRangeMM(BaseModel):
    """单方向走纸/套准偏差范围（毫米），min <= max，正负皆可。"""

    min_mm: float
    max_mm: float

    @field_validator("max_mm")
    @classmethod
    def _ordered(cls, v: float, info) -> float:
        if info.data.get("min_mm") is not None and v < info.data["min_mm"]:
            raise ValueError("偏差范围 max_mm 不能小于 min_mm")
        return v

    def endpoints(self) -> tuple[float, float]:
        return self.min_mm, self.max_mm


class GainRange(BaseModel):
    """条纹增益范围（整数打印点）：正值增粗（墨点增益），负值收窄。"""

    min_dots: int = Field(..., ge=-10, le=10)
    max_dots: int = Field(..., ge=-10, le=10)

    @field_validator("max_dots")
    @classmethod
    def _ordered(cls, v: int, info) -> int:
        if info.data.get("min_dots") is not None and v < info.data["min_dots"]:
            raise ValueError("增益范围 max_dots 不能小于 min_dots")
        return v

    def endpoints(self) -> tuple[int, int]:
        return self.min_dots, self.max_dots


class PrinterProfile(BaseModel):
    printer_id: str = Field(..., min_length=1, max_length=64)
    dpi: int = Field(..., gt=0, le=1200, description="打印机分辨率，常见 203 / 300")
    offset_x_mm: OffsetRangeMM = Field(
        ..., description="横向（走纸宽度方向）套准偏差范围，mm"
    )
    offset_y_mm: OffsetRangeMM = Field(
        ..., description="纵向（走纸方向）套准偏差范围，mm"
    )
    gain_dots: GainRange = Field(
        ..., description="条纹增益范围（整数点）：正为油墨增粗，负为收窄"
    )


class FleetCompatibilityRequest(BaseModel):
    label: LabelSpec
    printers: list[PrinterProfile] = Field(..., min_length=1, max_length=20)
    layouts: list[LayoutSpec] = Field(..., min_length=1, max_length=50)

    @field_validator("printers")
    @classmethod
    def _unique_printer_ids(cls, v: list[PrinterProfile]) -> list[PrinterProfile]:
        ids = [p.printer_id for p in v]
        dup = {i for i in ids if ids.count(i) > 1}
        if dup:
            raise ValueError(f"打印机档案 ID 必须唯一，重复：{sorted(dup)}")
        return v

    @field_validator("layouts")
    @classmethod
    def _unique_layout_ids(cls, v: list[LayoutSpec]) -> list[LayoutSpec]:
        ids = [l.layout_id for l in v]
        dup = {i for i in ids if ids.count(i) > 1}
        if dup:
            raise ValueError(f"版式 ID 必须唯一，重复：{sorted(dup)}")
        return v


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


class FleetCondition(BaseModel):
    """触发最差状态的公差端点工况。"""

    offset_x_mm: float
    offset_y_mm: float
    gain_dots: int


class FleetCheckResult(BaseModel):
    check: str
    worst_status: Literal["pass", "warn", "fail"]
    trigger_condition: FleetCondition
    margin: float | None = Field(
        None, description="最差工况下的归一化裕量 available/required，≥1 达标"
    )
    margin_mm: float | None = Field(
        None, description="最差工况下的物理裕量（mm），用于静区/越界/禁区"
    )
    detail: str
    element: dict[str, Any] | None = None


class PrinterCompatibilityResult(BaseModel):
    printer_id: str
    dpi: int
    status: Literal["ok", "warning", "fail", "error"]
    error: ErrorBody | None = None
    worst_status: Literal["ok", "warning", "fail"] | None = None
    worst_margin: float | None = None
    trigger_condition: FleetCondition | None = None
    failed_elements: list[dict[str, Any]] = []
    checks: list[FleetCheckResult] = []
    scale_range: dict[str, Any] | None = None
    combinations_evaluated: int = 0
    barcode: dict[str, Any] | None = None


class BoundConflict(BaseModel):
    kind: Literal["lower", "upper"]
    value: float
    printer_id: str
    dpi: int
    constraint: str


class CommonScaleRange(BaseModel):
    min: float | None = None
    max: float | None = None
    feasible: bool
    lower_bounds: list[dict[str, Any]] = []
    upper_bounds: list[dict[str, Any]] = []
    conflicts: list[BoundConflict] = Field(
        default_factory=list,
        description="区间为空时，相互冲突的上下界及其来源机型",
    )
    blocked_by: dict[str, Any] | None = None
    basis: str


class FleetLayoutResult(BaseModel):
    layout_id: str
    status: Literal["ok", "warning", "fail", "error"]
    error: ErrorBody | None = None
    printers: list[PrinterCompatibilityResult] = []
    common_scale_range: CommonScaleRange | None = None
    worst_margin: float | None = None


class FleetCompatibilityResponse(BaseModel):
    results: list[FleetLayoutResult]
    summary: dict[str, Any]
