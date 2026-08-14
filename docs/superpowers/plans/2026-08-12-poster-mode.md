# 功能2「剧本海报模式」实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 工作台增加海报模式：上传剧本 docx → LLM 出 8 组海报提示词（6亮2暗，含平台安全区约束）→ 剧多多资产卡批量生成 → 画廊挑选 → 剪映扩图复审闭环。

**Architecture:** 单页双模式（顶部切换），复用现有任务队列/批量管线/进度/终止/画廊/保存；新增 docx 解析、海报 skill 提示词、复审模块（review_pending/review_done + 剪映驱动一期半自动兜底）。

**Tech Stack:** FastAPI + Playwright + SQLite（stdlib）+ python-docx（新增依赖）+ 原生 JS 单页。

## Global Constraints

- spec：`docs/superpowers/specs/2026-08-12-poster-mode-design.md`
- **项目不是 git 仓库**：所有 commit 步骤跳过，以「运行验证」替代
- 版本规则：本功能交付时 VERSION / package.json / README 三处同步升到 **1.2.0**
- 提示词模板做成 Cherry 技能库 skill 文件（运行时实时读取 + 内置兜底副本），与角色 skill 同机制
- 安全区量化值（必须逐字使用）：顶部裁剪区 0%~12.6%、文案警戒线 12.6%~15.4%、安全区 15.4%~78%、数据裁剪区 78%~87%、底部裁剪区 87%~100%
- 海报提示词硬规则：主标题=剧本英文名；禁止主标题以外任何文字；人物+标题在垂直 15%~78% 区间
- 后端改动后必须重启服务验证；前端静态文件实时读盘不用重启
- 测试运行方式：`cd ~/jdd-studio && uv run --with pytest python -m pytest tests/ -v`

---

### Task 1: 数据层（kind 列 + reviews 表）与依赖

**Files:**
- Modify: `app/store.py`
- Modify: `pyproject.toml`（加 python-docx）
- Test: `tests/test_store.py`

**Interfaces:**
- Produces:
  - `store.create_generation(prompt, character_name="", prompts=None, kind="character") -> dict`
  - `store.create_review(gen_id, src_path) -> dict`（reviews 行：id/gen_id/src_path/file_name/status=pending/expanded_name/created_at/updated_at）
  - `store.list_reviews(status=None) -> list[dict]`
  - `store.update_review(rid, **fields)`（同 update_generation 语义）
  - `store.delete_review(rid) -> bool`
  - generations 行 dict 新增 `kind` 字段

- [ ] **Step 1: 写失败测试**

```python
# tests/test_store.py
import time

def test_kind_column_and_reviews(tmp_path, monkeypatch):
    from app import store, config
    monkeypatch.setattr(store, "_con", None)
    monkeypatch.setattr(config, "load", lambda: {})  # 不读真 config 也无所谓
    # 把数据库重定向到临时目录
    monkeypatch.setattr(store.config, "root", lambda: tmp_path)
    store._con = None

    g = store.create_generation("p1", "海报", prompts=["p1"], kind="poster")
    assert g["kind"] == "poster"
    g2 = store.create_generation("p2", "jj")  # 默认 character
    assert g2["kind"] == "character"

    r = store.create_review(g["id"], "candidates/abc/x_01.png")
    assert r["status"] == "pending" and r["file_name"] == "x_01.png"
    store.update_review(r["id"], status="approved", expanded_name="x_01_big.png")
    rows = store.list_reviews(status="approved")
    assert len(rows) == 1 and rows[0]["expanded_name"] == "x_01_big.png"
    assert store.delete_review(r["id"]) is True
    assert store.list_reviews() == []

def test_old_db_migration(tmp_path, monkeypatch):
    """旧库（无 kind 列、无 reviews 表）打开后自动迁移。"""
    import sqlite3
    from app import store
    db = tmp_path / "tasks.db"
    con = sqlite3.connect(str(db))
    con.execute("CREATE TABLE generations (id TEXT PRIMARY KEY, character_name TEXT DEFAULT '', prompt TEXT NOT NULL, prompts TEXT DEFAULT '[]', status TEXT NOT NULL DEFAULT 'queued', stage TEXT DEFAULT '', error TEXT DEFAULT '', image_paths TEXT DEFAULT '[]', created_at REAL NOT NULL, updated_at REAL NOT NULL)")
    con.execute("INSERT INTO generations (id, prompt, created_at, updated_at) VALUES ('old1','hello',1,1)")
    con.commit(); con.close()

    monkeypatch.setattr(store, "_con", None)
    monkeypatch.setattr(store.config, "root", lambda: tmp_path)
    g = store.get_generation("old1")
    assert g is not None and g["kind"] == "character"  # 迁移后默认 character
    assert store.list_reviews() == []  # reviews 表已建
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd ~/jdd-studio && uv run --with pytest python -m pytest tests/test_store.py -v`
Expected: FAIL（create_review 不存在 / kind 不存在）

