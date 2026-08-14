"""数据层测试：kind 列迁移 + reviews 表 CRUD。"""
from __future__ import annotations


def test_kind_column_and_reviews(tmp_path, monkeypatch):
    from app import store, config
    monkeypatch.setattr(store, "_con", None)
    monkeypatch.setattr(config, "load", lambda: {})
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
    con.execute(
        "CREATE TABLE generations (id TEXT PRIMARY KEY, character_name TEXT DEFAULT '', prompt TEXT NOT NULL, prompts TEXT DEFAULT '[]', status TEXT NOT NULL DEFAULT 'queued', stage TEXT DEFAULT '', error TEXT DEFAULT '', image_paths TEXT DEFAULT '[]', created_at REAL NOT NULL, updated_at REAL NOT NULL)"
    )
    con.execute("INSERT INTO generations (id, prompt, created_at, updated_at) VALUES ('old1','hello',1,1)")
    con.commit()
    con.close()

    monkeypatch.setattr(store, "_con", None)
    monkeypatch.setattr(store.config, "root", lambda: tmp_path)
    g = store.get_generation("old1")
    assert g is not None and g["kind"] == "character"  # 迁移后默认 character
    assert g["count_per_group"] == 4  # 迁移后默认每组 4 张
    assert store.list_reviews() == []  # reviews 表已建


def test_list_by_status(tmp_path, monkeypatch):
    """流水线调度器的取队列接口：按状态过滤 + 创建时间升序（先进先出）。"""
    from app import store, config
    monkeypatch.setattr(store, "_con", None)
    monkeypatch.setattr(store.config, "root", lambda: tmp_path)
    store._con = None

    a = store.create_generation("任务A", "甲")
    b = store.create_generation("任务B", "乙")
    c = store.create_generation("任务C", "丙")
    store.update_generation(b["id"], status="done")
    queued = store.list_generations_by_status(["queued"])
    assert [g["prompt"] for g in queued] == ["任务A", "任务C"]  # B 不在 queued 里，且按创建顺序
    assert store.list_generations_by_status([]) == []
