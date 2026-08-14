"""FastAPI 后端：路由 + 任务队列 + SSE + 静态文件。"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import tempfile
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import browser, capcut_driver, castsheet, config, pipeline, promptgen, roster as roster_mod, scriptdoc, store, vision

# ---------------- 任务队列（单 worker 串行） ----------------
_queue: asyncio.Queue[str] = asyncio.Queue()
_sse_subs: set[asyncio.Queue] = set()
_worker_task: asyncio.Task | None = None

# ---------------- 生成进度（内存态，用于进度条和剩余时间预估） ----------------
_progress: dict[str, dict] = {}

# 每组内各阶段约占的时间比重：投递很快，收集（等图）最久，下载其次
_STAGE_WEIGHT = {"navigating": 0.03, "dispatching": 0.15, "collecting": 0.85, "downloading": 0.95}


def _progress_update(gid: str, stage: str, index: int | None, total: int | None) -> dict:
    """根据当前阶段算出完成百分比，并用「已用时 ÷ 已完成比例」外推剩余时间。

    投递即走模式下阶段顺序：navigating → dispatching → collecting → downloading
    各阶段按「已处理组数 ÷ 总组数」线性推进本阶段区间。
    """
    p = _progress.setdefault(gid, {"started": time.time()})
    if stage == "dispatching":
        frac = 0.03 + 0.12 * (index / total) if index and total else 0.06
    elif stage == "collecting":
        frac = 0.15 + 0.70 * (index / total) if index and total else 0.50
    elif stage == "downloading":
        frac = 0.85 + 0.10 * (index / total) if index and total else 0.90
    elif stage == "navigating":
        frac = 0.02
    else:
        frac = _STAGE_WEIGHT.get(stage, 0.5)
    frac = min(max(frac, 0.0), 0.99)
    elapsed = time.time() - p["started"]
    # 刚开始时比例太小，外推会严重失真，先不显示 ETA
    eta = elapsed * (1 - frac) / frac if frac > 0.03 else None
    p.update({
        "percent": round(frac * 100, 1),
        "eta_seconds": round(eta) if eta is not None else None,
        "index": index or 0,
        "total": total or 0,
        "stage": stage,
        "elapsed": round(elapsed),
    })
    return {k: p[k] for k in ("percent", "eta_seconds", "index", "total", "stage")}


def broadcast(event: dict):
    for q in list(_sse_subs):
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            pass


def _prepare_job(gen: dict) -> dict:
    """把数据库里的任务记录整理成流水线作业（参考图快照解析为带绝对路径的清单）。"""
    prompts = gen.get("prompts") or ([gen["prompt"]] if gen.get("prompt") else [])
    refs = None
    if gen.get("kind") == "poster":
        refs_dir = config.resolve_path(config.get("paths", "refs_dir", default="refs"))
        refs = [{**r, "path": str(refs_dir / r["file"])} for r in (gen.get("refs") or [])]
    return {"gen_id": gen["id"], "prompts": prompts,
            "character_name": gen.get("character_name", ""),
            "refs": refs, "count_per_group": int(gen.get("count_per_group") or 4)}


async def _worker():
    """流水线调度器：攒一批排队任务，单页面连续投递、统一收集（批内不排队）。"""
    while True:
        await _queue.get()
        await asyncio.sleep(0.3)  # 合并连发入队：连续点几次生成只开一次页面
        jobs = [_prepare_job(g) for g in store.list_generations_by_status(["queued"])]
        if not jobs:
            continue

        async def on_status(stage, gen_id=None, **info):
            # 任务终态：done/failed/cancelled
            if stage == "job_done":
                store.update_generation(gen_id, status="done", image_paths=info.get("images", []),
                                        error=info.get("error", ""))
                broadcast({"type": "status", "id": gen_id, "status": "done", "images": info.get("images", [])})
                _progress.pop(gen_id, None)
                return
            if stage in ("job_failed", "job_cancelled"):
                st = "failed" if stage == "job_failed" else "cancelled"
                store.update_generation(gen_id, status=st, stage="", error=info.get("error", ""))
                broadcast({"type": "status", "id": gen_id, "status": st, "error": info.get("error", "")})
                _progress.pop(gen_id, None)
                return
            # 收集阶段的在飞清单（前端多进度卡用）
            if stage == "collect_active":
                broadcast({"type": "collect_active", "active": info.get("active", [])})
                return
            # 常规阶段进度（带 gen_id 才记账）
            if gen_id:
                store.update_generation(gen_id, status=stage, stage=stage)
                broadcast({"type": "status", "id": gen_id, "status": stage, **info})
                payload = _progress_update(gen_id, stage, info.get("index"), info.get("total"))
                broadcast({"type": "progress", "id": gen_id, **payload})

        try:
            await pipeline.run_pipeline(jobs, status_cb=on_status)
        except Exception as e:
            # 兜底：流水线整体异常时把仍在进行中的任务标记失败（导航失败已逐任务上报过）
            for j in jobs:
                g = store.get_generation(j["gen_id"])
                if g and g["status"] in _ACTIVE_STATUSES:
                    store.update_generation(j["gen_id"], status="failed", stage="unknown", error=str(e))
                    broadcast({"type": "status", "id": j["gen_id"], "status": "failed", "error": str(e)})
                    _progress.pop(j["gen_id"], None)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _worker_task
    if os.environ.get("JDD_DEBUG"):
        import faulthandler
        import sys
        faulthandler.dump_traceback_later(45, repeat=True, file=sys.stderr)
    try:
        _clear_refs_on_start()  # 每次启动清空参考图+角色表（与剧本同步归零）
    except Exception as e:
        print(f"[startup] 清空参考图失败（不影响启动）：{e}")
    _worker_task = asyncio.create_task(_worker())
    yield
    _worker_task.cancel()
    await browser.close()


app = FastAPI(title="剧多多文生图工作台", lifespan=lifespan)

STATIC_DIR = Path(__file__).parent / "static"


# ---------------- 请求模型 ----------------
class PromptReq(BaseModel):
    description: str
    model: str | None = None
    count: int = 6


class GenReq(BaseModel):
    prompt: str = ""
    prompts: list[str] | None = None   # 批量：整批提示词依次生成
    character_name: str = ""
    kind: str = "character"            # character=角色图 / poster=剧本海报
    count_per_group: int = 4           # 每组张数：4=选"4张"点1次 / 6=选"3张"连点2次


class SaveReq(BaseModel):
    image_paths: list[str]
    character_name: str = "未命名角色"
    dest_root: str | None = None  # None/"default"=项目内 saved_images；否则须是已登记的自定义目录


class SaveDirReq(BaseModel):
    path: str


class MkdirReq(BaseModel):
    path: str
    name: str


class ReviewSendReq(BaseModel):
    image_paths: list[str]
    gen_id: str = ""


class ReviewApproveReq(BaseModel):
    file_name: str
    title: str = "海报"
    dest_root: str | None = None


class ReviewRejectReq(BaseModel):
    id: str


class ReviewDeleteDoneReq(BaseModel):
    file_name: str


class OpenFolderReq(BaseModel):
    which: str  # pending | done


class RefDeleteReq(BaseModel):
    id: str


class RefRenameReq(BaseModel):
    id: str
    name: str


class RefRecogReq(BaseModel):
    id: str


# ---------------- 参考图管理（剧本海报模式） ----------------

def _refs_dir() -> Path:
    d = config.resolve_path(config.get("paths", "refs_dir", default="refs"))
    d.mkdir(parents=True, exist_ok=True)
    return d


def _load_refs() -> list[dict]:
    try:
        return json.loads((_refs_dir() / "refs.json").read_text(encoding="utf-8"))
    except Exception:
        return []


def _store_refs(refs: list[dict]):
    (_refs_dir() / "refs.json").write_text(
        json.dumps(refs, ensure_ascii=False, indent=2), encoding="utf-8"
    )


@app.get("/api/refs")
def list_refs():
    """参考图清单：列表顺序即全局编号（第 1 个 = 参考图片1）。"""
    return {"refs": [{**r, "url": f"/refs/{r['file']}"} for r in _load_refs()]}


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


@app.post("/api/refs/delete")
def delete_ref(req: RefDeleteReq):
    refs = _load_refs()
    left = [r for r in refs if r["id"] != req.id]
    if len(left) == len(refs):
        raise HTTPException(404, "参考图不存在")
    _store_refs(left)
    fname = next((r["file"] for r in refs if r["id"] == req.id), None)
    if fname:
        try:
            (_refs_dir() / fname).unlink()
        except FileNotFoundError:
            pass
    return {"ok": True}


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


class RefImportConfirmReq(BaseModel):
    token: str


def _staging_dir(token: str) -> Path | None:
    import re as _re
    if not _re.fullmatch(r"[0-9a-f]{8}", token or ""):
        return None
    return _refs_dir() / f".staging_{token}"


@app.post("/api/refs/clear")
def clear_refs():
    """一键清空参考图：删全部条目+图片（含孤儿文件），保留 roster.json 角色表。"""
    _store_refs([])
    for f in _refs_dir().iterdir():
        if f.is_file() and f.suffix.lower() in _IMG_EXTS:
            f.unlink()
    return {"ok": True}


def _clear_refs_on_start():
    """启动时全量清空：参考图条目+图片+中断的导入暂存 + 剧本角色表（roster.json）。

    参考图和角色表都归属于「当前这一部剧」：旧剧残留的角色表会让新剧参考图的
    AI 自动识别对错人，所以每次启动两者同步归零，从下次上传剧本重新建立。
    """
    _store_refs([])
    d = _refs_dir()
    for f in d.iterdir():
        if f.is_file() and f.suffix.lower() in _IMG_EXTS:
            f.unlink()
        elif f.is_dir() and f.name.startswith(".staging_"):
            shutil.rmtree(f, ignore_errors=True)
    roster_mod.clear_roster()


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
    import io as _io
    from PIL import Image
    meta = []
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
    # 删旧：清单内文件 + refs/ 根目录下清单外的孤儿图片（.staging_* 子目录不受影响）
    for r in _load_refs():
        try:
            (_refs_dir() / r["file"]).unlink()
        except FileNotFoundError:
            pass
    for f in _refs_dir().iterdir():
        if f.is_file() and f.suffix.lower() in _IMG_EXTS:
            f.unlink()
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


# ---------------- 保存位置管理 ----------------
SAVE_DIRS_FILE = config.root() / "save_dirs.json"


def _default_save_dir() -> Path:
    return config.resolve_path(config.get("paths", "saved_dir", default="saved_images"))


def _load_save_dirs() -> dict:
    try:
        data = json.loads(SAVE_DIRS_FILE.read_text(encoding="utf-8"))
        return {"custom": data.get("custom", []), "last": data.get("last", "default")}
    except Exception:
        return {"custom": [], "last": "default"}


def _store_save_dirs(data: dict):
    SAVE_DIRS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _resolve_dest_root(dest_root: str | None) -> Path:
    """把前端传来的保存根目录解析成安全路径：只允许 默认目录 或 已登记的自定义目录。"""
    if not dest_root or dest_root == "default":
        return _default_save_dir()
    p = Path(dest_root).expanduser().resolve()
    if str(p) in _load_save_dirs()["custom"]:
        return p
    raise HTTPException(400, "该目录未登记，请先通过「浏览选择新位置」添加")


@app.get("/api/save-dirs")
def save_dirs():
    data = _load_save_dirs()
    return {
        "default": str(_default_save_dir()),
        "custom": data["custom"],
        "last": data["last"],
    }


@app.post("/api/save-dirs")
def add_save_dir(req: SaveDirReq):
    """登记一个自定义保存目录（不存在则创建），并记为最近使用。"""
    p = Path(req.path).expanduser().resolve()
    try:
        p.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        raise HTTPException(400, f"目录不可用：{e}")
    if not p.is_dir():
        raise HTTPException(400, "该路径不是文件夹")
    if not os.access(p, os.W_OK):
        raise HTTPException(400, "该文件夹没有写入权限")
    data = _load_save_dirs()
    sp = str(p)
    if sp not in data["custom"]:
        data["custom"].append(sp)
    data["last"] = sp
    _store_save_dirs(data)
    return {"ok": True, "path": sp}


@app.post("/api/save-dirs/last")
def set_last_save_dir(req: SaveDirReq):
    """记住最近使用的保存位置（"default" 或已登记目录）。"""
    data = _load_save_dirs()
    if req.path != "default" and req.path not in data["custom"]:
        raise HTTPException(400, "该目录未登记")
    data["last"] = req.path
    _store_save_dirs(data)
    return {"ok": True}


@app.get("/api/browse")
def browse_dirs(path: str | None = None):
    """文件夹浏览器：列出指定路径下的子文件夹（默认用户主目录）。"""
    p = Path(path).expanduser() if path else Path.home()
    p = p.resolve()
    if not p.is_dir():
        raise HTTPException(400, "文件夹不存在")
    dirs = []
    try:
        for child in sorted(p.iterdir(), key=lambda x: x.name.lower()):
            if child.is_dir() and not child.name.startswith("."):
                dirs.append(child.name)
    except PermissionError:
        raise HTTPException(403, "没有权限访问该文件夹")
    return {"path": str(p), "parent": str(p.parent), "dirs": dirs}


@app.post("/api/mkdir")
def make_dir(req: MkdirReq):
    name = req.name.strip()
    if not name or "/" in name or "\\" in name or name in (".", ".."):
        raise HTTPException(400, "文件夹名不合法")
    base = Path(req.path).expanduser().resolve()
    if not base.is_dir():
        raise HTTPException(400, "上级文件夹不存在")
    target = base / name
    try:
        target.mkdir(exist_ok=True)
    except Exception as e:
        raise HTTPException(400, f"创建失败：{e}")
    return {"ok": True, "path": str(target)}


# ---------------- 路由 ----------------
@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/config")
def get_config():
    llm = config.llm_config()
    return {
        "version": config.version(),
        "models": config.get("llm", "models", default=[llm["model"]]),
        "model": llm["model"],
        "key_source": llm["key_source"],
        "site_url": config.get("site", "url"),
        "selectors_ready": browser.is_configured(),
    }


@app.post("/api/prompts")
async def gen_prompts(req: PromptReq):
    if not req.description.strip():
        raise HTTPException(400, "角色描述不能为空")
    try:
        return await promptgen.generate_schemes(req.description.strip(), req.model, req.count)
    except promptgen.PromptGenError as e:
        raise HTTPException(502, str(e))


@app.post("/api/script")
async def script_to_posters(file: UploadFile = File(...), model: str = Form(default=None)):
    """上传剧本 docx → 解析全文 → LLM 出 8 组海报提示词（6 亮调 2 暗调）。"""
    if not (file.filename or "").lower().endswith(".docx"):
        raise HTTPException(400, "只支持 .docx 格式的 Word 文档")
    try:
        with tempfile.NamedTemporaryFile(suffix=".docx", delete=False) as tmp:
            tmp.write(await file.read())
            tmp_path = Path(tmp.name)
        text = scriptdoc.extract_text(tmp_path)
    except scriptdoc.ScriptDocError as e:
        raise HTTPException(400, str(e))
    finally:
        try:
            os.unlink(tmp.name)
        except Exception:
            pass
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


@app.get("/api/characters")
async def list_chars():
    """抓取剧多多项目里的全部角色卡名字（供控制台「选角色卡」下拉）。"""
    try:
        return {"characters": await pipeline.list_characters()}
    except pipeline.PipelineError as e:
        code = 401 if e.stage == "auth" else 502
        raise HTTPException(code, str(e))
    except Exception as e:
        raise HTTPException(502, f"获取角色列表失败：{e}")


@app.post("/api/generate")
async def generate(req: GenReq):
    plist = [p.strip() for p in (req.prompts or []) if p and p.strip()]
    if not plist and req.prompt.strip():
        plist = [req.prompt.strip()]
    if not plist:
        raise HTTPException(400, "提示词不能为空")
    if not req.character_name.strip():
        raise HTTPException(400, "请先选择角色卡")
    if not browser.is_configured():
        raise HTTPException(400, "页面选择器未配置，请先运行 bun run calibrate 完成校准")
    if req.kind not in ("character", "poster"):
        raise HTTPException(400, f"非法的任务类型：{req.kind}")
    if req.count_per_group not in (4, 6):
        raise HTTPException(400, f"每组张数只支持 4 或 6：{req.count_per_group}")
    # 参考图快照：提交时定格当前清单（重试/排队期间改动参考图不影响本任务）
    refs_snapshot = _load_refs() if req.kind == "poster" else []
    gen = store.create_generation(plist[0], req.character_name.strip(), prompts=plist, kind=req.kind,
                                  refs=refs_snapshot, count_per_group=req.count_per_group)
    await _queue.put(gen["id"])
    return {"generation_id": gen["id"], "count": len(plist)}


@app.get("/api/generations")
def list_gens():
    items = store.list_generations()
    # 把内存中的实时进度（百分比/剩余时间）合并进去，前端刷新页面也能接上
    for it in items:
        p = _progress.get(it["id"])
        if p:
            it["progress"] = p
    return {"items": items}


# 还在跑/排队的状态（终止按钮要对这些生效）
_ACTIVE_STATUSES = ("queued", "navigating", "dispatching", "collecting", "downloading")


@app.post("/api/stop")
async def stop_all():
    """终止全部生成：排队中的直接标记取消（worker 会跳过），正在跑的通知 pipeline 立刻中断。"""
    stopped = []
    for g in store.list_generations():
        if g["status"] not in _ACTIVE_STATUSES:
            continue
        if g["status"] == "queued":
            store.update_generation(g["id"], status="cancelled", error="已手动终止")
            broadcast({"type": "status", "id": g["id"], "status": "cancelled"})
            # 也要打进 pipeline 的取消标记：流水线里「待投递」的任务靠它跳过
            await pipeline.request_cancel(g["id"])
            stopped.append(g["id"])
        else:
            # 运行中的：打取消标记并强关页面/下载连接，worker 随后把状态置为 cancelled
            await pipeline.request_cancel(g["id"])
            stopped.append(g["id"])
    return {"stopped": stopped, "count": len(stopped)}


@app.post("/api/generations/{gid}/stop")
async def stop_gen(gid: str):
    """终止单个任务。"""
    gen = store.get_generation(gid)
    if not gen:
        raise HTTPException(404, "任务不存在")
    if gen["status"] not in _ACTIVE_STATUSES:
        raise HTTPException(400, "该任务已结束，无需终止")
    if gen["status"] == "queued":
        store.update_generation(gid, status="cancelled", error="已手动终止")
        broadcast({"type": "status", "id": gid, "status": "cancelled"})
    else:
        await pipeline.request_cancel(gid)
    return {"ok": True}


@app.post("/api/generations/{gid}/delete")
def delete_gen(gid: str):
    """删除一组生成记录：数据库记录 + candidates/<gid>/ 候选图文件一起删。
    已保存到「保存位置」的图是复制件，不受影响。运行中的任务禁止删除（先终止）。"""
    gen = store.get_generation(gid)
    if not gen:
        raise HTTPException(404, "任务不存在")
    if gen["status"] in _ACTIVE_STATUSES:
        raise HTTPException(400, "任务正在进行中，请先终止再删除")
    store.delete_generation(gid)
    cand_dir = config.resolve_path(config.get("paths", "candidates_dir", default="candidates")) / gid
    shutil.rmtree(cand_dir, ignore_errors=True)
    return {"ok": True}


@app.post("/api/generations/{gid}/retry")
async def retry_gen(gid: str):
    gen = store.get_generation(gid)
    if not gen:
        raise HTTPException(404, "任务不存在")
    if gen["status"] not in ("failed", "cancelled"):
        raise HTTPException(400, "只有失败或被终止的任务可以重试")
    store.update_generation(gid, status="queued", error="", stage="")
    await _queue.put(gid)
    return {"ok": True}


@app.post("/api/save")
def save_images(req: SaveReq):
    if not req.image_paths:
        raise HTTPException(400, "未选择图片")
    # 直接平铺保存到选定位置，不再建 角色名/日期 子文件夹
    # （候选图文件名本身已含 角色名+组号+时间戳，平铺也不会重名）
    dest_dir = _resolve_dest_root(req.dest_root)
    dest_dir.mkdir(parents=True, exist_ok=True)

    saved, errors = [], []
    root = config.root().resolve()
    for p in req.image_paths:
        src = (root / p).resolve()
        # 防目录穿越：只允许复制项目内 candidates/ 的文件
        if not str(src).startswith(str(root)) or not src.exists():
            errors.append(f"{p}：文件不存在或越界")
            continue
        dest = dest_dir / src.name
        n = 1
        while dest.exists():
            dest = dest_dir / f"{src.stem}_{n}{src.suffix}"
            n += 1
        shutil.copy2(src, dest)
        try:
            saved_path = str(dest.relative_to(root))
        except ValueError:
            saved_path = str(dest)  # 项目外目录存绝对路径
        rec = store.add_saved(req.character_name.strip(), p, saved_path)
        saved.append(rec)
    try:
        dest_display = str(dest_dir.relative_to(root))
    except ValueError:
        dest_display = str(dest_dir)
    # 记住这次用的位置
    if req.dest_root and req.dest_root != "default":
        try:
            data = _load_save_dirs(); data["last"] = req.dest_root; _store_save_dirs(data)
        except Exception:
            pass
    return {"saved": saved, "errors": errors, "dest_dir": dest_display}


# ---------------- 复审（AI 扩图） ----------------
_IMG_EXTS = (".png", ".jpg", ".jpeg", ".webp")


def _review_dir(which: str) -> Path:
    key = "review_pending_dir" if which == "pending" else "review_done_dir"
    default = "review_pending" if which == "pending" else "review_done"
    d = config.resolve_path(config.get("paths", key, default=default))
    d.mkdir(parents=True, exist_ok=True)
    return d


@app.post("/api/review/send")
def review_send(req: ReviewSendReq):
    """把勾选的候选图复制进待扩图文件夹，建复审记录，并拉起剪映（未装则只开文件夹）。"""
    if not req.image_paths:
        raise HTTPException(400, "未选择图片")
    root = config.root().resolve()
    pending = _review_dir("pending")
    sent = 0
    for p in req.image_paths:
        src = (root / p).resolve()
        # 防目录穿越：只允许项目内文件
        if not str(src).startswith(str(root)) or not src.exists():
            continue
        stem, suffix = src.stem, src.suffix
        dest = pending / src.name
        n = 1
        while dest.exists():
            dest = pending / f"{stem}_{n}{suffix}"
            n += 1
        shutil.copy2(src, dest)
        store.create_review(req.gen_id, p)
        sent += 1
    if not sent:
        raise HTTPException(400, "没有可送审的图片（文件不存在或越界）")
    has_capcut = capcut_driver.send_to_expand(pending)
    return {"sent": sent, "capcut": has_capcut}


@app.get("/api/review/list")
def review_list():
    pending = []
    for r in store.list_reviews(status="pending"):
        fp = _review_dir("pending") / r["file_name"]
        pending.append({**r, "url": "/" + r["src_path"], "file_exists": fp.exists()})
    done_dir = _review_dir("done")
    done = [
        {"file_name": f.name, "url": f"/review_done/{f.name}", "mtime": f.stat().st_mtime}
        for f in sorted(done_dir.iterdir(), key=lambda x: x.stat().st_mtime)
        if f.suffix.lower() in _IMG_EXTS
    ]
    return {"pending": pending, "done": done}


@app.post("/api/review/approve")
def review_approve(req: ReviewApproveReq):
    """采用扩后图：平铺复制到保存位置 + 记 saved 表 + 尽力配对 pending 记录置 approved。"""
    name = Path(req.file_name).name  # 防目录穿越
    src = _review_dir("done") / name
    if not src.exists():
        raise HTTPException(404, "扩后图不存在")
    dest_dir = _resolve_dest_root(req.dest_root)
    dest_dir.mkdir(parents=True, exist_ok=True)
    stem, suffix = Path(name).stem, Path(name).suffix
    dest = dest_dir / name
    n = 1
    while dest.exists():
        dest = dest_dir / f"{stem}_{n}{suffix}"
        n += 1
    shutil.copy2(src, dest)
    root = config.root().resolve()
    try:
        saved_path = str(dest.relative_to(root))
    except ValueError:
        saved_path = str(dest)
    store.add_saved(req.title, f"review_done/{name}", saved_path)
    # 尽力配对：pending 原图文件名与扩后图文件名互为包含即认为同源；
    # 配对成功后清掉待扩图文件副本（使命完成，避免残留占空间）
    for r in store.list_reviews(status="pending"):
        s = Path(r["file_name"]).stem
        if s in stem or stem in s:
            store.update_review(r["id"], status="approved", expanded_name=name)
            try:
                (_review_dir("pending") / r["file_name"]).unlink()
            except FileNotFoundError:
                pass
    return {"ok": True, "saved_path": saved_path}


@app.post("/api/review/reject")
def review_reject(req: ReviewRejectReq):
    """撤回待扩图：删待扩文件 + 删记录。"""
    rows = [r for r in store.list_reviews() if r["id"] == req.id]
    if not rows:
        raise HTTPException(404, "记录不存在")
    try:
        (_review_dir("pending") / rows[0]["file_name"]).unlink()
    except FileNotFoundError:
        pass
    store.delete_review(req.id)
    return {"ok": True}


@app.post("/api/review/delete-done")
def review_delete_done(req: ReviewDeleteDoneReq):
    name = Path(req.file_name).name
    fp = _review_dir("done") / name
    if not fp.exists():
        raise HTTPException(404, "文件不存在")
    fp.unlink()
    return {"ok": True}


@app.post("/api/open-folder")
def open_folder(req: OpenFolderReq):
    if req.which not in ("pending", "done"):
        raise HTTPException(400, "which 只能是 pending/done")
    subprocess.run(["open", str(_review_dir(req.which))], check=False)
    return {"ok": True}


@app.get("/api/events")
async def events():
    q: asyncio.Queue = asyncio.Queue(maxsize=200)
    _sse_subs.add(q)

    async def stream():
        try:
            yield 'data: {"type": "connected"}\n\n'
            while True:
                try:
                    ev = await asyncio.wait_for(q.get(), timeout=25)
                    yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
        finally:
            _sse_subs.discard(q)

    return StreamingResponse(stream(), media_type="text/event-stream")


# ---------------- 静态图片 ----------------
app.mount("/candidates", StaticFiles(directory=config.resolve_path(config.get("paths", "candidates_dir", default="candidates"))), name="candidates")
app.mount("/saved", StaticFiles(directory=config.resolve_path(config.get("paths", "saved_dir", default="saved_images"))), name="saved")
for _d in ("review_pending", "review_done"):
    app.mount(f"/{_d}", StaticFiles(directory=_review_dir(_d)), name=_d)
app.mount("/refs", StaticFiles(directory=_refs_dir()), name="refs")
