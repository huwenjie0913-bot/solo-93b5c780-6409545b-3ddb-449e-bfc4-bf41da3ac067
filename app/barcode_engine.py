"""条码编码与真实模块（module）条纹提取。

通过 python-barcode 生成与实际渲染完全一致的 0/1 模块序列，
再做游程分解得到条/空宽度（单位：模块）。
"""

from __future__ import annotations

from barcode.codex import Code128
from barcode.ean import EuropeanArticleNumber13

from .errors import UnencodableDataError


def ean13_check_digit(digits12: str) -> int:
    total = sum((3 if i % 2 else 1) * int(d) for i, d in enumerate(digits12))
    return (10 - total % 10) % 10


def encode_modules(symbology: str, data: str) -> tuple[str, str]:
    """返回 (模块位串, 含校验位的完整可读码)。

    非法字符、EAN-13 长度/校验位错误抛出 UnencodableDataError。
    """
    if symbology == "ean13":
        if not data.isdigit():
            raise UnencodableDataError(
                "EAN-13 只能包含数字 0-9",
                {"symbology": symbology, "data": data},
            )
        if len(data) not in (12, 13):
            raise UnencodableDataError(
                "EAN-13 需要 12 位数据（自动补校验位）或 13 位（含校验位）",
                {"symbology": symbology, "length": len(data)},
            )
        if len(data) == 13:
            expect = ean13_check_digit(data[:12])
            if expect != int(data[12]):
                raise UnencodableDataError(
                    f"EAN-13 校验位错误：应为 {expect}，实际为 {data[12]}",
                    {
                        "symbology": symbology,
                        "expected_check_digit": expect,
                        "given_check_digit": int(data[12]),
                    },
                )
        obj = EuropeanArticleNumber13(data[:12])
    else:  # code128
        try:
            obj = Code128(data)
        except Exception as exc:  # IllegalCharacterError 在构造期抛出
            raise UnencodableDataError(
                f"数据无法按 {symbology} 编码：{exc}",
                {"symbology": symbology, "data": data},
            ) from exc

    try:
        lines = obj.build()
    except Exception as exc:  # IllegalCharacterError 等
        raise UnencodableDataError(
            f"数据无法按 {symbology} 编码：{exc}",
            {"symbology": symbology, "data": data},
        ) from exc
    if not lines or not lines[0]:
        raise UnencodableDataError(
            f"数据无法按 {symbology} 编码：编码结果为空",
            {"symbology": symbology, "data": data},
        )
    return lines[0], obj.get_fullcode()


def runs_from_bits(bits: str) -> list[dict]:
    """把 0/1 模块串分解为条/空游程。

    返回 [{is_bar, start_module, modules}]，索引即条码元素序号。
    """
    runs: list[dict] = []
    cur = bits[0]
    start = 0
    for i, b in enumerate(bits[1:], 1):
        if b != cur:
            runs.append(
                {"is_bar": cur == "1", "start_module": start, "modules": i - start}
            )
            cur, start = b, i
    runs.append(
        {"is_bar": cur == "1", "start_module": start, "modules": len(bits) - start}
    )
    return runs
