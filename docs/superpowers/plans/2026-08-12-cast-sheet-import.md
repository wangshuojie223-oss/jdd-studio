# 飞书角色候选表导入参考图 Implementation Plan

> **For agentic workers:** 本计划按 superpowers:executing-plans 内联逐任务执行。Steps 用 `- [ ]` 跟踪。

**Goal:** 海报模式参考图区支持从飞书导出的《角色候选表》xlsx 一键导入：定版列（或黄底格）的图 + 人物列角色名，整单替换现有参考图。

**Architecture:** 新增 `app/castsheet.py`（openpyxl 解析：表头定位/图片锚点归位/黄色兜底）；`POST /api/refs/import`（预览+staging 暂存）→ `POST /api/refs/import/confirm`（替换落盘）；前端加导入按钮。手动上传+视觉识别（v1.4.0）保留共存。

**Tech Stack:** FastAPI + openpyxl（新增依赖）+ Pillow + pytest（LLM 不涉及）

**Spec:** `docs/superpowers/specs/2026-08-12-cast-sheet-import-design.md`

## Global Constraints

- 项目**不是 git 仓库**：commit 步骤=跑全量测试 `cd ~/jdd-studio && uv run --with pytest python -m pytest tests/ -v` 全绿
- 导入条目**不写** confidence/pending（人工选定，比 AI 权威）
- 导入即替换：confirm 时删除旧参考图文件+清单
- 场景候选表 sheet 不导入
- 版本收尾：1.4.0 → 1.5.0 四处（VERSION / package.json / pyproject.toml / README 顶部）

---

### Task C1: `app/castsheet.py` 解析器

**Files:**
- Create: `app/castsheet.py`
- Test: `tests/test_castsheet.py`

**Interfaces:**
- Produces:
  - `class CastSheetError(Exception)`
  - `parse_castsheet(xlsx_path: Path) -> dict` → `{"sheet": str, "characters": [{"name": str, "intro": str, "image": bytes|None, "ext": ".png"|".jpg"}]}`
- Consumes: openpyxl、Pillow（已有）

- [ ] **Step 1: 写失败测试** `tests/test_castsheet.py`

