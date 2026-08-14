# 参考图自动识别 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 海报模式上传参考图后，视觉模型对照剧本角色表自动识别角色名（附置信度），免手打。

**Architecture:** 新增 `app/roster.py`（剧本→角色表，缓存 refs/roster.json）与 `app/vision.py`（缩图+视觉匹配，固定 gemini-3.6-flash）；`POST /api/refs` 上传即识别（无角色表则 pending，/api/script 成功后批量补识别）；前端参考图卡片加置信度徽章+改名+重识别。

**Tech Stack:** FastAPI + OpenAI SDK（AIOnly 网关）+ Pillow（新增）+ pytest（TestClient，LLM 全部打桩）

**Spec:** `docs/superpowers/specs/2026-08-12-ref-auto-match-design.md`

## Global Constraints

- 项目**不是 git 仓库**：所有「commit」步骤改为「跑全量测试 `uv run --with pytest python -m pytest tests/ -v` 确认全绿」
- 视觉识别**固定用 gemini-3.6-flash**，不受界面模型下拉影响
- 送视觉模型前**必须缩图**：最长边 768px、JPEG q80（原图直送网关会 400）
- refs.json 条目新字段（confidence/reason/pending）全部可选，旧数据必须兼容
- 识别/角色表抽取失败**永不阻塞**上传与海报生成
- 测试命令统一：`cd ~/jdd-studio && uv run --with pytest python -m pytest tests/ -v`
- 版本收尾：1.3.1 → 1.4.0，同步 VERSION / package.json / pyproject.toml / README 顶部

---

### Task 1: `app/roster.py` — 角色表抽取与缓存

**Files:**
- Create: `app/roster.py`
- Test: `tests/test_roster.py`

**Interfaces:**
- Produces（后续任务依赖这些签名）:
  - `parse_json_loose(text: str) -> list | None` — 去 ```json 围栏、截取首个 `[` 到末个 `]` 解析；失败返回 None
  - `async def extract_roster(script_text: str, model: str | None = None, caller=None) -> list[dict]` — caller 签名 `async (messages: list[dict], model: str) -> str`（返回 LLM 原始文本）；LLM/解析失败返回 `[]`，不抛异常
  - `load_roster() -> list[dict]` — 读 `refs/roster.json`，不存在/损坏返回 `[]`
  - `save_roster(roster: list[dict]) -> None`
- Consumes: `app.config.llm_config(model)` → `{"api_key","base_url","model",...}`；`app.config.resolve_path(p)`

- [ ] **Step 1: 写失败测试** `tests/test_roster.py`

```python
"""角色表抽取与缓存测试（LLM 打桩）。"""
from __future__ import annotations


def _isolate(tmp_path, monkeypatch):
    from app import config
    monkeypatch.setattr(config, "resolve_path", lambda p: tmp_path / p)


