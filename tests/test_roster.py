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
