# 飞书角色候选表导入参考图 设计文档（v1.5.0）

日期：2026-08-12
状态：已与杰确认（取图路径=导出 xlsx 解析；导入即替换；手动上传保留共存）

## 背景与目标

逐张上传参考图太繁琐。团队实际工作流：每部剧在飞书维护一张《角色候选表》，每行一个角色、多张候选定妆照，**选定的那张放在「定版」列**（杰口述"黄底"=选定标记，导出件中体现为定版列；设计兼容两种信号）。目标：工作台直接从导出的 xlsx 批量导入选定参考图，名字从表格读，免手打、免视觉识别。

## 已确认的决策

1. **访问路径：飞书导出 xlsx → 工作台解析**。已用真实文件验证：黄底填充色、图片锚点行列、嵌入图原分辨率（1088×1920 PNG，~2.4-3.3MB）均可读。
2. **导入即替换**：导入时清空现有参考图（前端弹确认），编号=表格行序。
3. **两种方式共存**：手动单张上传+视觉识别（v1.4.0）完整保留。
4. **导入条目视为人工选定**：不写 confidence/pending，前端无徽章。

## 真实表格结构（《我的护工丈夫是亿万富豪》角色候选表.xlsx 实测）

- sheet「角色候选表」：A=人物（`Evelyn Hart｜22岁｜女主角`）、B=简介、C=定版、D~K=候选1~8、L=修改意见、M~P=淡妆版/同类型新脸
- 每行定版列恰 1 张图（8 角色 8 张）；另有 sheet「场景候选表」（名称/图片/修改意见）——**不导入**（仅角色表）

## 组件

### ① `app/castsheet.py`（新增）— 解析器

`parse_castsheet(xlsx_path: Path) -> dict`，返回：
```python
{"sheet": "角色候选表",
 "characters": [{"name": "Evelyn Hart", "intro": "...", "image": b"<PNG字节>"|None}, ...]}
```
- **定位**：在全部 sheet 中找第一个含「人物」表头的（前 10 行内扫描）
- **列**：人物/简介按表头文字定位；取图列=「定版」列；**无「定版」列表头时 → 找黄色填充单元格（R高G高B低的黄色系，如 FFFFFF00/FFFFF258）上的图**（黄底兜底）
- **名字**：人物列值按 `｜` 切分取首段；无 `｜` 取整值
- **图片归位**：按锚点 `_from`（row/col）映射到单元格；TwoCellAnchor/OneCellAnchor 都支持；AbsoluteAnchor 跳过
- **缺图行**：image=None，照常入列（接口层标 `has_image: false`）
- 无「人物」表头 → 抛 `CastSheetError("找不到「人物」列…")`

### ② `app/main.py` 接口（两步，先预览后落盘）

- `POST /api/refs/import`：上传 xlsx → 解析 → 图片暂存 `refs/.staging_<uuid>/` → 返回 `{"token", "sheet", "total", "characters":[{"name","intro","has_image","w","h"}]}`；解析失败 400。发起新导入时清掉旧 staging 目录
- `POST /api/refs/import/confirm`：`{"token"}` → **替换语义**：删除现有全部参考图文件与清单条目 → staging 图片移入 refs/（新 id）→ 写 refs.json（含 name/intro，无 confidence）→ 删 staging → 返回新清单。token 无效 404

### ③ 前端 `app/static/index.html`

- 参考图区加「📥 从角色候选表导入」按钮+隐藏 file input（accept=.xlsx）
- 选择文件 → POST import → 弹 confirm 预览（角色名+是否缺图清单，「将替换现有 N 张参考图」警示）→ 确认 → POST confirm → refreshRefs()
- 手动上传区原样保留

### ④ 依赖与版本

- pyproject 加 `openpyxl>=3.1`（pillow 已有）
- 版本 1.4.0 → **1.5.0**（VERSION/package.json/pyproject/README 四处）

## 错误处理

| 场景 | 行为 |
|---|---|
| 非 xlsx/损坏 | 400「无法解析」 |
| 找不到「人物」表头 | 400 列出 sheet 名 |
| 有定版列但某行无图 | 预览标「缺图」，confirm 时跳过该行（不入清单） |
| 无定版列且无黄底格 | 400「找不到定版列或黄底标记」 |
| confirm token 失效 | 404 |

## 测试（TDD）

- `tests/test_castsheet.py`：fixture 在测试内用 openpyxl 现场构建（小 PNG 经 `openpyxl.drawing.image.Image` 锚入单元格）——表头定位、定版列取图、黄底兜底、名字 `｜` 切分、缺图行、无表头报错
- `tests/test_refs.py`（扩）：import 预览返回角色清单+staging 不落 refs.json；confirm 后旧条目/旧文件被替换、新条目含 name/intro 无 confidence；坏 token 404；坏文件 400
- 39 个旧测试保持绿色

## 明确不做（YAGNI）

- 场景候选表导入
- 飞书 API 直连（杰已选导出路线）
- 导入图再跑视觉识别（表格自带名字）
- 简介 intro 参与提示词生成（先存着，不进 promptgen）