- [ ] **Step 3: 实现**

`pyproject.toml` 的 dependencies 加 `"python-docx"`。

`store.py`：
- SCHEMA 追加：

```sql
CREATE TABLE IF NOT EXISTS reviews (
    id TEXT PRIMARY KEY,
    gen_id TEXT DEFAULT '',
    src_path TEXT NOT NULL,
    file_name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',  -- pending/approved/rejected
    expanded_name TEXT DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
```

- `_conn()` 的旧库迁移块在现有 prompts 迁移后追加：

```python
        if "kind" not in cols:
            _con.execute("ALTER TABLE generations ADD COLUMN kind TEXT DEFAULT 'character'")
```

- `create_generation` 签名加 `kind: str = "character"`，INSERT 列加 kind。
- 新增：

```python
def create_review(gen_id: str, src_path: str) -> dict:
    rid = uuid.uuid4().hex[:12]
    now = time.time()
    file_name = src_path.rsplit("/", 1)[-1]
    with _lock:
        _conn().execute(
            "INSERT INTO reviews (id, gen_id, src_path, file_name, status, created_at, updated_at) VALUES (?,?,?,?,'pending',?,?)",
            (rid, gen_id, src_path, file_name, now, now),
        )
        _conn().commit()
    return [r for r in list_reviews() if r["id"] == rid][0]


def list_reviews(status: str | None = None) -> list[dict]:
    with _lock:
        if status:
            rows = _conn().execute("SELECT * FROM reviews WHERE status=? ORDER BY created_at", (status,)).fetchall()
        else:
            rows = _conn().execute("SELECT * FROM reviews ORDER BY created_at").fetchall()
    return [dict(r) for r in rows]


def update_review(rid: str, **fields):
    if not fields:
        return
    fields["updated_at"] = time.time()
    cols = ", ".join(f"{k}=?" for k in fields)
    with _lock:
        _conn().execute(f"UPDATE reviews SET {cols} WHERE id=?", (*fields.values(), rid))
        _conn().commit()


def delete_review(rid: str) -> bool:
    with _lock:
        cur = _conn().execute("DELETE FROM reviews WHERE id=?", (rid,))
        _conn().commit()
    return cur.rowcount > 0
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run --with pytest python -m pytest tests/test_store.py -v`
Expected: 2 passed

- [ ] **Step 5: 验证点**——`uv sync` 确认 python-docx 装上；服务能正常重启（旧 tasks.db 真实迁移不炸）。

---

### Task 2: 海报提示词 skill 文件 + promptgen 扩展

**Files:**
- Create: `~/Library/Application Support/CherryStudio/Data/Skills/film-poster-prompter/SKILL.md`
- Modify: `app/promptgen.py`
- Test: `tests/test_promptgen.py`

**Interfaces:**
- Consumes: Task 1 无依赖
- Produces:
  - `promptgen.load_poster_prompt() -> str`（运行时读 skill 文件，异常退回内置副本）
  - `promptgen.generate_poster_schemes(script_text: str, model: str | None = None) -> dict`，返回 `{"character": 英文剧名, "face_base": 基调, "schemes": [{name, prompt, note}×8], "model", "raw"}`（键名与 generate_schemes 一致，前端复用）
  - `parse_schemes` 支持 `## 剧名：` 与 `**剧本基调（8 组共用）**` 两种标题（向后兼容角色版）

- [ ] **Step 1: 写失败测试**

```python
# tests/test_promptgen.py
import asyncio
from app import promptgen

SAMPLE = """## 剧名：ECHOES OF SILENCE
**剧本基调（8 组共用）**：悬疑惊悚，压抑中爆发
### 方案一｜群像对峙（亮调）
纯白到冷蓝渐变背景的五人对峙海报……主标题"ECHOES OF SILENCE"以金属光效悬浮于画面视觉中心……
> 设计说明：对峙构图外化冲突
### 方案二｜暗夜独行（暗调）
低-key布光，主角剪影……
> 设计说明：压迫感
"""

def test_parse_poster_format():
    r = promptgen.parse_schemes(SAMPLE)
    assert r["character"] == "ECHOES OF SILENCE"
    assert "悬疑" in r["face_base"]
    assert len(r["schemes"]) == 2
    assert "亮调" in r["schemes"][0]["name"]
    assert "暗调" in r["schemes"][1]["name"]

def test_parse_character_format_still_works():
    text = "## 角色：冷面杀手\n**面部基底（6 组共用）**：高颧骨\n### 方案一｜冷峻\n提示词内容\n> 设计说明：x"
    r = promptgen.parse_schemes(text)
    assert r["character"] == "冷面杀手" and r["face_base"] == "高颧骨"

def test_load_poster_prompt_reads_skill_file():
    p = promptgen.load_poster_prompt()
    assert "15%" in p and "78%" in p and "禁止" in p  # 安全区规则在模板里
    assert not p.startswith("---")  # frontmatter 已剥除
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run --with pytest python -m pytest tests/test_promptgen.py -v`
Expected: FAIL（load_poster_prompt 不存在 / 剧名解析不到）

