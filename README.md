# 标签点阵化预检 API

毫米尺寸的标签版式在 203 / 300 DPI 打印机上点阵化后，窄条取整、静区被裁、
旋转越界都可能导致无法稳定扫描。本服务在打印前对版式做全面预检。

## 运行

```bash
pip install -r requirements.txt
uvicorn app.main:app --port 8000
pytest tests/          # 10 个用例
```

## 接口

### `POST /v1/preflight`

一次提交多套版式，结果**按最差裕量升序**（风险最高在前，错误版式最前）。

```json
{
  "label":   {"width_mm": 100, "height_mm": 60},
  "printer": {"dpi": 203},
  "layouts": [{
    "layout_id": "A",
    "barcode": {"symbology": "ean13", "data": "5901234123457",
                "box": {"x_mm": 10, "y_mm": 10, "width_mm": 40, "height_mm": 20,
                        "rotation_deg": 0}},
    "quiet_zone_mm": 3.0,          // 缺省按 10 个模块宽
    "min_narrow_dots": 1,          // 最窄条最小可接受点数
    "texts": [{"id": "t1", "content": "LOT", "x_mm": 60, "y_mm": 10,
               "width_mm": 30, "height_mm": 5}],
    "forbidden_zones": [{"id": "cut", "x_mm": 0, "y_mm": 50,
                         "width_mm": 100, "height_mm": 10}]
  }]
}
```

每套版式返回：

- **barcode**：模块数、理想/实际模块宽（点）、总宽（点/mm）、最大取整偏差 %、
  最窄条/空点数及其元素定位（游程序号、模块偏移）
- **checks**：`module_rounding` / `narrowest_bar` / `out_of_bounds` /
  `quiet_zone` / `forbidden_zone` / `text_collision`，各带 pass/warn/fail、
  归一化裕量（available/required，≥1 达标）与可定位元素
- **simulations**：`thicken_1dot` / `thin_1dot` —— 模拟条纹横向增粗/收窄
  1 点（油墨增益）后最窄条/空点数与是否有元素消失
- **risks**：风险原因 + 条码元素定位 + 建议
- **suggested_scale_range**：满足最窄条、框宽、静区、标签边界约束的缩放区间
- **preview_png_base64**：按打印机 DPI 渲染的 PNG 预览（真实点阵条纹，
  禁区红色、静区黄色、条码框蓝色、最窄条红框高亮）

## 错误分类

| 场景 | 位置 | code | HTTP |
|---|---|---|---|
| 参数问题（尺寸非正、字段缺失等） | 请求级 | `INVALID_PARAMETERS` | 422 |
| 不可编码数据（非法字符、EAN-13 校验位错） | 版式级 | `UNENCODABLE_DATA` | 行内 error |
| 无法排入给定框（每模块 1 点也超宽） | 版式级 | `CANNOT_FIT` | 行内 error |

批量提交时单套版式的数据问题不中断整批，以行内 `error` 返回。

## 实现要点

- 模块位串取自 python-barcode `build()`，与真实渲染一致；游程分解得条/空元素
- 点阵化采用**累计四舍五入**（模块边界取整），模拟真实光栅化，逐元素报告取整偏差
- 旋转在条码本地坐标系扩张静区后再旋转取包围盒，梯式放置（90°）也能正确检查
- 建议缩放区间同时受最窄条点数、框宽、静区富余、旋转包围盒边界四类约束
