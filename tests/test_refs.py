"""参考图接口测试：上传/列表/删除 + 生成任务 refs 快照。"""
from __future__ import annotations

import io

from fastapi.testclient import TestClient


def _png() -> bytes:
    """最小合法 PNG（1x1）。"""
    import base64
    return base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
    )


def _setup(tmp_path, monkeypatch):
    from app import store, main, config
    monkeypatch.setattr(store, "_con", None)
    monkeypatch.setattr(config, "root", lambda: tmp_path)
    monkeypatch.setattr(config, "resolve_path", lambda p: tmp_path / p)
    monkeypatch.setattr(main.config, "resolve_path", lambda p: tmp_path / p)
    return main, TestClient(main.app)


def test_refs_crud(tmp_path, monkeypatch):
    main, client = _setup(tmp_path, monkeypatch)

    r = client.post("/api/refs", files={"file": ("jack.png", _png())}, data={"name": "杰克（男主）"})
    assert r.status_code == 200, r.text
    rid = r.json()["id"]

    r = client.post("/api/refs", files={"file": ("e.png", _png())}, data={"name": "艾琳"})
    assert r.status_code == 200

    lst = client.get("/api/refs").json()["refs"]
    assert [x["name"] for x in lst] == ["杰克（男主）", "艾琳"]  # 顺序=编号
    assert lst[0]["url"].startswith("/refs/")
    assert (tmp_path / "refs").exists()

    r = client.post("/api/refs/delete", json={"id": rid})
    assert r.status_code == 200
    lst = client.get("/api/refs").json()["refs"]
    assert len(lst) == 1 and lst[0]["name"] == "艾琳"


def test_generate_snapshots_refs(tmp_path, monkeypatch):
    main, client = _setup(tmp_path, monkeypatch)
    from app import store
    monkeypatch.setattr(main.browser, "is_configured", lambda: True)
    client.post("/api/refs", files={"file": ("a.png", _png())}, data={"name": "杰克"})

    r = client.post("/api/generate", json={"prompts": ["杰克（参考图片1）站在雨里"], "character_name": "海报", "kind": "poster"})
    assert r.status_code == 200
    gid = r.json()["generation_id"]
    g = store.get_generation(gid)
    assert len(g["refs"]) == 1 and g["refs"][0]["name"] == "杰克"
    # 快照不受后续删除影响
    client.post("/api/refs/delete", json={"id": g["refs"][0]["id"]})
    g2 = store.get_generation(gid)
    assert len(g2["refs"]) == 1


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
    monkeypatch.setattr(vision, "recognize", _fake_recog())  # 不应被调用，结果以手填为准

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


def _xlsx_bytes(tmp_path):
    from test_castsheet import build_sheet
    p = build_sheet(tmp_path / "候选表.xlsx")
    return p.read_bytes()


def test_import_preview_and_confirm_replaces(tmp_path, monkeypatch):
    main, client = _setup(tmp_path, monkeypatch)
    # 先有一张旧参考图（将被替换）
    r = client.post("/api/refs", files={"file": ("old.png", _png())}, data={"name": "旧角色"})
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


def test_import_confirm_cleans_orphan_images(tmp_path, monkeypatch):
    """confirm 替换时：refs/ 根目录下清单外的孤儿图片文件也一并清理。"""
    main, client = _setup(tmp_path, monkeypatch)
    (tmp_path / "refs").mkdir(parents=True, exist_ok=True)
    (tmp_path / "refs" / "orphan.png").write_bytes(b"orphan")  # 不在清单里的残留

    r = client.post("/api/refs/import", files={"file": ("候选表.xlsx", _xlsx_bytes(tmp_path))})
    token = r.json()["token"]
    r = client.post("/api/refs/import/confirm", json={"token": token})
    assert r.status_code == 200
    assert not (tmp_path / "refs" / "orphan.png").exists()
    assert len(r.json()["refs"]) == 2


def test_clear_refs(tmp_path, monkeypatch):
    """一键清空：条目+图片+孤儿全删，roster.json 角色表保留。"""
    main, client = _setup(tmp_path, monkeypatch)
    _seed_roster(tmp_path)
    client.post("/api/refs", files={"file": ("a.png", _png())}, data={"name": "甲"})
    client.post("/api/refs", files={"file": ("b.png", _png())}, data={"name": "乙"})
    (tmp_path / "refs" / "orphan.png").write_bytes(b"x")

    r = client.post("/api/refs/clear")
    assert r.status_code == 200 and r.json()["ok"] is True
    assert client.get("/api/refs").json()["refs"] == []
    assert not list((tmp_path / "refs").glob("*.png"))
    assert (tmp_path / "refs" / "roster.json").exists()  # 角色表保留


def test_startup_clears_refs_and_roster(tmp_path, monkeypatch):
    """启动清空：参考图条目+图片+中断导入的暂存目录+剧本角色表 全归零。"""
    main, client = _setup(tmp_path, monkeypatch)
    from app import roster as roster_mod

    # 造残留：一张参考图（无角色表时上传→pending，不触发视觉识别）+ roster + staging 目录
    r = client.post("/api/refs", files={"file": ("old.png", _png())}, data={"name": "旧角色"})
    assert r.status_code == 200, r.text
    roster_mod.save_roster([{"name": "OldName"}])
    staging = main._refs_dir() / ".staging_deadbeef"
    staging.mkdir()
    (staging / "x.png").write_bytes(_png())
    assert roster_mod.load_roster()

    main._clear_refs_on_start()

    assert client.get("/api/refs").json()["refs"] == []      # 条目清空
    assert roster_mod.load_roster() == []                     # 角色表同步清空
    assert not staging.exists()                               # 暂存目录清掉
    assert not list(main._refs_dir().glob("*.png"))           # 图片文件清掉