- [ ] **Step 3: 实现**

3a. 创建 skill 文件 `~/Library/Application Support/CherryStudio/Data/Skills/film-poster-prompter/SKILL.md`（同时让它成为 Cherry 技能库可维护的正式 skill）：

```markdown
---
name: 影视海报设计Prompt工程师
description: 把剧本全文转换为好莱坞电影海报的中文文生图提示词，一次输出同一剧本的 8 组海报方案（6 组亮调、2 组暗调）。真人质感欧美人物、戏剧冲突构图、英文主标题带光效、严格遵守平台封面安全区（人物与标题在垂直 15%~78% 区间）。当用户提到「海报」「剧本出海报」「封面」「宣传图」时使用；输入通常是剧本全文或剧本文档。不用于单角色肖像（那是 film-character-prompter）。
---

# 影视海报设计 Prompt 工程师

把剧本全文翻译成 8 组好莱坞电影海报文生图提示词：真人质感、戏剧冲突、标题醒目、严守平台安全区。

## 流程

1. **解析剧本**：提取剧名（英文名）、剧情基调、3~5 个关键人物（姓名、性格、身份、彼此关系）。
2. **标题定名**：主标题用剧本英文名替换模板中的 XXXXX 占位。文档没有英文名 → 你起一个符合基调的英文名，并在剧名行后标注（AI 起名）。
3. **生成 8 组方案**：6 组亮调、2 组暗调。每组一个构图方向（如：群像对峙 / 主角特写+群像剪影 / 三角关系 / 动作瞬间 / 象征意象 / 远景史诗），人物组合与站位必须体现性格、身份与人物关系。
4. **输出格式**：

```
## 剧名：{英文剧名}
**剧本基调（8 组共用）**：{一句话}
### 方案一｜{方向名}（亮调）
{提示词}
> 设计说明：{一句话}
```

每组提示词完整独立、可直接复制；亮调/暗调标注在方案名末尾括号里。

## 锁定元素（每组提示词必须包含）

```
好莱坞电影海报，9:16 竖版，真人质感，真实欧美人物，电影级布光，细腻震撼，充满戏剧冲突。{人物组合：姓名/外观/服装/站位/彼此关系}。主标题"XXXXX"以{符合剧本调性的光效与装饰元素}呈现，位置靠近画面视觉中心。{亮调：明亮高反差 / 暗调：低-key悬疑}。重要：主要人物与主标题整体位于画面垂直方向 15%~78% 的区间内，垂直方向尽可能居中；画面顶部 15% 与底部 22% 为平台裁剪区，严禁放置人物头部、标题与任何关键视觉元素。画面中禁止出现主标题以外的任何文字。超高分辨率，大师作品。
```

## 规范

- 每组 **250 字以内**，连贯段落、不堆标签；具体名词 > 抽象形容词
- 亮调 6 组在前、暗调 2 组在后；两组暗调构图方向不得重复亮调已用过的
- 只输出提示词文本，不生图、不存档

## 边界

- 剧本缺人物信息 → 按剧情合理补全，不追问
- 导演指令与本规范冲突 → 以导演最新指令为准，并一句话说明改动点
- 严格遵守输出格式，不要输出格式之外的多余内容
```

3b. `promptgen.py`：
- 常量加 `POSTER_SKILL_FILE = Path.home() / "Library/Application Support/CherryStudio/Data/Skills/film-poster-prompter/SKILL.md"`
- 把现有 `load_system_prompt()` 重构为通用 `_load_skill(path, fallback)`，`load_system_prompt()` 与 `load_poster_prompt()` 都是它的薄封装；新增 `_FALLBACK_POSTER_PROMPT`（内容 = 上面 skill 正文去掉 frontmatter 的完整副本）
- `parse_schemes` 两个正则改为兼容：`r"^##\s*(?:角色|剧名)[:：]\s*(.+)$"` 和 `r"\*\*(?:面部基底|剧本基调)（[^）]*）\*\*[:：]\s*(.+)"`
- 抽出 `_call_llm(system_prompt, user_msg, model)`（现有重试逻辑原样搬入）；`generate_schemes` 改调它；新增：

```python
async def generate_poster_schemes(script_text: str, model: str | None = None) -> dict:
    """剧本全文 → 8 组海报提示词（6 亮调 2 暗调）。"""
    user_msg = f"剧本全文：\n{script_text}\n\n请按流程输出 8 组海报提示词（前 6 组亮调、后 2 组暗调）。"
    return await _call_llm(load_poster_prompt(), user_msg, model)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run --with pytest python -m pytest tests/test_promptgen.py -v`
Expected: 3 passed