def test_save_load_roundtrip(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    from app import roster
    data = [{"name": "Evelyn Hart", "gender": "Female", "age": 22,
             "identity": "失明千金", "appearance": "金棕长发"}]
    roster.save_roster(data)
    assert roster.load_roster() == data


def test_load_missing_returns_empty(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    from app import roster
    assert roster.load_roster() == []


def test_parse_json_loose_plain_and_fenced():
    from app import roster
    assert roster.parse_json_loose('[{"a":1}]') == [{"a": 1}]
    assert roster.parse_json_loose('```json\n[{"a":1}]\n```') == [{"a": 1}]
    assert roster.parse_json_loose('前言 [{"a":1}] 后记') == [{"a": 1}]
    assert roster.parse_json_loose('不是JSON') is None


def test_extract_roster_with_fake_caller(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    import asyncio
    from app import roster

    async def fake_caller(messages, model):
        # 提示词里必须带剧本全文
        assert "杰克" in messages[-1]["content"]
        return '```json\n[{"name":"杰克","gender":"Male","age":35,"identity":"刑警","appearance":"冷峻"}]\n```'

    result = asyncio.run(roster.extract_roster("杰克：男，35岁", caller=fake_caller))
    assert result[0]["name"] == "杰克"


def test_extract_roster_llm_failure_returns_empty(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    import asyncio
    from app import roster

    async def bad_caller(messages, model):
        raise RuntimeError("网关炸了")

    assert asyncio.run(roster.extract_roster("任何剧本", caller=bad_caller)) == []
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd ~/jdd-studio && uv run --with pytest python -m pytest tests/test_roster.py -v`
Expected: FAIL `ModuleNotFoundError: No module named 'app.roster'`

- [ ] **Step 3: 实现** `app/roster.py`

```python
"""剧本 → 角色表（name/gender/age/identity/appearance），缓存到 refs/roster.json。"""
from __future__ import annotations

import json
import re

from openai import AsyncOpenAI

from . import config

ROSTER_PROMPT = """你是短剧选角导演。通读下面剧本，抽取【有名有姓的主要角色】名单（最多8个，按戏份排序）。
每个角色输出：name（英文名）、gender、age（约数）、identity（一句话身份）、appearance（外貌关键特征：发型发色/气质/常见着装，用于和演员定妆照比对）。
只输出 JSON 数组，不要任何其他文字。

剧本：
%s"""

# 视觉识别固定模型（实测 3 次全对且最便宜），不受界面模型下拉影响
VISION_MODEL = "gemini-3.6-flash"


def _roster_file():
    d = config.resolve_path(config.get("paths", "refs_dir", default="refs"))
    d.mkdir(parents=True, exist_ok=True)
    return d / "roster.json"


def parse_json_loose(text: str) -> list | None:
    """容错解析 JSON 数组：去 ```json 围栏、截取首个 [ 到末个 ]。失败返回 None。"""
    m = re.search(r"\[.*\]", text or "", re.S)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except Exception:
        return None
    return data if isinstance(data, list) else None


async def _default_caller(messages: list[dict], model: str) -> str:
    cfg = config.llm_config(model)
    client = AsyncOpenAI(base_url=cfg["base_url"], api_key=cfg["api_key"], timeout=120)
    resp = await client.chat.completions.create(model=cfg["model"], messages=messages, temperature=0.3)
    return resp.choices[0].message.content or ""


async def extract_roster(script_text: str, model: str | None = None, caller=None) -> list[dict]:
    """LLM 抽角色表。任何失败返回 []（不阻塞海报生成）。"""
    call = caller or _default_caller
    try:
        raw = await call([{"role": "user", "content": ROSTER_PROMPT % script_text}], model or VISION_MODEL)
        return parse_json_loose(raw) or []
    except Exception:
        return []


def load_roster() -> list[dict]:
    try:
        return json.loads(_roster_file().read_text(encoding="utf-8"))
    except Exception:
        return []


def save_roster(roster: list[dict]) -> None:
    _roster_file().write_text(json.dumps(roster, ensure_ascii=False, indent=2), encoding="utf-8")
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run --with pytest python -m pytest tests/test_roster.py -v`
Expected: 5 PASS

- [ ] **Step 5: 全量测试确认无回归**

Run: `uv run --with pytest python -m pytest tests/ -v`
Expected: 旧 20 个 + 新 5 个全绿

---

### Task 2: `app/vision.py` — 缩图 + 视觉识别

**Files:**
- Create: `app/vision.py`
- Test: `tests/test_vision.py`
- Modify: `pyproject.toml`（dependencies 加 `"pillow>=10.0"`）

**Interfaces:**
- Consumes: `roster.VISION_MODEL`、`roster.parse_json_loose`、`config.llm_config`
- Produces:
  - `shrink_b64(image_path: Path, max_px: int = 768, quality: int = 80) -> str` — JPEG base64
  - `async def recognize(image_path: Path, roster_list: list[dict], caller=None) -> dict` — 返回 `{"name": str|None, "confidence": float, "reason": str}`；roster 为空直接返回 `{"name": None, "confidence": 0.0, "reason": "无角色表"}` 不调 LLM；LLM 异常**抛出**（由调用方决定降级）
  - `async def recognize_batch(image_paths: list[Path], roster_list: list[dict], caller=None) -> list[dict]` — 一次请求多张图，返回与输入等长的上同结构列表

- [ ] **Step 1: 写失败测试** `tests/test_vision.py`

```python
"""视觉识别测试（LLM 打桩，PIL 缩图走真实代码）。"""
from __future__ import annotations

import asyncio
import base64
import io

ROSTER = [
    {"name": "Evelyn Hart", "gender": "Female", "age": 22, "identity": "失明千金", "appearance": "金棕长发"},
    {"name": "Richard Hart", "gender": "Male", "age": 53, "identity": "父亲", "appearance": "银灰发三件套"},
]


def _photo(tmp_path, size=(2000, 1000), name="p.png"):
    from PIL import Image
    p = tmp_path / name
    Image.new("RGB", size, (120, 30, 30)).save(p)
    return p


def test_shrink_b64(tmp_path):
    from app import vision
    b64 = vision.shrink_b64(_photo(tmp_path))
    from PIL import Image
    im = Image.open(io.BytesIO(base64.b64decode(b64)))
    assert im.format == "JPEG" and max(im.size) <= 768


def test_recognize_empty_roster_skips_llm(tmp_path):
    from app import vision
    r = asyncio.run(vision.recognize(_photo(tmp_path), []))
    assert r == {"name": None, "confidence": 0.0, "reason": "无角色表"}


def test_recognize_match(tmp_path):
    from app import vision

    async def fake_caller(messages, model):
        assert model == "gemini-3.6-flash"
        content = messages[-1]["content"]
        assert any(p.get("type") == "image_url" for p in content)  # 图真的带上了
        return '[{"image":1,"match":"Evelyn Hart","confidence":0.95,"reason":"金棕长发年轻女"}]'

    r = asyncio.run(vision.recognize(_photo(tmp_path), ROSTER, caller=fake_caller))
    assert r["name"] == "Evelyn Hart" and r["confidence"] == 0.95


def test_recognize_bad_json_returns_null(tmp_path):
    from app import vision

    async def garbage(messages, model):
        return "我看不懂"

    r = asyncio.run(vision.recognize(_photo(tmp_path), ROSTER, caller=garbage))
    assert r["name"] is None and r["confidence"] == 0.0


def test_recognize_batch_maps_by_index(tmp_path):
    from app import vision
    paths = [_photo(tmp_path, name="a.png"), _photo(tmp_path, name="b.png")]

    async def fake_caller(messages, model):
        return ('[{"image":2,"match":"Richard Hart","confidence":0.9,"reason":"银灰发"},'
                '{"image":1,"match":"Evelyn Hart","confidence":0.8,"reason":"年轻女"}]')

    rs = asyncio.run(vision.recognize_batch(paths, ROSTER, caller=fake_caller))
    assert [r["name"] for r in rs] == ["Evelyn Hart", "Richard Hart"]  # 按输入顺序，不按返回顺序
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run --with pytest python -m pytest tests/test_vision.py -v`
Expected: FAIL `ModuleNotFoundError: No module named 'app.vision'`（若 pillow 未装先补 pyproject 依赖）

- [ ] **Step 3: pyproject 加依赖 + 实现** `app/vision.py`

pyproject.toml dependencies 追加一行 `"pillow>=10.0",`，然后 `cd ~/jdd-studio && uv sync`。

```python
"""参考图视觉识别：缩图 → 视觉模型对照角色表认人。固定 gemini-3.6-flash。"""
from __future__ import annotations

import base64
import io
from pathlib import Path

from openai import AsyncOpenAI

from . import config, roster as roster_mod

MATCH_ONE_PROMPT = """这是某短剧的一张演员定妆参考图。请对照剧本角色表，判断图中最可能是哪个角色。
依据：性别、年龄段、气质、着装与角色身份的吻合度。
只输出 JSON：[{"image":1,"match":"角色name或null","confidence":0.0~1.0,"reason":"20字内"}]，不要其他文字。
图中人物与任何角色都不像时 match 填 null。

角色表：
%s"""

MATCH_BATCH_PROMPT = """下面是某短剧的 %d 张演员定妆参考图（按发送顺序编号1~%d），以及剧本角色表。
请把每张图匹配到最可能的角色。依据：性别、年龄段、气质、着装与角色身份的吻合度。
每张图输出：image（编号）、match（角色name，不像任何角色填null）、confidence（0~1）、reason（20字内）。
只输出 JSON 数组，不要其他文字。

角色表：
%s"""


def shrink_b64(image_path: Path, max_px: int = 768, quality: int = 80) -> str:
    """必须缩图：原图（~3MB/张）直送网关会 400。768px JPEG q80 ≈ 34KB。"""
    from PIL import Image
    im = Image.open(image_path).convert("RGB")
    im.thumbnail((max_px, max_px))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=quality)
    return base64.b64encode(buf.getvalue()).decode()


async def _default_caller(messages: list[dict], model: str) -> str:
    cfg = config.llm_config(model)
    client = AsyncOpenAI(base_url=cfg["base_url"], api_key=cfg["api_key"], timeout=120)
    resp = await client.chat.completions.create(model=cfg["model"], messages=messages, temperature=0.3)
    return resp.choices[0].message.content or ""


def _null(reason: str = "") -> dict:
    return {"name": None, "confidence": 0.0, "reason": reason}


def _to_result(item: dict) -> dict:
    """模型输出项 → 统一结构；match 不在角色表里也照收（前端可人工判断）。"""
    return {
        "name": item.get("match") or None,
        "confidence": float(item.get("confidence") or 0),
        "reason": str(item.get("reason") or ""),
    }


async def recognize(image_path: Path, roster_list: list[dict], caller=None) -> dict:
    if not roster_list:
        return _null("无角色表")
    call = caller or _default_caller
    import json
    prompt = MATCH_ONE_PROMPT % json.dumps(roster_list, ensure_ascii=False)
    content = [
        {"type": "text", "text": prompt},
        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{shrink_b64(image_path)}"}},
    ]
    raw = await call([{"role": "user", "content": content}], roster_mod.VISION_MODEL)
    items = roster_mod.parse_json_loose(raw)
    if not items:
        return _null("模型输出无法解析")
    return _to_result(items[0])


async def recognize_batch(image_paths: list[Path], roster_list: list[dict], caller=None) -> list[dict]:
    if not roster_list or not image_paths:
        return [_null("无角色表") for _ in image_paths]
    import json
    call = caller or _default_caller
    n = len(image_paths)
    prompt = MATCH_BATCH_PROMPT % (n, n, json.dumps(roster_list, ensure_ascii=False))
    content = [{"type": "text", "text": prompt}]
    for p in image_paths:
        content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{shrink_b64(p)}"}})
    raw = await call([{"role": "user", "content": content}], roster_mod.VISION_MODEL)
    items = roster_mod.parse_json_loose(raw) or []
    by_idx = {int(i.get("image", 0)): i for i in items if isinstance(i, dict)}
    return [_to_result(by_idx[k]) if k in by_idx else _null("模型未返回该图") for k in range(1, n + 1)]
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run --with pytest python -m pytest tests/test_vision.py -v`
Expected: 5 PASS

- [ ] **Step 5: 全量测试**

Run: `uv run --with pytest python -m pytest tests/ -v`
Expected: 全绿

---

### Task 3: `/api/refs` 上传即识别 + rename + recognize 接口

**Files:**
- Modify: `app/main.py`（`/api/refs` 区块，约 180–240 行）
- Test: `tests/test_refs.py`（追加）

**Interfaces:**
- Consumes: `vision.recognize(image_path, roster_list)`（Task 2）；`roster.load_roster()`（Task 1）
- Produces:
  - `POST /api/refs`：表单 `name` 改选填。refs.json 条目结构 `{id, name, file, confidence?, reason?, pending?}`
  - `POST /api/refs/rename`：body `{"id": str, "name": str}` → `{"ok": true}`；改名同时删除该条 `pending`
  - `POST /api/refs/recognize`：body `{"id": str}` → `{"id", "name", "confidence", "reason"}`；无角色表 → 400「还没有角色表，请先上传剧本」
- 行为规约（spec）：手填 name 则跳过识别；name 空 + 有角色表 → 同步识别填名；name 空 + 无角色表 → `pending: true`；识别抛异常 → 照常上传成功 + `pending: true`

- [ ] **Step 1: 追加失败测试**（加到 `tests/test_refs.py` 尾部，复用文件里的 `_png()` 和 `_setup()`）

```python
def _fake_recog(name="Evelyn Hart", conf=0.95):
    async def fake(image_path, roster_list, caller=None):
        return {"name": name, "confidence": conf, "reason": "测试"}
    return fake


def _seed_roster(tmp_path):
    import json
    (tmp_path / "refs").mkdir(parents=True, exist_ok=True)
    (tmp_path / "refs" / "roster.json").write_text(
        json.dumps([{"name": "Evelyn Hart", "gender": "Female", "age": 22,
                     "identity": "失明千金", "appearance": "金棕长发"}], ensure_ascii=False),
        encoding="utf-8")


def test_upload_auto_recognizes_when_roster_ready(tmp_path, monkeypatch):
    main, client = _setup(tmp_path, monkeypatch)
    from app import vision
    _seed_roster(tmp_path)
    monkeypatch.setattr(vision, "recognize", _fake_recog())

    r = client.post("/api/refs", files={"file": ("e.png", _png())}, data={"name": ""})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["name"] == "Evelyn Hart" and body["confidence"] == 0.95
    lst = client.get("/api/refs").json()["refs"]
    assert lst[0]["name"] == "Evelyn Hart" and "pending" not in lst[0]


def test_upload_pending_when_no_roster(tmp_path, monkeypatch):
    main, client = _setup(tmp_path, monkeypatch)
    r = client.post("/api/refs", files={"file": ("e.png", _png())}, data={"name": ""})
    assert r.status_code == 200
    assert r.json()["pending"] is True and r.json()["name"] == ""


def test_upload_manual_name_skips_recognition(tmp_path, monkeypatch):
    main, client = _setup(tmp_path, monkeypatch)
    from app import vision
    _seed_roster(tmp_path)
    monkeypatch.setattr(vision, "recognize", _fake_recog())  # 不应被调用也无所谓，结果以手填为准

    r = client.post("/api/refs", files={"file": ("e.png", _png())}, data={"name": "我手填的"})
    assert r.json()["name"] == "我手填的" and "confidence" not in r.json()


def test_upload_recognition_failure_marks_pending(tmp_path, monkeypatch):
    main, client = _setup(tmp_path, monkeypatch)
    from app import vision
    _seed_roster(tmp_path)

    async def boom(image_path, roster_list, caller=None):
        raise RuntimeError("LLM 超时")
    monkeypatch.setattr(vision, "recognize", boom)

    r = client.post("/api/refs", files={"file": ("e.png", _png())}, data={"name": ""})
    assert r.status_code == 200 and r.json()["pending"] is True


def test_rename_and_recognize_endpoints(tmp_path, monkeypatch):
    main, client = _setup(tmp_path, monkeypatch)
    from app import vision
    _seed_roster(tmp_path)
    monkeypatch.setattr(vision, "recognize", _fake_recog("Richard Hart", 0.9))

    r = client.post("/api/refs", files={"file": ("e.png", _png())}, data={"name": ""})
    rid = r.json()["id"]

    r = client.post("/api/refs/rename", json={"id": rid, "name": "手改名"})
    assert r.status_code == 200
    lst = client.get("/api/refs").json()["refs"]
    assert lst[0]["name"] == "手改名" and "pending" not in lst[0]

    r = client.post("/api/refs/recognize", json={"id": rid})
    assert r.status_code == 200 and r.json()["name"] == "Richard Hart"
    lst = client.get("/api/refs").json()["refs"]
    assert lst[0]["name"] == "Richard Hart" and lst[0]["confidence"] == 0.9


def test_recognize_without_roster_400(tmp_path, monkeypatch):
    main, client = _setup(tmp_path, monkeypatch)
    r = client.post("/api/refs", files={"file": ("e.png", _png())}, data={"name": "x"})
    rid = r.json()["id"]
    r = client.post("/api/refs/recognize", json={"id": rid})
    assert r.status_code == 400


def test_old_refs_json_compatible(tmp_path, monkeypatch):
    """旧格式条目（无 confidence/pending）照常列表。"""
    main, client = _setup(tmp_path, monkeypatch)
    import json
    (tmp_path / "refs").mkdir(parents=True, exist_ok=True)
    (tmp_path / "refs" / "refs.json").write_text(
        json.dumps([{"id": "old1", "name": "杰克", "file": "old1.png"}], ensure_ascii=False), encoding="utf-8")
    lst = client.get("/api/refs").json()["refs"]
    assert lst[0]["name"] == "杰克"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run --with pytest python -m pytest tests/test_refs.py -v`
Expected: 新 6 个 FAIL（404/字段缺失）

- [ ] **Step 3: 改 `app/main.py`**

3a. 文件顶部 import 区加：`from . import roster as roster_mod, vision`

3b. 替换 `add_ref` 整个函数：

```python
@app.post("/api/refs")
async def add_ref(file: UploadFile = File(...), name: str = Form(default="")):
    name = name.strip()
    ext = Path(file.filename or "x.png").suffix.lower()
    if ext not in _IMG_EXTS:
        raise HTTPException(400, "只支持 png/jpg/jpeg/webp 图片")
    refs = _load_refs()
    if len(refs) >= 10:
        raise HTTPException(400, "剧多多单个任务最多 10 张参考图，请先删除不需要的")
    rid = uuid.uuid4().hex[:8]
    fname = f"{rid}{ext}"
    fpath = _refs_dir() / fname
    fpath.write_bytes(await file.read())
    entry = {"id": rid, "name": name, "file": fname}
    if not name:
        # 没手填名字 → 视觉识别（有角色表立即识别，否则挂起等剧本）
        roster_list = roster_mod.load_roster()
        if roster_list:
            try:
                rec = await vision.recognize(fpath, roster_list)
                entry.update({"name": rec["name"] or "", "confidence": rec["confidence"],
                              "reason": rec["reason"]})
            except Exception:
                entry["pending"] = True  # 识别失败不阻塞上传
        else:
            entry["pending"] = True
    refs.append(entry)
    _store_refs(refs)
    return {**entry, "index": len(refs)}
```

3c. `delete_ref` 下方追加两个接口（请求模型用 pydantic，文件里已有 `RefDeleteReq` 同款写法可照抄）：

```python
class RefRenameReq(BaseModel):
    id: str
    name: str


class RefRecogReq(BaseModel):
    id: str


@app.post("/api/refs/rename")
def rename_ref(req: RefRenameReq):
    refs = _load_refs()
    for r in refs:
        if r["id"] == req.id:
            r["name"] = req.name.strip()
            r.pop("pending", None)
            _store_refs(refs)
            return {"ok": True}
    raise HTTPException(404, "参考图不存在")


@app.post("/api/refs/recognize")
async def recognize_ref(req: RefRecogReq):
    refs = _load_refs()
    entry = next((r for r in refs if r["id"] == req.id), None)
    if not entry:
        raise HTTPException(404, "参考图不存在")
    roster_list = roster_mod.load_roster()
    if not roster_list:
        raise HTTPException(400, "还没有角色表，请先上传剧本")
    try:
        rec = await vision.recognize(_refs_dir() / entry["file"], roster_list)
    except Exception as e:
        raise HTTPException(502, f"识别失败：{e}")
    entry.update({"name": rec["name"] or entry.get("name", ""), "confidence": rec["confidence"],
                  "reason": rec["reason"]})
    entry.pop("pending", None)
    _store_refs(refs)
    return {"id": entry["id"], "name": entry["name"], "confidence": entry["confidence"],
            "reason": entry["reason"]}
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run --with pytest python -m pytest tests/test_refs.py -v`
Expected: 全部 PASS（含旧 2 个）

- [ ] **Step 5: 全量测试**

Run: `uv run --with pytest python -m pytest tests/ -v`
Expected: 全绿

---

### Task 4: `/api/script` 前置角色表抽取 + 成功后补识别

**Files:**
- Modify: `app/main.py`（`script_to_posters`，约 374–395 行）
- Test: `tests/test_script_api.py`（追加）

**Interfaces:**
- Consumes: `roster_mod.extract_roster/save_roster/load_roster`（Task 1）、`vision.recognize_batch`（Task 2）
- Produces: `/api/script` 响应在原有 dict 上增加 `"roster_count": int`；pending 条目识别后写回 refs.json（删 `pending`、写 name/confidence/reason；识别出 name 为空的条目保留 pending）

- [ ] **Step 1: 追加失败测试**（加到 `tests/test_script_api.py` 尾部）

```python
def test_script_extracts_roster_and_fills_pending(tmp_path, monkeypatch):
    from app import store, main, config, promptgen, vision, roster as roster_mod
    monkeypatch.setattr(store, "_con", None)
    monkeypatch.setattr(config, "resolve_path", lambda p: tmp_path / p)
    monkeypatch.setattr(main.config, "resolve_path", lambda p: tmp_path / p)

    async def fake_generate(script_text, model=None, refs=None):
        return {"character": "TEST", "face_base": "", "schemes": [
            {"name": f"s{i}", "prompt": f"p{i}", "note": ""} for i in range(1, 9)],
            "model": "fake", "raw": ""}
    monkeypatch.setattr(main.promptgen, "generate_poster_schemes", fake_generate)

    async def fake_roster(script_text, model=None, caller=None):
        return [{"name": "Evelyn Hart", "gender": "Female", "age": 22,
                 "identity": "失明千金", "appearance": "金棕长发"}]
    monkeypatch.setattr(roster_mod, "extract_roster", fake_roster)
    monkeypatch.setattr(main.roster_mod, "extract_roster", fake_roster)

    async def fake_batch(image_paths, roster_list, caller=None):
        return [{"name": "Evelyn Hart", "confidence": 0.9, "reason": "x"} for _ in image_paths]
    monkeypatch.setattr(vision, "recognize_batch", fake_batch)
    monkeypatch.setattr(main.vision, "recognize_batch", fake_batch)

    client = TestClient(main.app)
    # 先传一张无名参考图 → pending
    import base64
    png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
    r = client.post("/api/refs", files={"file": ("e.png", png)}, data={"name": ""})
    assert r.json()["pending"] is True

    r = client.post("/api/script", files={"file": ("剧本.docx", _docx_bytes())})
    assert r.status_code == 200, r.text
    assert r.json()["roster_count"] == 1
    # roster.json 已缓存、pending 已补识别
    assert roster_mod.load_roster()[0]["name"] == "Evelyn Hart"
    lst = client.get("/api/refs").json()["refs"]
    assert lst[0]["name"] == "Evelyn Hart" and "pending" not in lst[0]


def test_script_roster_failure_does_not_block(tmp_path, monkeypatch):
    from app import store, main, config, roster as roster_mod
    monkeypatch.setattr(store, "_con", None)
    monkeypatch.setattr(config, "resolve_path", lambda p: tmp_path / p)
    monkeypatch.setattr(main.config, "resolve_path", lambda p: tmp_path / p)

    async def fake_generate(script_text, model=None, refs=None):
        return {"character": "TEST", "face_base": "", "schemes": [
            {"name": f"s{i}", "prompt": f"p{i}", "note": ""} for i in range(1, 9)],
            "model": "fake", "raw": ""}
    monkeypatch.setattr(main.promptgen, "generate_poster_schemes", fake_generate)

    async def boom(script_text, model=None, caller=None):
        raise RuntimeError("抽取失败")
    monkeypatch.setattr(roster_mod, "extract_roster", boom)
    monkeypatch.setattr(main.roster_mod, "extract_roster", boom)

    client = TestClient(main.app)
    r = client.post("/api/script", files={"file": ("剧本.docx", _docx_bytes())})
    assert r.status_code == 200 and r.json()["roster_count"] == 0  # 海报照常出
```

注意：`extract_roster` 自身已吞异常返回 []，这里的 boom 打桩是双保险——main 里仍要 try/except 包裹（防未来改动）。

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run --with pytest python -m pytest tests/test_script_api.py -v`
Expected: 新 2 个 FAIL（roster_count 缺失等）

- [ ] **Step 3: 改 `script_to_posters`**——在 `text = scriptdoc.extract_text(...)` 成功之后、`generate_poster_schemes` 之前插入角色表抽取，在生成成功后补识别：

```python
    try:
        # 前置：抽角色表并缓存（固定 gemini-3.6-flash，不跟表单模型——kimi-k3 长文会超时；失败不阻塞海报生成）
        try:
            roster_list = await roster_mod.extract_roster(text)
        except Exception:
            roster_list = []
        roster_mod.save_roster(roster_list)

        # 把当前参考图清单（编号=顺序）随剧本一起给 LLM，提示词里才会带「（参考图片N）」限定词
        result = await promptgen.generate_poster_schemes(text, model, refs=_load_refs())

        # 补识别：早先于剧本上传的无名参考图
        refs = _load_refs()
        pend = [r for r in refs if r.get("pending")]
        if pend and roster_list:
            try:
                recs = await vision.recognize_batch(
                    [_refs_dir() / r["file"] for r in pend], roster_list)
                for entry, rec in zip(pend, recs):
                    entry.update({"name": rec["name"] or entry.get("name", ""),
                                  "confidence": rec["confidence"], "reason": rec["reason"]})
                    if rec["name"]:
                        entry.pop("pending", None)
                _store_refs(refs)
            except Exception:
                pass  # 补识别失败保持 pending，下次再说
        result["roster_count"] = len(roster_list)
        return result
    except promptgen.PromptGenError as e:
        raise HTTPException(502, str(e))
```

（原函数里 `return await promptgen.generate_poster_schemes(...)` 一行被上面整块替换；docx 解析/异常处理部分不动。）

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run --with pytest python -m pytest tests/test_script_api.py -v`
Expected: 全 PASS

- [ ] **Step 5: 全量测试**

Run: `uv run --with pytest python -m pytest tests/ -v`
Expected: 全绿

---

### Task 5: 前端参考图区（徽章 / 改名 / 重识别 / 名字选填 / 角色表状态）

**Files:**
- Modify: `app/static/index.html`（参考图 CSS 约 54 行、HTML 约 139–146 行、JS 约 273–315 行）

**Interfaces:**
- Consumes: `GET /api/refs`（条目现含 `confidence?/reason?/pending?`）、`POST /api/refs/rename`、`POST /api/refs/recognize`、`/api/script` 响应的 `roster_count`

- [ ] **Step 1: CSS**——`/* 参考图管理 */` 区块内追加：

```css
  .ref-item .nm-input{width:100%;font-size:12px;border:1px solid transparent;background:transparent;text-align:center;border-radius:4px;padding:1px 2px}
  .ref-item .nm-input:hover,.ref-item .nm-input:focus{border-color:#444;outline:none}
  .ref-badge{display:inline-block;font-size:10px;padding:0 5px;border-radius:6px;margin-top:2px}
  .ref-badge.hi{background:#1d4d2b;color:#7fe0a0}
  .ref-badge.mid{background:#4d3f1d;color:#e0c97f}
  .ref-badge.lo{background:#4d1d1d;color:#e07f7f}
  .ref-recog{background:none;border:0;cursor:pointer;font-size:12px;padding:0}
```

（已确认 `.ref-item` 现有样式带 `position:relative`；🔍 不做绝对定位，放进徽章行内联展示，避免与左上角编号、右上角删除钮冲突。）

- [ ] **Step 2: HTML**——添加参考图一行的名字输入框改提示文案：

```html
    <input type="text" id="refName" placeholder="角色名（可留空，AI 自动识别）" style="width:200px">
```

- [ ] **Step 3: JS `refreshRefs()` 重写渲染**（替换现有 `$('#refList').innerHTML = ...` 整段）：

```javascript
  $('#refList').innerHTML = r.refs.map((x, i) => {
    let badge = '';
    if (x.pending) badge = '<span class="ref-badge lo">⏳ 待识别</span>';
    else if (x.confidence != null) {
      const c = x.confidence;
      const cls = c >= 0.9 ? 'hi' : c >= 0.6 ? 'mid' : 'lo';
      badge = `<span class="ref-badge ${cls}" title="${escapeHtml(x.reason || '')}">${c >= 0.6 ? Math.round(c * 100) + '%' : '未识别'}</span>`;
    }
    return `
    <div class="ref-item">
      <img src="${x.url}" loading="lazy" title="${escapeHtml(x.name)}">
      <span class="num">${i + 1}</span>
      <button class="del" onclick="delRef('${x.id}')" title="删除这张参考图">✕</button>
      <input class="nm-input" value="${escapeHtml(x.name)}" placeholder="未命名"
             onchange="renameRef('${x.id}', this.value)" title="点击可改名">
      <div>${badge}<button class="ref-recog" onclick="reRecog('${x.id}')" title="AI 重新识别">🔍</button></div>
    </div>`;
  }).join('') || '<div class="sub">还没上传参考图（不上传则人物长相由 AI 自由发挥）</div>';
```

- [ ] **Step 4: JS 新函数 + 上传放宽**（紧跟 `window.delRef` 之后追加；`btnAddRef` 里删掉 `if (!name) return alert(...)` 一行）：

```javascript
window.renameRef = async (id, name) => {
  await fetch('/api/refs/rename', {method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({id, name})});
  refreshRefs();
};
window.reRecog = async id => {
  const r = await (await fetch('/api/refs/recognize', {method: 'POST',
    headers: {'Content-Type': 'application/json'}, body: JSON.stringify({id})})).json();
  if (r.detail) alert(r.detail);
  refreshRefs();
};
```

- [ ] **Step 5: 剧本成功后显示角色表状态 + 刷新参考图**——`btnScript` 成功分支里（`schemes` 渲染之后）追加：

```javascript
    $('#scriptMsg').textContent = `✅ 8 组提示词已生成；角色表已就绪（${r.roster_count ?? 0} 角色）`;
    refreshRefs();  // 可能有 pending 图刚被补识别
```

（先读现有成功分支代码，变量名以实际为准。）

- [ ] **Step 6: 全量测试 + 启动冒烟**

Run: `uv run --with pytest python -m pytest tests/ -v`（前端无单测，后端全绿即可）
再 `uv run python -m app.main` 或管家菜单启动，浏览器开 http://127.0.0.1:8321 确认参考图区渲染正常

---

### Task 6: 收尾（版本号 / README / 打包确认 / 实测）

**Files:**
- Modify: `VERSION`、`package.json`、`pyproject.toml`、`README.md`、`工作台管家.command`（只读确认）

- [ ] **Step 1: 版本 1.3.1 → 1.4.0 四处**（VERSION 文件、package.json `"version"`、pyproject.toml `version`、README 顶部「当前版本」行）

- [ ] **Step 2: README 功能段**「角色参考图」一条改写为：

```markdown
- **角色参考图**：可上传最多 10 张角色参考图并按上传顺序编号；**角色名可留空——视觉模型对照剧本角色表自动识别填名（带置信度徽章，可随时手改/重识别）**；提示词里写「角色名（参考图片N）」，生成时每组**只上传该组实际引用的图**并自动重编号
```

- [ ] **Step 3: 打包排除确认**——`rg "refs" 工作台管家.command`：refs/ 目录应已在 rsync 排除清单（refs.json、roster.json 都在其内，无需新增）；若只有 refs.json 单列则补上 roster.json

- [ ] **Step 4: 全量测试**

Run: `uv run --with pytest python -m pytest tests/ -v`
Expected: 全部 PASS（约 33 个）

- [ ] **Step 5: 真实环境冒烟（需杰配合或授权联网调 LLM）**——用《我的护工丈夫是亿万富豪》剧本 + 4 张定妆照在界面上走一遍：传剧本（角色表 8 个）→ 逐张传图自动识别 → 徽章显示 → 改名 → 重识别

- [ ] **Step 6: 记忆归档**——journal 记 v1.4.0 上线 + FACT.md 更新版本与能力清单
