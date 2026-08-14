"""参考图编号工具测试：提取 + 单趟重映射（含两位数不互相污染）。"""
from __future__ import annotations

import asyncio

from app import promptgen


def test_extract_basic():
    p = "杰克（参考图片1）站在左侧，艾琳（参考图片3）抱着孩子"
    assert promptgen.extract_ref_numbers(p) == [1, 3]


def test_extract_tolerates_missing_char_and_spaces():
    assert promptgen.extract_ref_numbers("角色（参考图 2）出场") == [2]
    assert promptgen.extract_ref_numbers("没有标记的提示词") == []


def test_extract_dedupes_keeps_order():
    assert promptgen.extract_ref_numbers("（参考图片3）（参考图片1）（参考图片3）") == [3, 1]


def test_remap_basic():
    p = "杰克（参考图片1）与艾琳（参考图片3）对峙"
    new, mapping = promptgen.remap_refs(p)
    assert mapping == {1: 1, 3: 2}
    assert "（参考图片1）" in new and "（参考图片2）" in new
    assert "参考图片3" not in new


def test_remap_two_digits_no_clobber():
    """两位数编号不能被一位数替换污染；重映射按全局编号升序（与上传顺序一致）。"""
    p = "（参考图片10）和（参考图片1）"
    new, mapping = promptgen.remap_refs(p)
    assert mapping == {1: 1, 10: 2}  # 升序：1号→组内1，10号→组内2
    assert new == "（参考图片2）和（参考图片1）"  # 10号先出现但变为2，互不污染


def test_remap_no_markers():
    new, mapping = promptgen.remap_refs("无标记提示词")
    assert new == "无标记提示词" and mapping == {}


def test_poster_user_msg_includes_ref_list(monkeypatch):
    captured = {}

    async def fake_call(system_prompt, user_msg, model):
        captured["user_msg"] = user_msg
        return {"schemes": [], "character": "", "face_base": ""}

    monkeypatch.setattr(promptgen, "_call_llm", fake_call)
    refs = [{"name": "杰克（男主）"}, {"name": "艾琳（女主）"}]
    asyncio.run(promptgen.generate_poster_schemes("剧本内容", refs=refs))
    assert "参考图片1=杰克（男主）" in captured["user_msg"]
    assert "参考图片2=艾琳（女主）" in captured["user_msg"]