- [ ] **Step 5: 验证点**——CLI 自测角色版不回退：`uv run python -m app.promptgen "测试" 2>&1 | head -5` 不崩（LLM 不通也能看到调用动作）。

---

### Task 3: docx 解析模块 + /api/script 接口

**Files:**
- Create: `app/scriptdoc.py`
- Modify: `app/main.py`
- Test: `tests/test_scriptdoc.py`

**Interfaces:**
- Consumes: Task 2 的 `generate_poster_schemes`
- Produces:
  - `scriptdoc.extract_text(path: Path) -> str`（段落+表格文本，截断 20000 字）
  - `POST /api/script`（multipart 表单：file=docx, model=可选）→ 返回 `{"character": 剧名, "face_base": 基调, "schemes": [...], "model": ...}`；错误：400（非 docx/解析失败）、502（LLM 失败）

- [ ] **Step 1: 写失败测试**

```python
# tests/test_scriptdoc.py
from pathlib import Path

def _make_docx(path: Path, paragraphs, table_rows=None):
    from docx import Document
    doc = Document()
    for p in paragraphs:
        doc.add_paragraph(p)
    if table_rows:
        t = doc.add_table(rows=len(table_rows), cols=2)
        for i, (a, b) in enumerate(table_rows):
            t.rows[i].cells[0].text = a
            t.rows[i].cells[1].text = b
    doc.save(str(path))

def test_extract_paragraphs_and_tables(tmp_path):
    from app import scriptdoc
    fp = tmp_path / "s.docx"
    _make_docx(fp, ["《Silent Echo》", "第一集 雨夜", "杰克：男，35岁，刑警"], table_rows=[("英文名", "SILENT ECHO")])
    text = scriptdoc.extract_text(fp)
    assert "雨夜" in text and "杰克" in text and "SILENT ECHO" in text

def test_extract_truncates(tmp_path):
    from app import scriptdoc
    fp = tmp_path / "big.docx"
    _make_docx(fp, ["字" * 30000])
    assert len(scriptdoc.extract_text(fp)) <= 20050  # 截断 + 省略标记

def test_extract_rejects_bad_file(tmp_path):
    from app import scriptdoc
    fp = tmp_path / "x.docx"
    fp.write_text("不是docx")
    try:
        scriptdoc.extract_text(fp)
        assert False, "应该抛 ScriptDocError"
    except scriptdoc.ScriptDocError:
        pass
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run --with pytest python -m pytest tests/test_scriptdoc.py -v`
Expected: FAIL（模块不存在）

- [ ] **Step 3: 实现 `app/scriptdoc.py`**

```python
"""剧本 Word 文档解析：提取全文文本（段落 + 表格），超长截断。"""
from __future__ import annotations

from pathlib import Path

from docx import Document

MAX_CHARS = 20_000


class ScriptDocError(Exception):
    pass


def extract_text(path: Path) -> str:
    try:
        doc = Document(str(path))
    except Exception as e:
        raise ScriptDocError(f"无法读取 Word 文档：{e}")
    parts: list[str] = []
    for p in doc.paragraphs:
        t = p.text.strip()
        if t:
            parts.append(t)
    for table in doc.tables:  # 人物表/信息表常在表格里
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells if c.text.strip()]
            if cells:
                parts.append(" | ".join(cells))
    text = "\n".join(parts).strip()
    if not text:
        raise ScriptDocError("文档里没有读到文字内容")
    if len(text) > MAX_CHARS:
        text = text[:MAX_CHARS] + "\n……（剧本过长，已截断）"
    return text
```

`main.py` 加接口（UploadFile 保存到临时文件再解析；LLM 失败返回 502；filename 后缀校验 .docx）：

```python
from fastapi import UploadFile, File, Form
import tempfile

@app.post("/api/script")
async def script_to_posters(file: UploadFile = File(...), model: str = Form(default=None)):
    if not (file.filename or "").lower().endswith(".docx"):
        raise HTTPException(400, "只支持 .docx 格式的 Word 文档")
    from . import scriptdoc
    try:
        with tempfile.NamedTemporaryFile(suffix=".docx", delete=False) as tmp:
            tmp.write(await file.read())
            tmp_path = Path(tmp.name)
        text = scriptdoc.extract_text(tmp_path)
    except scriptdoc.ScriptDocError as e:
        raise HTTPException(400, str(e))
    try:
        return await promptgen.generate_poster_schemes(text, model)
    except promptgen.PromptGenError as e:
        raise HTTPException(502, str(e))
```

- [ ] **Step 4: 跑测试确认通过 + 接口冒烟**

Run: `uv run --with pytest python -m pytest tests/test_scriptdoc.py -v`（3 passed）
再用 TestClient 冒烟（monkeypatch generate_poster_schemes 返回假数据，POST 一个构造的 docx，断言 200 与 schemes 结构）。

- [ ] **Step 5: 验证点**——重启服务，接口 405/400 路径正确（`curl -X POST /api/script` 无文件应 422）。

