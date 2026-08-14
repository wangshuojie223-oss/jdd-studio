"""提示词生成测试：海报格式解析 + 角色格式兼容 + skill 文件实时加载。"""
from __future__ import annotations

from app import promptgen

SAMPLE = """## 剧名：ECHOES OF SILENCE
**剧本基调（8 组共用）**：悬疑惊悚，压抑中爆发
### 方案一｜群像对峙（亮调）
冷蓝渐变背景的五人对峙海报……主标题"ECHOES OF SILENCE"以金属光效悬浮于画面视觉中心……
> 设计说明：对峙构图外化冲突
### 方案二｜暗夜独行（暗调）
低-key布光，主角剪影……
> 设计说明：压迫感
"""


def test_parse_poster_format():
    r = promptgen.parse_schemes(SAMPLE)
    assert r["character"] == "ECHOES OF SILENCE"
    assert "悬疑" in r["face_base"]
    assert len(r["schemes"]) == 2
    assert "亮调" in r["schemes"][0]["name"]
    assert "暗调" in r["schemes"][1]["name"]


def test_parse_character_format_still_works():
    text = "## 角色：冷面杀手\n**面部基底（6 组共用）**：高颧骨\n### 方案一｜冷峻\n提示词内容\n> 设计说明：x"
    r = promptgen.parse_schemes(text)
    assert r["character"] == "冷面杀手" and r["face_base"] == "高颧骨"


def test_load_poster_prompt_reads_skill_file():
    p = promptgen.load_poster_prompt()
    assert "15%" in p and "78%" in p and "禁止" in p  # 安全区规则在模板里
    assert not p.startswith("---")  # frontmatter 已剥除
