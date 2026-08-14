"""复审链路测试：送去扩图 → 列表 → 采用入成品库 → 撤回。"""
from __future__ import annotations

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
    assert r.status_code == 200, r.text
    assert (tmp_path / "saved_images" / "p1_01_expanded.png").exists()

    r = client.post("/api/review/reject", json={"id": lst["pending"][0]["id"]})
    assert r.status_code == 200
    assert not (tmp_path / "review_pending" / "p1_01.png").exists()

    # 删已扩图文件
    r = client.post("/api/review/delete-done", json={"file_name": "p1_01_expanded.png"})
    assert r.status_code == 200
    assert not (tmp_path / "review_done" / "p1_01_expanded.png").exists()

    # 越界防护：candidates 外的路径不允许送审
    r = client.post("/api/review/send", json={"image_paths": ["../outside.png"], "gen_id": ""})
    assert r.status_code in (400, 200) and r.json().get("sent", 0) == 0
