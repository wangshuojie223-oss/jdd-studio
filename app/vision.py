"""参考图视觉识别：缩图 → 视觉模型对照角色表认人。固定 gemini-3.6-flash。"""
from __future__ import annotations

import base64
import io
import json
from pathlib import Path

from openai import AsyncOpenAI

from . import config, roster as roster_mod

MATCH_ONE_PROMPT = """这是某短剧的一张演员定妆参考图。请对照剧本角色表，判断图中最可能是哪个角色。
依据：性别、年龄段、气质、着装与角色身份的吻合度。
只输出 JSON：[{"image":1,"match":"角色name或null","confidence":0.0~1.0,"reason":"20字内"}]，不要其他文字。
图中人物与任何角色都不像时 match 填 null。

角色表：
%s"""

MATCH_BATCH_PROMPT = """下面是某短剧的 %d 张演员定妆参考图（按发送顺序编号1~%d），以及剧本角色表。
请把每张图匹配到最可能的角色。依据：性别、年龄段、气质、着装与角色身份的吻合度。
每张图输出：image（编号）、match（角色name，不像任何角色填null）、confidence（0~1）、reason（20字内）。
只输出 JSON 数组，不要其他文字。

角色表：
%s"""


def shrink_b64(image_path: Path, max_px: int = 768, quality: int = 80) -> str:
    """必须缩图：原图（~3MB/张）直送网关会 400。768px JPEG q80 ≈ 34KB。"""
    from PIL import Image
    im = Image.open(image_path).convert("RGB")
    im.thumbnail((max_px, max_px))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=quality)
    return base64.b64encode(buf.getvalue()).decode()


async def _default_caller(messages: list[dict], model: str) -> str:
    cfg = config.llm_config(model)
    client = AsyncOpenAI(base_url=cfg["base_url"], api_key=cfg["api_key"], timeout=120)
    resp = await client.chat.completions.create(model=cfg["model"], messages=messages, temperature=0.3)
    return resp.choices[0].message.content or ""


def _null(reason: str = "") -> dict:
    return {"name": None, "confidence": 0.0, "reason": reason}


def _to_result(item: dict) -> dict:
    """模型输出项 → 统一结构；match 不在角色表里也照收（前端可人工判断）。"""
    return {
        "name": item.get("match") or None,
        "confidence": float(item.get("confidence") or 0),
        "reason": str(item.get("reason") or ""),
    }


async def recognize(image_path: Path, roster_list: list[dict], caller=None) -> dict:
    """单张识别。LLM 异常向外抛（调用方决定降级为 pending）。"""
    if not roster_list:
        return _null("无角色表")
    call = caller or _default_caller
    prompt = MATCH_ONE_PROMPT % json.dumps(roster_list, ensure_ascii=False)
    content = [
        {"type": "text", "text": prompt},
        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{shrink_b64(image_path)}"}},
    ]
    raw = await call([{"role": "user", "content": content}], roster_mod.VISION_MODEL)
    items = roster_mod.parse_json_loose(raw)
    if not items:
        return _null("模型输出无法解析")
    return _to_result(items[0])


async def recognize_batch(image_paths: list[Path], roster_list: list[dict], caller=None) -> list[dict]:
    """一次请求多张图（补识别用），返回与输入等长的结果列表（按输入顺序）。"""
    if not roster_list or not image_paths:
        return [_null("无角色表") for _ in image_paths]
    call = caller or _default_caller
    n = len(image_paths)
    prompt = MATCH_BATCH_PROMPT % (n, n, json.dumps(roster_list, ensure_ascii=False))
    content = [{"type": "text", "text": prompt}]
    for p in image_paths:
        content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{shrink_b64(p)}"}})
    raw = await call([{"role": "user", "content": content}], roster_mod.VISION_MODEL)
    items = roster_mod.parse_json_loose(raw) or []
    by_idx = {int(i.get("image", 0)): i for i in items if isinstance(i, dict)}
    return [_to_result(by_idx[k]) if k in by_idx else _null("模型未返回该图") for k in range(1, n + 1)]
