"""领域错误类型：区分参数问题、不可编码数据、无法排入给定框。"""


class PreflightError(Exception):
    """预检领域错误基类。"""

    code = "PREFLIGHT_ERROR"
    http_status = 400

    def __init__(self, message: str, details: dict | None = None):
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "details": self.details}


class UnencodableDataError(PreflightError):
    """数据无法按所选码制编码（非法字符、EAN-13 校验位错误等）。"""

    code = "UNENCODABLE_DATA"
    http_status = 400


class CannotFitError(PreflightError):
    """即使每模块只占 1 个打印点，条码也无法排入给定条码框。"""

    code = "CANNOT_FIT"
    http_status = 409
