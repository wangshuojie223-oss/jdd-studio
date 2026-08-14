"""任务记录存储：stdlib sqlite3，服务重启后历史不丢。"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid

from . import config

_lock = threading.RLock()  # 可重入：create_generation 等函数持锁后还会调用 _conn() 初始化
_con: sqlite3.Connection | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS generations (
    id TEXT PRIMARY KEY,
    character_name TEXT DEFAULT '',
    prompt TEXT NOT NULL,
    prompts TEXT DEFAULT '[]',               -- JSON 数组：整批提示词（批量生成）
    status TEXT NOT NULL DEFAULT 'queued',   -- queued/filling/generating/downloading/done/failed
    stage TEXT DEFAULT '',
    error TEXT DEFAULT '',
    image_paths TEXT DEFAULT '[]',           -- JSON 数组，相对项目根
    kind TEXT DEFAULT 'character',           -- character=角色图 / poster=剧本海报
    refs TEXT DEFAULT '[]',                  -- JSON 数组：提交时的参考图快照 [{id,name,file}]
    count_per_group INTEGER DEFAULT 4,       -- 每组张数：4=选"4张"点1次 / 6=选"3张"连点2次
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS saved (
    id TEXT PRIMARY KEY,
    character_name TEXT DEFAULT '',
    src_path TEXT NOT NULL,                  -- 来源候选图（相对路径）
    saved_path TEXT NOT NULL,                -- 保存后的路径（相对路径）
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS reviews (
    id TEXT PRIMARY KEY,
    gen_id TEXT DEFAULT '',                  -- 来源生成任务
    src_path TEXT NOT NULL,                  -- 送去扩图的候选原图（相对项目根）
    file_name TEXT NOT NULL,                 -- 原图文件名
    status TEXT NOT NULL DEFAULT 'pending',  -- pending/approved/rejected
    expanded_name TEXT DEFAULT '',           -- 扩后图文件名（二期自动配对用）
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
"""


def _conn() -> sqlite3.Connection:
    global _con
    if _con is None:
        db = config.root() / "tasks.db"
        # timeout：拿不到锁最多等 15s 抛错，绝不无限阻塞
        _con = sqlite3.connect(str(db), check_same_thread=False, timeout=15)
        _con.row_factory = sqlite3.Row
        with _lock:
            # WAL：多读单写不互堵；busy_timeout 双保险
            _con.execute("PRAGMA journal_mode=WAL")
            _con.execute("PRAGMA busy_timeout=15000")
            _con.executescript(SCHEMA)
        # 旧库迁移：generations 表补 prompts 列
        cols = {r[1] for r in _con.execute("PRAGMA table_info(generations)")}
        if "prompts" not in cols:
            _con.execute("ALTER TABLE generations ADD COLUMN prompts TEXT DEFAULT '[]'")
        # 旧库迁移：generations 表补 kind 列（character=角色图 / poster=剧本海报）
        if "kind" not in cols:
            _con.execute("ALTER TABLE generations ADD COLUMN kind TEXT DEFAULT 'character'")
        # 旧库迁移：generations 表补 refs 列（参考图快照）
        if "refs" not in cols:
            _con.execute("ALTER TABLE generations ADD COLUMN refs TEXT DEFAULT '[]'")
        # 旧库迁移：generations 表补 count_per_group 列（每组张数：4=单次 / 6=3张×2次）
        if "count_per_group" not in cols:
            _con.execute("ALTER TABLE generations ADD COLUMN count_per_group INTEGER DEFAULT 4")
    return _con


def _row_to_dict(r: sqlite3.Row) -> dict:
    d = dict(r)
    if "image_paths" in d:
        try:
            d["image_paths"] = json.loads(d["image_paths"])
        except (json.JSONDecodeError, TypeError):
            d["image_paths"] = []
    if "prompts" in d:
        try:
            d["prompts"] = json.loads(d["prompts"])
        except (json.JSONDecodeError, TypeError):
            d["prompts"] = []
    if "refs" in d:
        try:
            d["refs"] = json.loads(d["refs"])
        except (json.JSONDecodeError, TypeError):
            d["refs"] = []
    return d


def create_generation(prompt: str, character_name: str = "", prompts: list[str] | None = None, kind: str = "character", refs: list | None = None, count_per_group: int = 4) -> dict:
    gid = uuid.uuid4().hex[:12]
    now = time.time()
    plist = prompts if prompts else [prompt]
    if count_per_group not in (4, 6):
        count_per_group = 4
    with _lock:
        _conn().execute(
            "INSERT INTO generations (id, character_name, prompt, prompts, status, kind, refs, count_per_group, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (gid, character_name, prompt, json.dumps(plist, ensure_ascii=False), "queued", kind,
             json.dumps(refs or [], ensure_ascii=False), count_per_group, now, now),
        )
        _conn().commit()
    return get_generation(gid)


def update_generation(gid: str, **fields):
    if not fields:
        return
    fields["updated_at"] = time.time()
    if "image_paths" in fields and not isinstance(fields["image_paths"], str):
        fields["image_paths"] = json.dumps(fields["image_paths"], ensure_ascii=False)
    cols = ", ".join(f"{k}=?" for k in fields)
    with _lock:
        _conn().execute(f"UPDATE generations SET {cols} WHERE id=?", (*fields.values(), gid))
        _conn().commit()


def get_generation(gid: str) -> dict | None:
    with _lock:
        r = _conn().execute("SELECT * FROM generations WHERE id=?", (gid,)).fetchone()
    return _row_to_dict(r) if r else None


def delete_generation(gid: str) -> bool:
    with _lock:
        cur = _conn().execute("DELETE FROM generations WHERE id=?", (gid,))
        _conn().commit()
    return cur.rowcount > 0


def list_generations(limit: int = 50) -> list[dict]:
    with _lock:
        rows = _conn().execute(
            "SELECT * FROM generations ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def list_generations_by_status(statuses: list[str]) -> list[dict]:
    """按状态取任务（创建时间升序，先进先出）——流水线调度器用。"""
    if not statuses:
        return []
    marks = ",".join("?" * len(statuses))
    with _lock:
        rows = _conn().execute(
            f"SELECT * FROM generations WHERE status IN ({marks}) ORDER BY created_at ASC", statuses
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


# ---------------- 复审（AI 扩图） ----------------

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


def add_saved(character_name: str, src_path: str, saved_path: str) -> dict:
    sid = uuid.uuid4().hex[:12]
    with _lock:
        _conn().execute(
            "INSERT INTO saved (id, character_name, src_path, saved_path, created_at) VALUES (?,?,?,?,?)",
            (sid, character_name, src_path, saved_path, time.time()),
        )
        _conn().commit()
    return {"id": sid, "character_name": character_name, "saved_path": saved_path}


def list_saved(limit: int = 100) -> list[dict]:
    with _lock:
        rows = _conn().execute(
            "SELECT * FROM saved ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]
