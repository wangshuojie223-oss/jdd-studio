# 参考图自动识别 设计文档（v1.4.0）

日期：2026-08-12
状态：已与杰确认（识别时机=传图即识别；置信度=全部自动填+徽章显示）

## 背景与目标

海报模式的参考图功能（v1.3.0）要求上传每张图时**手动输入角色名**，LLM 生成海报提示词时依据名字标注「（参考图片N）」。手打名字繁琐且容易写错（写错名 → 提示词标注错 → 人物对不上）。

本功能：上传参考图后，**视觉模型对照剧本角色表自动识别图中角色并填入名字**，附置信度徽章，名字可随时手改。已于 2026-08-12 用《我的护工丈夫是亿万富豪》剧本 + 4 张定妆照实测：gemini-3.6-flash 三次全对（4/4），缩图后单次识别秒级。

## 已确认的需求决策

1. **识别时机：传图即识别**。上传剧本时顺带抽角色表缓存；之后每传一张参考图立刻自动识别填名；早于剧本上传的图在剧本就绪后自动补识别。全程零额外按钮。
2. **置信度处理：全部自动填 + 徽章**。不设拦截阈值：识别出名字就自动填入，名字旁显示置信度（≥0.9 绿 / 0.6~0.9 黄 / 更低或无法匹配 红「未识别」）；识别为 null（图中人物不在剧本里）则留空。名字任何时刻可手改。
3. **范围：仅海报模式**。角色图模式无参考图功能，不动。

## 架构（方案A：角色表缓存 + 单图视觉识别）

```
上传剧本.docx ──► /api/script ──► ① roster.extract(剧本全文) ─► refs/roster.json（缓存）
                                  ② generate_poster_schemes（原有，8 组提示词）
                                  ③ 补识别 refs.json 里 pending 的图（批量一次调用）

上传参考图 ──► POST /api/refs ──► 存图 + refs.json
                                  └─ roster 已缓存？ vision.recognize(图, roster) 同步识别填名
                                                     否 → 标 pending，等剧本
```

不采用：每次识别送剧本全文（慢且贵）；从已生成提示词反推角色表（覆盖不全）。

## 组件

### ① `app/roster.py`（新增）

- `extract_roster(script_text, model=None) -> list[dict]`：文本 LLM（gemini-3.6-flash）从剧本抽角色表，字段 `name/gender/age/identity/appearance`，最多 8 个按戏份排序。提示词沿用实测版。
- `load_roster() / save_roster(roster)`：读写 `refs/roster.json`（本机状态，打包排除清单补充）。
- 抽取失败（LLM 报错/JSON 解析失败）返回空列表，**不阻塞**海报生成；roster.json 不存在时 `load_roster()` 返回 `[]`。

### ② `app/vision.py`（新增）

- `recognize(image_path: Path, roster: list[dict]) -> dict`：
  - PIL 缩图：最长边 768px、转 RGB、JPEG q80、base64（**必须缩图**：4 张 3MB 原图一次请求网关 400，且报错信息误导为 "missing or empty model"；缩后约 34KB/张）
  - 视觉模型（**固定 gemini-3.6-flash**，不受界面模型下拉影响）单张匹配：返回 `{"name": str|None, "confidence": float, "reason": str}`
  - roster 为空直接返回 `{"name": None, "confidence": 0, "reason": "无角色表"}`，不调 LLM
- `recognize_batch(image_paths, roster) -> list[dict]`：一次请求带多张缩略图（用于补识别；实测整批模式已验证）
- JSON 解析兼容 ```json 围栏（gemini-3.1-pro 的习惯），解析失败按 name=None 处理
- 依赖：新增 `pillow`

### ③ `app/main.py` 接口改动

- `POST /api/refs`（改）：存图后若 roster 非空 → 同步 `recognize()` 并把 `name`（识别结果）、`confidence`、`reason` 写入 refs.json 该条；roster 为空 → 该条标 `"pending": true`。表单 name 由必填改为**选填**（与前端④一致）：手填了则以手填为准、跳过识别；留空则由识别填名，识别也为 null 则名字暂空、红标「未识别」。识别 LLM 失败 → 图照常上传成功，标 pending，接口不报错。
- `POST /api/script`（改）：成功生成 8 组提示词后，若 refs.json 有 pending 条目 → `recognize_batch()` 补识别并写回；roster 抽取失败时跳过补识别。
- `POST /api/refs/rename`（新）：`{id, name}` 改名。现状改不了名只能删了重传，顺带补上；改名同时清除该条 `pending`。
- `POST /api/refs/recognize`（新）：`{id}` 单张重识别，写回 name/confidence/reason。
- refs.json 条目新字段均可选，**旧数据无这些字段时前端与接口照常工作**（视为已人工命名）。

### ④ 前端 `app/static/index.html`（参考图区）

- 参考图卡片：名字框（可编辑，失焦调 rename）+ 置信度徽章（绿/黄/红三档，红显示「未识别」）+ 「🔍」重识别小按钮
- 添加参考图弹窗：**名字改为选填**（填了则以手填为准，识别仅补空名）
- 海报模式上传剧本按钮旁状态提示：「角色表已就绪（N 角色）」/「无角色表（识别不可用）」

### ⑤ 打包与配置

- `工作台管家.command` 打包排除清单补 `refs/roster.json`（refs/ 整个目录已在排除清单内，确认即可）
- `pyproject.toml` 加 `pillow`
- 版本：1.3.1 → **1.4.0**（新功能），同步 VERSION / package.json / pyproject.toml / README 顶部

## 错误处理

| 场景 | 行为 |
|---|---|
| 上传图时无角色表 | 标 pending，提示「上传剧本后自动识别」 |
| 识别 LLM 超时/报错 | 图上传成功，标 pending，不弹错 |
| 视觉模型返回无法解析 | 按 name=None（红标「未识别」） |
| roster 抽取失败 | 仅日志警告，海报生成照旧 |
| 补识别部分失败 | 成功的写回，失败的保持 pending |

## 测试（TDD）

- `tests/test_roster.py`：extract_roster 提示词包含剧本全文；save/load 往返；load 不存在返回 []
- `tests/test_vision.py`：recognize 空 roster 不调 LLM 直接返回 null；```json 围栏解析；坏 JSON 返回 null；缩图尺寸 ≤768px（mock PIL 之外的部分，视觉调用 mock）
- `tests/test_refs.py`（扩）：上传时 roster 存在→自动填名；不存在→pending；rename 接口改名并清 pending；旧格式 refs.json（无 confidence/pending）兼容
- `tests/test_script_api.py`（扩）：/api/script 成功后 pending 条目被补识别（mock vision）
- 全部 20 个旧测试保持绿色

## 明确不做（YAGNI）

- 角色图模式的参考图（该模式无此功能）
- 置信度阈值拦截/人工确认流程（杰选全部自动填）
- 前端展示完整角色表（只在识别中后台使用）
- 重新上传剧本后自动重识别已命名图（名字以人工/首次识别为准，想重识别点 🔍）