```python
"""角色候选表 xlsx 解析测试（fixture 用 openpyxl 现场构建）。"""
from __future__ import annotations

import io

import pytest


def png_bytes(color=(200, 30, 30)) -> bytes:
    from PIL import Image as PImage
    buf = io.BytesIO()
    PImage.new("RGB", (64, 64), color).save(buf, "PNG")
    return buf.getvalue()


def build_sheet(path, with_final=True, yellow_cell=None, skip_img_row=None):
    """构建迷你候选表：人物/简介[/定版]/1/2 列 + 2 行角色。返回 path。"""
    import openpyxl
    from openpyxl.drawing.image import Image as XLImage
    from openpyxl.styles import PatternFill
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "角色候选表"
    headers = ["人物", "简介"] + (["定版"] if with_final else []) + ["1", "2"]
    for i, h in enumerate(headers, 1):
        ws.cell(row=1, column=i, value=h)
    ws.cell(row=2, column=1, value="Evelyn Hart｜22岁｜女主角")
    ws.cell(row=2, column=2, value="简介A")
    ws.cell(row=3, column=1, value="Lucas Vale｜33岁｜男主角")
    ws.cell(row=3, column=2, value="简介B")
    if yellow_cell:  # 如 "C3"
        ws[yellow_cell].fill = PatternFill("solid", start_color="FFFFF258")
    img_col = "C" if with_final else "C"  # 定版列或无定版时的黄底列都放 C
    for row in (2, 3):
        if row == skip_img_row:
            continue
        img = XLImage(io.BytesIO(png_bytes((50 * row, 30, 30))))
        ws.add_image(img, f"{img_col}{row}")
    wb.save(path)
    return path


def test_parse_final_column(tmp_path):
    from app import castsheet
    r = castsheet.parse_castsheet(build_sheet(tmp_path / "t.xlsx"))
    assert r["sheet"] == "角色候选表"
    assert len(r["characters"]) == 2
    e, l = r["characters"]
    assert e["name"] == "Evelyn Hart" and e["intro"] == "简介A"
    assert e["image"] and e["ext"] == ".png"
    assert l["name"] == "Lucas Vale"


def test_parse_yellow_fallback(tmp_path):
    from app import castsheet
    # 无定版列：C3 黄底 → 图从黄底格取；第2行无黄底 → 缺图
    r = castsheet.parse_castsheet(build_sheet(tmp_path / "t.xlsx", with_final=False, yellow_cell="C3"))
    assert r["characters"][0]["image"] is None
    assert r["characters"][1]["image"] is not None


def test_parse_missing_image_row(tmp_path):
    from app import castsheet
    r = castsheet.parse_castsheet(build_sheet(tmp_path / "t.xlsx", skip_img_row=3))
    assert r["characters"][1]["image"] is None


def test_parse_no_person_header_raises(tmp_path):
    import openpyxl
    from app import castsheet
    p = tmp_path / "bad.xlsx"
    wb = openpyxl.Workbook()
    wb.active.cell(row=1, column=1, value="随便什么")
    wb.save(p)
    with pytest.raises(castsheet.CastSheetError):
        castsheet.parse_castsheet(p)


def test_name_without_separator(tmp_path):
    import openpyxl
    from openpyxl.drawing.image import Image as XLImage
    from app import castsheet
    p = tmp_path / "t.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.cell(row=1, column=1, value="人物")
    ws.cell(row=1, column=2, value="定版")
    ws.cell(row=2, column=1, value="Lucky")  # 无｜分隔
    ws.add_image(XLImage(io.BytesIO(png_bytes())), "B2")
    wb.save(p)
    r = castsheet.parse_castsheet(p)
    assert r["characters"][0]["name"] == "Lucky"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd ~/jdd-studio && uv run --with pytest --with openpyxl python -m pytest tests/test_castsheet.py -v`
Expected: FAIL `ModuleNotFoundError: No module named 'app.castsheet'`

- [ ] **Step 3: pyproject 加依赖 + 实现**

pyproject.toml dependencies 加 `"openpyxl>=3.1",` → `uv sync`。

`app/castsheet.py`：