---

### Task 4: 生成管线支持 kind=poster

**Files:**
- Modify: `app/main.py`（GenReq + /api/generate）
- Test: `tests/test_generate_kind.py`

**Interfaces:**
- Consumes: Task 1 的 kind 参数
- Produces: `POST /api/generate` 接受 `kind: "character" | "poster"`（默认 character）；/api/generations 每项带 `kind`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_generate_kind.py
from fastapi.testclient import TestClient

def test_generate_accepts_poster_kind(tmp_path, monkeypatch):
    from app import store, main
    monkeypatch.setattr(store, "_con", None)
    monkeypatch.setattr(store.config, "root", lambda: tmp_path)
    monkeypatch.setattr(main.browser, "is_configured", lambda: True)
    client = TestClient(main.app)
    r = client.post("/api/generate", json={"prompts": ["海报提示词"], "character_name": "海报", "kind": "poster"})
    assert r.status_code == 200, r.text
    items = client.get("/api/generations").json()["items"]
    assert items[0]["kind"] == "poster"
    r2 = client.post("/api/generate", json={"prompts": ["x"], "character_name": "jj", "kind": "bogus"})
    assert r2.status_code == 400
```

- [ ] **Step 2: 跑测试确认失败**（kind 字段被忽略 → items[0]["kind"] == "character" 断言失败；bogus 不报错）

- [ ] **Step 3: 实现**——`GenReq` 加 `kind: str = "character"`；generate() 里校验 `req.kind not in ("character", "poster") → 400`，并 `store.create_generation(..., kind=req.kind)`。

- [ ] **Step 4: 跑测试确认通过**（2 个断言全过）

- [ ] **Step 5: 验证点**——重启服务，现有角色图生成（不带 kind）不受影响。

---

### Task 5: 剪映驱动（一期半自动）+ 复审后端

**Files:**
- Create: `app/capcut_driver.py`
- Modify: `app/main.py`（review 四接口 + 静态目录挂载）
- Test: `tests/test_review.py`

**Interfaces:**
- Consumes: Task 1 的 review CRUD
- Produces:
  - `capcut_driver.is_installed() -> bool`
  - `capcut_driver.send_to_expand(pending_dir: Path) -> bool`（打开剪映 + open 待扩图文件夹；剪映未装则只开文件夹，返回 False）
  - `POST /api/review/send` `{image_paths: [...], gen_id: str}` → 复制进 review_pending/ + 建记录 + 调驱动 → `{sent: n, capcut: bool}`
  - `GET /api/review/list` → `{pending: [{id, file_name, url, src_path}], done: [{file_name, url, mtime}]}`
  - `POST /api/review/approve` `{file_name, title, dest_root}` → review_done 里的图平铺复制到保存位置 + saved 记录 + 同名 pending 记录置 approved → `{saved_path}`
  - `POST /api/review/reject` `{id}` → 撤回报审：删 review_pending 文件 + 记录
  - 静态挂载：`/review_pending`、`/review_done`
  - 目录常量：`review_pending/`、`review_done/`（`config.paths` 可覆盖）

- [ ] **Step 1: 写失败测试**

```python
# tests/test_review.py
from pathlib import Path
from fastapi.testclient import TestClient

def _setup(tmp_path, monkeypatch):
    from app import store, main, config
    monkeypatch.setattr(store, "_con", None)
    monkeypatch.setattr(config, "root", lambda: tmp_path)
    monkeypatch.setattr(config, "resolve_path", lambda p: tmp_path / p)
    monkeypatch.setattr(main.config, "resolve_path", lambda p: tmp_path / p)
    # 造一张假候选图
    cand = tmp_path / "candidates" / "g1"
    cand.mkdir(parents=True)
    (cand / "p1_01.png").write_bytes(b"fakepng")
    return main, TestClient(main.app)

def test_send_list_approve_reject(tmp_path, monkeypatch):
    main, client = _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(main.capcut_driver, "send_to_expand", lambda d: False)

    r = client.post("/api/review/send", json={"image_paths": ["candidates/g1/p1_01.png"], "gen_id": "g1"})
    assert r.status_code == 200 and r.json()["sent"] == 1
    assert (tmp_path / "review_pending" / "p1_01.png").exists()

    lst = client.get("/api/review/list").json()
    assert len(lst["pending"]) == 1 and lst["pending"][0]["file_name"] == "p1_01.png"

    # 模拟剪映导出：往 review_done 丢一张扩后图
    (tmp_path / "review_done").mkdir(exist_ok=True)
    (tmp_path / "review_done" / "p1_01_expanded.png").write_bytes(b"big")
    lst = client.get("/api/review/list").json()
    assert any(d["file_name"] == "p1_01_expanded.png" for d in lst["done"])

    r = client.post("/api/review/approve", json={"file_name": "p1_01_expanded.png", "title": "ECHOES", "dest_root": "default"})
    assert r.status_code == 200
    assert (tmp_path / "saved_images" / "p1_01_expanded.png").exists()

    r = client.post("/api/review/reject", json={"id": lst["pending"][0]["id"]})
    assert r.status_code == 200
    assert not (tmp_path / "review_pending" / "p1_01.png").exists()
