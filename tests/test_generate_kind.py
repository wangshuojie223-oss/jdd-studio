"""生成接口 kind 参数测试：poster 落库、非法值拒绝。"""
from __future__ import annotations

from fastapi.testclient import TestClient


def _client(tmp_path, monkeypatch):
    from app import store, main
    monkeypatch.setattr(store, "_con", None)
    monkeypatch.setattr(store.config, "root", lambda: tmp_path)
    monkeypatch.setattr(main.browser, "is_configured", lambda: True)
    return TestClient(main.app)


def test_generate_accepts_poster_kind(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)

    r = client.post("/api/generate", json={"prompts": ["海报提示词"], "character_name": "海报", "kind": "poster"})
    assert r.status_code == 200, r.text
    items = client.get("/api/generations").json()["items"]
    assert items[0]["kind"] == "poster"

    r2 = client.post("/api/generate", json={"prompts": ["x"], "character_name": "jj", "kind": "bogus"})
    assert r2.status_code == 400

    # 不传 kind 默认 character（角色图流程不受影响）
    r3 = client.post("/api/generate", json={"prompts": ["角色提示词"], "character_name": "jj"})
    assert r3.status_code == 200
    items = client.get("/api/generations").json()["items"]
    kinds = {i["prompt"]: i["kind"] for i in items}
    assert kinds["角色提示词"] == "character"


def test_generate_count_per_group(tmp_path, monkeypatch):
    """每组张数：6 落库（重试沿用），缺省 4，非法值 400。"""
    client = _client(tmp_path, monkeypatch)

    r = client.post("/api/generate", json={"prompts": ["六张组"], "character_name": "jj", "count_per_group": 6})
    assert r.status_code == 200, r.text
    items = client.get("/api/generations").json()["items"]
    assert items[0]["count_per_group"] == 6

    # 不传默认 4
    r2 = client.post("/api/generate", json={"prompts": ["默认组"], "character_name": "jj"})
    assert r2.status_code == 200
    items = client.get("/api/generations").json()["items"]
    cpg = {i["prompt"]: i["count_per_group"] for i in items}
    assert cpg["默认组"] == 4

    # 非法值拒绝
    r3 = client.post("/api/generate", json={"prompts": ["x"], "character_name": "jj", "count_per_group": 5})
    assert r3.status_code == 400


def test_count_plan():
    """管线张数方案：4→("4张",1 次)，6→("3张",2 次)，其他一律回退 4 张方案。"""
    from app import pipeline
    assert pipeline._count_plan(4) == ("4张", 1)
    assert pipeline._count_plan(6) == ("3张", 2)
    assert pipeline._count_plan(0) == ("4张", 1)


def test_platform_name():
    """下载文件名沿用剧多多平台原始名（OSS 对象名），异常输入走兜底。"""
    from app import pipeline as pl
    # 正常 OSS 带签名 URL
    u = "https://bucket.oss-cn-beijing.aliyuncs.com/cgpt/2026-08-13/abc123def456.png?Expires=999&OSSAccessKeyId=x&Signature=y"
    assert pl._platform_name(u) == "abc123def456.png"
    # URL 编码 + 中文名
    u2 = "https://b.oss-cn-x.aliyuncs.com/p/%E6%B5%B7%E6%8A%A5%E5%9B%BE.jpg?x=1"
    assert pl._platform_name(u2) == "海报图.jpg"
    # 无扩展名 / 路径结尾目录 → 兜底
    assert pl._platform_name("https://b.oss-cn-x.aliyuncs.com/p/12345?x=1") == ""
    assert pl._platform_name("https://b.oss-cn-x.aliyuncs.com/p/") == ""
    # 目录穿越字符被清洗（不含 /）
    assert "/" not in pl._platform_name("https://b.oss-cn-x.aliyuncs.com/p/..%2Fevil.png")
