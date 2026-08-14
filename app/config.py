"""配置加载：config.yaml + 环境变量 + Cherry Studio sqlite 三级解析。"""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.yaml"

# Cherry Studio 数据库（只读访问）
CHERRY_DB = Path.home() / "Library/Application Support/CherryStudio/Data/cherrystudio.sqlite"

_cfg: dict | None = None


def load() -> dict:
    """加载 config.yaml（进程内缓存，reload_config() 可强制刷新）。"""
    global _cfg
    if _cfg is None:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            _cfg = yaml.safe_load(f)
    return _cfg


def reload_config() -> dict:
    global _cfg
    _cfg = None
    return load()


def get(*keys, default=None):
    cfg = load()
    node = cfg
    for k in keys:
        if not isinstance(node, dict) or k not in node:
            return default
        node = node[k]
    return node


def root() -> Path:
    return ROOT


def version() -> str:
    """软件版本号（项目根目录 VERSION 文件）。每次更新都要递增。"""
    try:
        return (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    except Exception:
        return "0.0.0"


def resolve_path(p: str) -> Path:
    path = Path(p)
    return path if path.is_absolute() else ROOT / path


def read_key_from_cherrystudio(provider_id: str = "aionly") -> str | None:
    """从 Cherry Studio 数据库只读读取指定提供商的第一个 API key。

    api_keys 字段是 JSON 数组，元素可能是 {"id":..., "key":...} 对象或纯字符串。
    一律以 mode=ro 打开，避免与 Cherry Studio 主进程的 WAL 锁冲突。
    """
    if not CHERRY_DB.exists():
        return None
    try:
        con = sqlite3.connect(f"file:{CHERRY_DB}?mode=ro", uri=True)
        try:
            row = con.execute(
                "SELECT api_keys FROM user_provider WHERE provider_id=?",
                (provider_id,),
            ).fetchone()
        finally:
            con.close()
    except sqlite3.Error:
        return None
    if not row or not row[0]:
        return None
    try:
        keys = json.loads(row[0])
    except json.JSONDecodeError:
        return None
    if not keys:
        return None
    first = keys[0]
    if isinstance(first, dict):
        return first.get("key")
    if isinstance(first, str):
        return first
    return None


def llm_config(model_override: str | None = None) -> dict:
    """返回 {api_key, base_url, model, key_source}。key 优先级：config.yaml > 环境变量 > Cherry Studio。"""
    api_key = get("llm", "api_key")
    source = "config"
    if not api_key:
        api_key = os.environ.get("JDD_LLM_API_KEY")
        source = "env"
    if not api_key:
        api_key = read_key_from_cherrystudio()
        source = "cherrystudio"
    if not api_key:
        source = "none"
    return {
        "api_key": api_key,
        "base_url": os.environ.get("JDD_LLM_BASE_URL") or get("llm", "base_url"),
        "model": model_override or os.environ.get("JDD_LLM_MODEL") or get("llm", "model"),
        "key_source": source if api_key else "none",
    }