```python
"""飞书《角色候选表》xlsx 解析：定版列（或黄底格）的图 = 选定角色参考图。"""
from __future__ import annotations

import io
from pathlib import Path

import openpyxl


class CastSheetError(Exception):
    """候选表无法解析。"""


def _is_yellow(rgb: str | None) -> bool:
    """黄色系填充（FFFFFF00 纯黄 / FFFFF258 浅黄等）：R、G 高，B 低且与 R 差距大。"""
    if not rgb or not isinstance(rgb, str) or len(rgb) != 8:
        return False
    try:
        r, g, b = int(rgb[2:4], 16), int(rgb[4:6], 16), int(rgb[6:8], 16)
    except ValueError:
        return False
    return r >= 200 and g >= 180 and b <= 160 and (r - b) >= 60


def _text(v) -> str:
    return str(v).strip() if v is not None else ""


def _find_sheet_and_header(wb):
    """在全部 sheet 前 10 行找「人物」表头。返回 (ws, header_row, {列名: 列号1-based})。"""
    for ws in wb.worksheets:
        for row in ws.iter_rows(min_row=1, max_row=10):
            cols = {_text(c.value): c.column for c in row if _text(c.value)}
            if "人物" in cols:
                return ws, row[0].row, cols
    raise CastSheetError("找不到「人物」列，请确认导出的是角色候选表（sheet 名："
                         + "/".join(ws.title for ws in wb.worksheets) + "）")


def _image_map(ws) -> dict:
    """{(row1based, col1based): png字节}——按图片锚点归位到单元格。"""
    out = {}
    for img in ws._images:
        try:
            r, c = img.anchor._from.row + 1, img.anchor._from.col + 1
        except AttributeError:
            continue  # AbsoluteAnchor 无单元格归属，跳过
        out.setdefault((r, c), img._data())
    return out


def _detect_ext(data: bytes) -> str:
    from PIL import Image
    fmt = Image.open(io.BytesIO(data)).format
    return ".jpg" if fmt == "JPEG" else ".png"


def parse_castsheet(xlsx_path: Path) -> dict:
    try:
        wb = openpyxl.load_workbook(xlsx_path)
    except Exception as e:
        raise CastSheetError(f"无法解析该文件（需要飞书导出的 .xlsx）：{e}") from e
    ws, hrow, cols = _find_sheet_and_header(wb)
    final_col = cols.get("定版")
    intro_col = cols.get("简介")
    images = _image_map(ws)

    # 黄底兜底：无定版列时，黄色填充格上的图=选定图
    yellow_cells: set[tuple[int, int]] = set()
    if final_col is None:
        for row in ws.iter_rows(min_row=hrow + 1):
            for c in row:
                if c.fill and c.fill.patternType and _is_yellow(getattr(c.fill.start_color, "rgb", None)):
                    yellow_cells.add((c.row, c.column))
        if not yellow_cells:
            raise CastSheetError("找不到「定版」列，也没有黄底标记的单元格，无法确定选定图")

    characters = []
    for row in ws.iter_rows(min_row=hrow + 1, max_col=max(cols.values())):
        person = _text(row[cols["人物"] - 1].value)
        if not person:
            continue
        name = person.split("｜")[0].strip() or person
        intro = _text(row[intro_col - 1].value) if intro_col else ""
        if final_col is not None:
            data = images.get((row[0].row, final_col))
        else:
            data = next((images[(row[0].row, c)] for (r, c) in yellow_cells
                         if r == row[0].row and (row[0].row, c) in images), None)
        characters.append({
            "name": name, "intro": intro,
            "image": data, "ext": _detect_ext(data) if data else None,
        })
    if not characters:
        raise CastSheetError("「人物」列下没有任何角色行")
    return {"sheet": ws.title, "characters": characters}
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run --with pytest python -m pytest tests/test_castsheet.py -v`
Expected: 5 PASS

- [ ] **Step 5: 全量测试**

Run: `uv run --with pytest python -m pytest tests/ -v`
Expected: 44 全绿

---

### Task C2: `/api/refs/import` 预览 + `/api/refs/import/confirm` 替换落盘

**Files:**
- Modify: `app/main.py`（refs 区块）
- Test: `tests/test_refs.py`（追加；fixture 构建器 `from test_castsheet import build_sheet, png_bytes`）

**Interfaces:**
- Consumes: `castsheet.parse_castsheet`、`CastSheetError`（C1）
- Produces:
  - `POST /api/refs/import`（表单 file=.xlsx）→ `{"token": "8位hex", "sheet": str, "total": int, "characters": [{"name","intro","has_image","w","h"}]}`；图片暂存 `refs/.staging_<token>/<序号><ext>` + `meta.json`（含 file 名）；新导入清掉所有旧 `.staging_*`
  - `POST /api/refs/import/confirm` body `{"token": str}` → 替换语义后返回 `{"refs": [...]}`（同 GET /api/refs 结构）；token 非法或不存在 → 404
- 行为：confirm 时**删除旧参考图全部文件**、写新 refs.json（条目 `{id,name,intro,file}`，无 confidence/pending）、缺图行跳过；refs.json 中 intro 字段对旧数据可选

- [ ] **Step 1: 追加失败测试**（`tests/test_refs.py` 尾部）