```

- [ ] **Step 2: 跑测试确认失败**

- [ ] **Step 3: 实现**

`app/capcut_driver.py`：

```python
"""剪映专业版驱动（一期：半自动兜底）。

二期（剪映装好后联调）：用 macOS AX 自动完成「放进时间轴 → 画面-基础-AI扩展 → 导出到 review_done/」，
接口签名保持不变。
"""
from __future__ import annotations

import subprocess
from pathlib import Path

APP_CANDIDATES = ("剪映专业版", "剪映", "CapCut")


def is_installed() -> bool:
    return any((Path("/Applications") / f"{n}.app").exists() for n in APP_CANDIDATES)


def send_to_expand(pending_dir: Path) -> bool:
    """打开待扩图文件夹；装了剪映则顺带把剪映拉起。返回剪映是否在。"""
    subprocess.run(["open", str(pending_dir)], check=False)
    if is_installed():
        for n in APP_CANDIDATES:
            if (Path("/Applications") / f"{n}.app").exists():
                subprocess.run(["open", "-a", n], check=False)
                break
        return True
    return False
```

`main.py`：
- 顶部 `from . import capcut_driver`；`StaticFiles` 挂载两个 review 目录（先 `mkdir(exist_ok=True)` 再 mount）
- 四个接口按上面签名实现；send 时复用 save 的越界防护（只允许 candidates/ 内文件）；approve 复用 `_resolve_dest_root` 平铺复制 + `store.add_saved(title, ...)`；list 扫 `review_done` 目录里的图片文件（.png/.jpg/.jpeg/.webp）

- [ ] **Step 4: 跑测试确认通过**

- [ ] **Step 5: 验证点**——重启服务，`curl http://127.0.0.1:8321/api/review/list` 返回空结构。

---

### Task 6: 前端——模式切换 + 剧本上传 + 海报徽章

**Files:**
- Modify: `app/static/index.html`

**Interfaces:**
- Consumes: Task 3、4 的接口
- Produces:
  - `state.mode`：`"character" | "poster"`（默认 character，localStorage 记忆）
  - 海报模式下 `submitGenerate` 带 `kind: "poster"`
  - 画廊标题行：kind=poster 显示「🎬 海报」徽章

- [ ] **Step 1: 模式切换 UI**——h1 下方加：

```html
<div class="row" id="modeTabs" style="margin-bottom:14px">
  <button class="ghost" id="tabChar">👤 角色图</button>
  <button class="ghost" id="tabPoster">🎬 剧本海报</button>
</div>
```

CSS：`.mode-on { background:var(--pri); color:#fff; border-color:var(--pri); }`

JS：

```js
function setMode(m) {
  state.mode = m;
  localStorage.setItem('mode', m);
  $('#tabChar').classList.toggle('mode-on', m === 'character');
  $('#tabPoster').classList.toggle('mode-on', m === 'poster');
  $('#charCard').classList.toggle('hidden', m !== 'character');
  $('#posterCard').classList.toggle('hidden', m !== 'poster');
  $('#reviewCard').classList.toggle('hidden', m !== 'poster');
  $('#charLabel').textContent = m === 'poster' ? '用哪张资产卡生成：' : '用哪张角色卡生成：';
  if (m === 'poster') refreshReview();
}
$('#tabChar').onclick = () => setMode('character');
$('#tabPoster').onclick = () => setMode('poster');
setMode(localStorage.getItem('mode') || 'character');
```

给现有 ① 卡片加 `id="charCard"`；② 里「用哪张角色卡生成」label 加 `id="charLabel"`。

- [ ] **Step 2: 海报上传卡片**——① 的兄弟卡片：

```html
<div class="card hidden" id="posterCard">
  <h2>① 上传剧本 <span class="sub" style="margin:0">.docx 格式，AI 分析后出 8 组海报提示词（6 亮调 2 暗调）</span></h2>
  <div class="row">
    <input type="file" id="scriptFile" accept=".docx" style="width:320px">
    <button id="btnScript">📄 分析剧本并生成 8 组海报提示词</button>
    <span class="sub" id="scriptMsg"></span>
  </div>
</div>
```

JS：

