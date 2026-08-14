"""剧本 → 角色表（name/gender/age/identity/appearance），缓存到 refs/roster.json。"""
from __future__ import annotations

import json
import re

from openai import AsyncOpenAI

from . import config

ROSTER_PROMPT = """你是短剧选角导演。通读下面剧本，抽取【有名有姓的主要角色】名单（最多8个，按戏份排序）。
每个角色输出：name（英文名）、gender、age（约数）、identity（一句话身份）、appearance（外貌关键特征：发型发色/气质/常见着装，用于和演员定妆照比对）。
只输出 JSON 数组，不要任何其他文字。

剧本：
%s"""

# 角色表抽取/视觉识别固定模型（实测 3 次全对且最便宜），不受界面模型下拉影响
VISION_MODEL = "gemini-3.6-flash"


def _roster_file():
    d = config.resolve_path(config.get("paths", "refs_dir", default="refs"))
    d.mkdir(parents=True, exist_ok=True)
    return d / "roster.json"


def parse_json_loose(text: str) -> list | None:
    """容错解析 JSON 数组：去 ```json 围栏、截取首个 [ 到末个 ]。失败返回 None。"""
    m = re.search(r"\[.*\]", text or "", re.S)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except Exception:
        return None
    return data if isinstance(data, list) else None


async def _default_caller(messages: list[dict], model: str) -> str:
    cfg = config.llm_config(model)
    client = AsyncOpenAI(base_url=cfg["base_url"], api_key=cfg["api_key"], timeout=120)
    resp = await client.chat.completions.create(model=cfg["model"], messages=messages, temperature=0.3)
    return resp.choices[0].message.content or ""


async def extract_roster(script_text: str, model: str | None = None, caller=None) -> list[dict]:
    """LLM 抽角色表。任何失败返回 []（不阻塞海报生成）。"""
    call = caller or _default_caller
    try:
        raw = await call([{"role": "user", "content": ROSTER_PROMPT % script_text}], model or VISION_MODEL)
        return parse_json_loose(raw) or []
    except Exception:
        return []


def load_roster() -> list[dict]:
    try:
        return json.loads(_roster_file().read_text(encoding="utf-8"))
    except Exception:
        return []


def save_roster(roster: list[dict]) -> None:
    _roster_file().write_text(json.dumps(roster, ensure_ascii=False, indent=2), encoding="utf-8")


def clear_roster() -> None:
    """删除缓存的角色表（启动时与参考图一起归零，避免旧剧角色表污染新剧的自动识别）。"""
    try:
        _roster_file().unlink()
    except FileNotFoundError:
        pass