```python
def _xlsx_bytes(tmp_path):
    from test_castsheet import build_sheet
    p = build_sheet(tmp_path / "候选表.xlsx")
    return p.read_bytes()


def test_import_preview_and_confirm_replaces(tmp_path, monkeypatch):
    main, client = _setup(tmp_path, monkeypatch)
    # 先有一张旧参考图（将被替换）
    r = client.post("/api/refs", files={"file": ("old.png", _png())}, data={"name": "旧角色"})
    old_id = r.json()["id"]
    old_file = r.json()["file"]

    # 预览：不落 refs.json
    r = client.post("/api/refs/import", files={"file": ("候选表.xlsx", _xlsx_bytes(tmp_path))})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 2 and body["characters"][0]["name"] == "Evelyn Hart"
    assert body["characters"][0]["has_image"] is True and body["characters"][0]["w"] == 64
    assert client.get("/api/refs").json()["refs"][0]["name"] == "旧角色"  # 还没替换

    # 确认：整单替换，旧文件删除
    r = client.post("/api/refs/import/confirm", json={"token": body["token"]})
    assert r.status_code == 200, r.text
    refs = r.json()["refs"]
    assert [x["name"] for x in refs] == ["Evelyn Hart", "Lucas Vale"]
    assert refs[0]["intro"] == "简介A" and "confidence" not in refs[0]
    assert not (tmp_path / "refs" / old_file).exists()  # 旧图文件已删
    assert (tmp_path / "refs" / refs[0]["file"]).exists()


def test_import_bad_file_400(tmp_path, monkeypatch):
    main, client = _setup(tmp_path, monkeypatch)
    r = client.post("/api/refs/import", files={"file": ("x.xlsx", b"not an xlsx")})
    assert r.status_code == 400


def test_import_confirm_bad_token_404(tmp_path, monkeypatch):
    main, client = _setup(tmp_path, monkeypatch)
    assert client.post("/api/refs/import/confirm", json={"token": "deadbeef"}).status_code == 404
    assert client.post("/api/refs/import/confirm", json={"token": "../etc"}).status_code == 404
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run --with pytest python -m pytest tests/test_refs.py -v`
Expected: 新 3 个 FAIL（404）

- [ ] **Step 3: 改 `app/main.py`**

3a. import 行追加 `castsheet`：`from . import browser, capcut_driver, castsheet, config, pipeline, promptgen, roster as roster_mod, scriptdoc, store, vision`

3b. refs 区块（`recognize_ref` 之后）追加：

```python
class RefImportConfirmReq(BaseModel):
    token: str


def _staging_dir(token: str) -> Path | None:
    import re as _re
    if not _re.fullmatch(r"[0-9a-f]{8}", token or ""):
        return None
    return _refs_dir() / f".staging_{token}"


@app.post("/api/refs/import")
async def import_refs(file: UploadFile = File(...)):
    """候选表 xlsx → 预览。图片暂存 refs/.staging_<token>/，confirm 后才落正式清单。"""
    if not (file.filename or "").lower().endswith(".xlsx"):
        raise HTTPException(400, "请上传飞书导出的 .xlsx 文件")
    with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as tmp:
        tmp.write(await file.read())
        tmp_path = Path(tmp.name)
    try:
        parsed = castsheet.parse_castsheet(tmp_path)
    except castsheet.CastSheetError as e:
        raise HTTPException(400, str(e))
    finally:
        try:
            os.unlink(tmp.name)
        except Exception:
            pass

    # 清掉旧 staging，建本次暂存
    for old in _refs_dir().glob(".staging_*"):
        shutil.rmtree(old, ignore_errors=True)
    token = uuid.uuid4().hex[:8]
    staging = _staging_dir(token)
    staging.mkdir()
    meta = []
    from PIL import Image
    import io as _io
    for i, ch in enumerate(parsed["characters"], 1):
        fname = None
        w = h = 0
        if ch["image"]:
            fname = f"{i:02d}{ch['ext']}"
            (staging / fname).write_bytes(ch["image"])
            im = Image.open(_io.BytesIO(ch["image"]))
            w, h = im.size
        meta.append({"name": ch["name"], "intro": ch["intro"], "file": fname, "w": w, "h": h})
    (staging / "meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    return {"token": token, "sheet": parsed["sheet"], "total": len(meta),
            "characters": [{**m, "has_image": bool(m["file"])} for m in meta]}


@app.post("/api/refs/import/confirm")
def import_refs_confirm(req: RefImportConfirmReq):
    """确认导入：整单替换现有参考图（旧文件删除），缺图角色跳过。"""
    staging = _staging_dir(req.token)
    if staging is None or not (staging / "meta.json").exists():
        raise HTTPException(404, "导入会话不存在或已过期，请重新导入")
    meta = json.loads((staging / "meta.json").read_text(encoding="utf-8"))
    # 删旧
    for r in _load_refs():
        try:
            (_refs_dir() / r["file"]).unlink()
        except FileNotFoundError:
            pass
    # 换新
    refs = []
    for m in meta:
        if not m.get("file"):
            continue
        rid = uuid.uuid4().hex[:8]
        fname = f"{rid}{Path(m['file']).suffix}"
        (staging / m["file"]).rename(_refs_dir() / fname)
        refs.append({"id": rid, "name": m["name"], "intro": m.get("intro", ""), "file": fname})
    _store_refs(refs)
    shutil.rmtree(staging, ignore_errors=True)
    return {"refs": [{**r, "url": f"/refs/{r['file']}"} for r in refs]}
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run --with pytest python -m pytest tests/test_refs.py -v`
Expected: 全 PASS