```js
$('#btnScript').onclick = async () => {
  const f = $('#scriptFile').files[0];
  if (!f) return alert('请先选择 .docx 剧本文件');
  const btn = $('#btnScript'); btn.disabled = true;
  $('#scriptMsg').textContent = 'AI 正在通读剧本并设计 8 组海报方案…（约 60-90 秒）';
  try {
    const fd = new FormData();
    fd.append('file', f);
    fd.append('model', $('#modelSel').value);
    const r = await (await fetch('/api/script', {method: 'POST', body: fd})).json();
    if (r.detail) throw new Error(r.detail);
    state.schemes = r.schemes;
    state.posterTitle = r.character || '';
    renderSchemes(r);
    $('#scriptMsg').textContent = `已生成 ${r.schemes.length} 组方案｜剧名：${r.character}`;
  } catch (e) { $('#scriptMsg').textContent = '❌ ' + e.message; }
  btn.disabled = false;
};
```

- [ ] **Step 3: 调性徽章 + 海报徽章**——`renderSchemes` 的 h3 里按名字含「亮调/暗调」渲染 `<span class="badge tone-light">☀️ 亮调</span>` / `<span class="badge tone-dark">🌙 暗调</span>`（CSS：亮 #fff3e0/#e8a13a，暗 #2b2f36/#e5e7eb）；`refreshGallery` 的 gen-head 里 `g.kind === 'poster'` 时加 `<span class="badge poster">🎬 海报</span>`（CSS：#eceffd/var(--pri)）。

- [ ] **Step 4: submitGenerate 带 kind**——`body: JSON.stringify({prompts, character_name, kind: state.mode})`；genMsg 文案按模式区分。

- [ ] **Step 5: 验证点**——`bun -e` 检查 JS 语法；刷新页面：两个 Tab 切换正常、模式记忆、角色图流程回归正常。

---

### Task 7: 前端——复审区（送去扩图 / 列表 / 安全区叠加 / 采用 / 撤回）

**Files:**
- Modify: `app/static/index.html`

**Interfaces:**
- Consumes: Task 5 的四接口
- Produces:
  - ③ 操作行在海报模式多一个「🔧 送去扩图」按钮（把 picked 图 POST /api/review/send）
  - ④ 复审卡片 `#reviewCard`：待扩区 + 已扩区 + 「🛡 安全区」开关
  - `refreshReview()`：渲染两区；done 图支持安全区叠加/✅ 采用/🗑 删除；pending 支持 ↩️ 撤回

- [ ] **Step 1: 复审卡片 HTML**（放在 ③ 画廊卡片之后、状态栏之前）：

```html
<div class="card hidden" id="reviewCard">
  <h2>④ 复审 · AI 扩图 <span class="sub" style="margin:0">剪映：画面 → 基础 → AI扩展，导出到「已扩图」文件夹</span>
    <button class="ghost" id="btnSafezone" style="margin-left:auto">🛡 安全区：关</button>
    <button class="ghost" id="btnOpenPending">📂 待扩图文件夹</button>
    <button class="ghost" id="btnOpenDone">📂 已扩图文件夹</button>
  </h2>
  <h3 style="font-size:13px;margin:8px 0 6px">待扩图（已送往剪映）</h3>
  <div class="thumbs" id="reviewPending"></div>
  <h3 style="font-size:13px;margin:14px 0 6px">已扩图（剪映导出，自动扫描）</h3>
  <div class="thumbs" id="reviewDone"></div>
</div>
```

打开文件夹按钮需要后端小接口：`POST /api/open-folder {which: "pending"|"done"}`（subprocess open，Task 5 顺带加上）。

- [ ] **Step 2: 安全区叠加 CSS**（包在 `.rv` 容器里，容器宽高=缩略图 126×224）：

```css
.rv { position:relative; width:126px; }
.rv img { width:126px; height:224px; object-fit:cover; border-radius:8px; border:2px solid var(--line); display:block; }
.safezone { position:absolute; inset:0; pointer-events:none; display:none; }
.safezone.on { display:block; }
.sz { position:absolute; left:0; right:0; }
.sz.top    { top:0;     height:12.6%; background:rgba(229,72,77,.45); }
.sz.warnT  { top:12.6%; height:2.8%;  background:rgba(232,161,58,.55); }
.sz.safe   { top:15.4%; height:62.6%; outline:2px dashed rgba(34,160,107,.9); outline-offset:-2px; }
.sz.warnB  { top:78%;   height:9%;    background:rgba(232,161,58,.55); }
.sz.bottom { top:87%;   height:13%;   background:rgba(229,72,77,.45); }
```

- [ ] **Step 3: refreshReview + 按钮逻辑**：

