"""视觉识别测试（LLM 打桩，PIL 缩图走真实代码）。"""
from __future__ import annotations

import asyncio
import base64
import io

ROSTER = [
    {"name": "Evelyn Hart", "gender": "Female", "age": 22, "identity": "失明千金", "appearance": "金棕长发"},
    {"name": "Richard Hart", "gender": "Male", "age": 53, "identity": "父亲", "appearance": "银灰发三件套"},
]


def _photo(tmp_path, size=(2000, 1000), name="p.png"):
    from PIL import Image
    p = tmp_path / name
    Image.new("RGB", size, (120, 30, 30)).save(p)
    return p


def test_shrink_b64(tmp_path):
    from app import vision
    b64 = vision.shrink_b64(_photo(tmp_path))
    from PIL import Image
    im = Image.open(io.BytesIO(base64.b64decode(b64)))
    assert im.format == "JPEG" and max(im.size) <= 768


def test_recognize_empty_roster_skips_llm(tmp_path):
    from app import vision
    r = asyncio.run(vision.recognize(_photo(tmp_path), []))
    assert r == {"name": None, "confidence": 0.0, "reason": "无角色表"}


def test_recognize_match(tmp_path):
    from app import vision

    async def fake_caller(messages, model):
        assert model == "gemini-3.6-flash"
        content = messages[-1]["content"]
        assert any(p.get("type") == "image_url" for p in content)  # 图真的带上了
        return '[{"image":1,"match":"Evelyn Hart","confidence":0.95,"reason":"金棕长发年轻女"}]'

    r = asyncio.run(vision.recognize(_photo(tmp_path), ROSTER, caller=fake_caller))
    assert r["name"] == "Evelyn Hart" and r["confidence"] == 0.95


def test_recognize_bad_json_returns_null(tmp_path):
    from app import vision

    async def garbage(messages, model):
        return "我看不懂"

    r = asyncio.run(vision.recognize(_photo(tmp_path), ROSTER, caller=garbage))
    assert r["name"] is None and r["confidence"] == 0.0


def test_recognize_batch_maps_by_index(tmp_path):
    from app import vision
    paths = [_photo(tmp_path, name="a.png"), _photo(tmp_path, name="b.png")]

    async def fake_caller(messages, model):
        return ('[{"image":2,"match":"Richard Hart","confidence":0.9,"reason":"银灰发"},'
                '{"image":1,"match":"Evelyn Hart","confidence":0.8,"reason":"年轻女"}]')

    rs = asyncio.run(vision.recognize_batch(paths, ROSTER, caller=fake_caller))
    assert [r["name"] for r in rs] == ["Evelyn Hart", "Richard Hart"]  # 按输入顺序，不按返回顺序
