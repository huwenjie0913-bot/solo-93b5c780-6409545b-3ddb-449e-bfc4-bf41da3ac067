# 标签点阵化预检 API

毫米尺寸的标签版式在 203 / 300 DPI 打印机上点阵化后，窄条取整、静区被裁、
旋转越界都可能导致无法稳定扫描。本服务在打印前对版式做全面预检。

## 运行

```bash
pip install -r requirements.txt
uvicorn app.main:app --port 8000
pytest tests/          # 24 个用例
```

## 接口

### `POST /v1/fleet-compatibility`

同一批毫米版式下发到不同仓库的多台热敏打印机（如 203 / 300 DPI），
逐机型枚举**公差端点工况**并重新点阵化：横/纵向套准偏差各取 min/max 端点，
条纹增益（整数点，正为墨点增粗、负为收窄）取 min/max 端点，共 2³=8 种组合。

```json
{
  "label": {"width_mm": 100, "height_mm": 60},
  "printers": [{
    "printer_id": "WH-A-203",
    "dpi": 203,
    "offset_x_mm": {"min_mm": -1.5, "max_mm": 1.5},
    "offset_y_mm": {"min_mm": -1.0, "max_mm": 1.0},
    "gain_dots":   {"min_dots": -1, "max_dots": 1}
  }, {
    "printer_id": "WH-B-300",
    "dpi": 300,
    "offset_x_mm": {"min_mm": -0.8, "max_mm": 0.8},
    "offset_y_mm": {"min_mm": -0.8, "max_mm": 0.8},
    "gain_dots":   {"min_dots": 0, "max_dots": 1}
  }],
  "layouts": [ /* 与 /v1/preflight 相同的 LayoutSpec 列表，ID 唯一 */ ]
}
```

每“套版式 × 机型”返回：

- **status / worst_status / worst_margin**：8 种工况中的最差状态与归一化裕量
- **trigger_condition**：触发最差状态的端点工况（offset_x_mm / offset_y_mm / gain_dots）
- **checks**：`narrowest_bar` / `narrowest_space` / `quiet_zone` /
  `out_of_bounds` / `forbidden_zone` / `text_collision`，各带最差工况、
  归一化裕量与物理裕量（`margin_mm`，毫米）和可定位元素
- **failed_elements**：被最差增益吃掉的条/空（`space_closed_by_gain` /
  `narrow_bar_below_min_dots` / `narrow_bar_zero_or_lost`），含名义点数与
  增益后点数、游程序号与模块偏移
- **scale_range**：该机型上的可行缩放区间及全部上下界（带来源约束名）

每套版式还返回 **common_scale_range** —— 全部机型缩放区间的交集：
区间为空时 `conflicts` 列出相互冲突的最大下界 / 最小上界及其机型（printer_id、
DPI、约束名）；某机型连名义点阵化都排不进框时以 `blocked_by` 指明该机型。

单套版式无法编码（UNENCODABLE_DATA）或某机型 CANNOT_FIT 均为**行内 error**，
不中断其余版式 × 机型组合。结果排序与 `/v1/preflight` 一致：错误版式最前，
其后按跨机型最差裕量升序。

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
| 参数问题（尺寸非正、字段缺失、档案/版式 ID 重复、偏差范围反向等） | 请求级 | `INVALID_PARAMETERS` | 422 |
| 不可编码数据（非法字符、EAN-13 校验位错） | 版式级 | `UNENCODABLE_DATA` | 行内 error |
| 无法排入给定框（每模块 1 点也超宽） | 版式级（fleet 中为机型级） | `CANNOT_FIT` | 行内 error |

批量提交时单套版式的数据问题不中断整批，以行内 `error` 返回；
`/v1/fleet-compatibility` 中 CANNOT_FIT 只标记对应机型，其余机型照常评估。

## 实现要点

- 模块位串取自 python-barcode `build()`，与真实渲染一致；游程分解得条/空元素
- 点阵化采用**累计四舍五入**（模块边界取整），模拟真实光栅化，逐元素报告取整偏差
- 旋转在条码本地坐标系扩张静区后再旋转取包围盒，梯式放置（90°）也能正确检查
- 禁区/文字碰撞按旋转后的真实条码实体做分离轴（SAT）精确相交判定，不用 AABB 近似
- 预览缩略（超大标签降像素）时背景、目标框、条纹共用同一缩放比例
- 建议缩放区间同时受最窄条点数、框宽、静区富余、旋转包围盒边界四类约束
- 机队分析枚举 8 种公差端点组合逐机型重新点阵化；套准偏移只平移实体（不旋转），
  正增益让条纹两端各外延 g 个打印点，几何检查（越界/静区/禁区）均在该外延后的
  真实旋转矩形上做 SAT 判定
- 机型缩放上下界按最不利端点解析推导：下界取收窄端的最窄条、增粗端的最窄空；
  上界取框宽（含增益外延）、四个偏移端点中最近的标签边/静区留界，以及每个裁切
  禁区在 4 条 SAT 分离轴上的临界缩放（禁区 × 偏移组合取最保守值，任何 s>0 都
  无法分离时上界为 0，区间即不可行）；
  共同区间为空时给出顶起下界与压住上界的具体机型与约束
- 失效条/空按工况遍历全部游程汇总：同一元素在更严苛增益端点下重复失效时只保留
  最严快照，+2 点增益闭合多个内部空时会逐个列出，而非仅报最窄的一个