```js
const szState = { on: false };
$('#btnSafezone').onclick = () => {
  szState.on = !szState.on;
  $('#btnSafezone').textContent = `🛡 安全区：${szState.on ? '开' : '关'}`;
  document.querySelectorAll('.safezone').forEach(el => el.classList.toggle('on', szState.on));
};
const SZ_HTML = '<div class="safezone"><div class="sz top"></div><div class="sz warnT"></div><div class="sz safe"></div><div class="sz warnB"></div><div class="sz bottom"></div></div>';

async function refreshReview() {
  if (state.mode !== 'poster') return;
  const r = await (await fetch('/api/review/list')).json();
  $('#reviewPending').innerHTML = (r.pending || []).map(p => `
    <div class="rv">
      <img src="/${p.src_path}" loading="lazy" onclick="openBox('/${p.src_path}')">
      <button class="ghost qj" onclick="reviewReject('${p.id}')">↩️ 撤回</button>
    </div>`).join('') || '<div class="sub">空</div>';
  $('#reviewDone').innerHTML = (r.done || []).map(d => `
    <div class="rv">
      <img src="${d.url}" loading="lazy" onclick="openBox('${d.url}')">${SZ_HTML}
      <button class="ok qj" onclick="reviewApprove('${d.file_name}')">✅ 采用</button>
      <button class="ghost qj" onclick="reviewDeleteDone('${d.file_name}')">🗑</button>
    </div>`).join('') || '<div class="sub">空（剪映导出后自动出现在这里）</div>';
  document.querySelectorAll('.safezone').forEach(el => el.classList.toggle('on', szState.on));
}
```

送去扩图按钮（③ 操作行，`id="btnToReview"`，海报模式才显示）：

```js
$('#btnToReview').onclick = async () => {
  if (!state.picked.size) return alert('先在画廊勾选要扩图的候选海报');
  const r = await (await fetch('/api/review/send', {method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({image_paths: [...state.picked], gen_id: state.lastGen || ''})})).json();
  if (r.detail) return alert(r.detail);
  alert(`已把 ${r.sent} 张原图放入待扩图文件夹${r.capcut ? '，剪映已打开' : '（未检测到剪映专业版，请先安装）'}。\n在剪映里：拖入时间轴 → 画面 → 基础 → AI扩展，导出到「已扩图」文件夹。`);
  state.picked.clear(); updateActionBar(); refreshReview();
};
```

采用/撤回/删除：

```js
window.reviewApprove = async fn => {
  const dest_root = $('#saveDirSel').value === '__browse__' ? 'default' : $('#saveDirSel').value;
  const r = await (await fetch('/api/review/approve', {method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({file_name: fn, title: state.posterTitle || '海报', dest_root})})).json();
  if (r.detail) return alert(r.detail);
  $('#saveMsg').textContent = `✅ 扩后图已入成品库：${r.saved_path}`;
  refreshReview();
};
window.reviewReject = async id => { await fetch('/api/review/reject', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({id})}); refreshReview(); };
window.reviewDeleteDone = async fn => { if (confirm(`删除已扩图 ${fn}？`)) { await fetch(`/api/review/delete-done`, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({file_name: fn})}); refreshReview(); } };
```

（对应后端 `POST /api/review/delete-done` 删 review_done 里的文件——Task 5 顺带加上。）

轮询：现有 5s setInterval 里加 `refreshReview()`（内部有 mode 判断，角色模式零开销）。

- [ ] **Step 4: 验证点**——JS 语法检查；海报模式下③出现送去扩图按钮、④复审卡片出现；安全区开关切换正常（可用 gallery 里已有的 poster 记录造数据实测）。

---

### Task 8: 端到端验证 + 版本 1.2.0 + 文档与安装包

**Files:**
- Modify: `VERSION`、`package.json`、`README.md`、`首次使用说明.txt`

- [ ] **Step 1**: 全量测试 `uv run --with pytest python -m pytest tests/ -v` 全绿
- [ ] **Step 2**: 重启服务，真实端到端：上传一个测试 docx（构造 2 页剧本）→ 出 8 组方案（核对 6 亮 2 暗、标题=英文名）→ 勾选 2 组用「海报」卡生成 → 8 张候选海报入库、进度条/终止正常
- [ ] **Step 3**: 复审链路实测：送 1 张去扩图 → 手动往 review_done 放一张图模拟剪映导出 → 页面出现 → 安全区叠加检查 → 采用 → 成品库出现
- [ ] **Step 4**: 版本三处升 1.2.0（VERSION/package.json/README），README 加功能 2 说明，首次使用说明.txt 加海报模式一节
- [ ] **Step 5**: 管家选 5 打新包 `jdd-studio-1.2.0.zip`（旧包清理），验证包内容
- [ ] **Step 6**: 更新外置记忆（FACT + JOURNAL）

---

## Self-Review 记录

- spec 覆盖：双模式①②③④ ✓（T6/T7）、skill 模板 ✓（T2）、安全区两处 ✓（T2 提示词 / T7 叠加）、剪映一期 ✓（T5）、版本规则 ✓（T8）、测试 ✓（各 Task）
- 类型一致性：review 接口字段名（file_name/src_path/url/id）在 T5 接口与 T7 前端间一致；parse_schemes 返回结构两种模式同构
- 一期配对降级说明：扩后图与原图的自动精确配对依赖驱动控制导出文件名，**二期**实现；一期 done 区直接列出扩后图，人工目检对比（杰已知情：一期半自动兜底）