- [ ] **Step 5: 全量测试**

Run: `uv run --with pytest python -m pytest tests/ -v`
Expected: 47 全绿

---

### Task C3: 前端导入按钮

**Files:**
- Modify: `app/static/index.html`（参考图 HTML 行 + JS refs 区块）

**Interfaces:**
- Consumes: `POST /api/refs/import`、`POST /api/refs/import/confirm`（C2）

- [ ] **Step 1: HTML**——`btnAddRef` 按钮后追加：

```html
    <button class="ghost" id="btnImportSheet">📥 从角色候选表导入</button>
    <input type="file" id="sheetFile" accept=".xlsx" style="display:none">
```

- [ ] **Step 2: JS**——`reRecog` 函数后追加：

```javascript
$('#btnImportSheet').onclick = () => $('#sheetFile').click();
$('#sheetFile').onchange = async () => {
  const f = $('#sheetFile').files[0];
  if (!f) return;
  const fd = new FormData();
  fd.append('file', f);
  const r = await (await fetch('/api/refs/import', {method: 'POST', body: fd})).json();
  $('#sheetFile').value = '';
  if (r.detail) return alert(r.detail);
  const lines = r.characters.map((c, i) => `${i + 1}. ${c.name}${c.has_image ? '' : '（缺图，将跳过）'}`);
  const cur = (state.refs || []).length;
  if (!confirm(`从「${r.sheet}」解析出 ${r.total} 个角色：\n\n${lines.join('\n')}\n\n` +
      `⚠️ 确认后将替换现有 ${cur} 张参考图`)) return;
  const c2 = await (await fetch('/api/refs/import/confirm', {method: 'POST',
    headers: {'Content-Type': 'application/json'}, body: JSON.stringify({token: r.token})})).json();
  if (c2.detail) return alert(c2.detail);
  refreshRefs();
};
```

- [ ] **Step 3: 全量测试 + 启动冒烟**

Run: `uv run --with pytest python -m pytest tests/ -v`，再重启服务开页面确认参考图区渲染

---

### Task C4: 收尾

- [ ] **Step 1: 版本 1.5.0 四处**（VERSION / package.json / pyproject.toml / README 顶部）
- [ ] **Step 2: README** 角色参考图条目开头加：「或一键导入飞书《角色候选表》xlsx：定版列（或黄底格）的图+角色名整单导入（替换现有），零手打」
- [ ] **Step 3: 打包确认**——`refs/` 整目录已在排除清单，`.staging_*` 在 refs/ 内随之排除 ✓（rg 确认）
- [ ] **Step 4: 全量测试全绿**
- [ ] **Step 5: 真实 E2E**——用桌面《我的护工丈夫是亿万富豪》角色候选表.xlsx 走 import → confirm，确认 8 角色全部带图入库、名字正确
- [ ] **Step 6: 记忆归档**——journal + FACT（版本 1.5.0、新接口、两种取图方式共存）
