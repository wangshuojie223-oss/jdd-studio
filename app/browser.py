"""浏览器会话管理：Playwright persistent context 单例（系统 Chrome + 独立 profile）。

登录态持久化在 chrome-profile/ 目录，进程重启后免登。
headless 模式（无窗口后台运行）由 config.yaml 的 browser.headless 控制，
也可用环境变量 JDD_HEADLESS=0/1 临时覆盖（登录、校准工具强制弹窗用的就是它）。
"""
from __future__ import annotations

import asyncio
import os

from playwright.async_api import BrowserContext, Page, async_playwright

from . import config

_pw = None
_context: BrowserContext | None = None
_lock = asyncio.Lock()


def _headless() -> bool:
    env = os.environ.get("JDD_HEADLESS")
    if env is not None:
        return env.lower() not in ("0", "false", "no", "")
    return bool(config.get("browser", "headless", default=False))


async def get_context() -> BrowserContext:
    """获取（或懒启动）全局浏览器上下文。"""
    global _pw, _context
    async with _lock:
        if _context is not None:
            try:
                # 探测 context 是否还活着
                _ = _context.pages
                return _context
            except Exception:
                _context = None
        _pw = await async_playwright().start()
        profile_dir = config.resolve_path(config.get("browser", "profile_dir", default="chrome-profile"))
        profile_dir.mkdir(parents=True, exist_ok=True)
        _context = await _pw.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            channel="chrome",
            headless=_headless(),
            viewport={"width": 1440, "height": 900},
            accept_downloads=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        return _context


async def new_page() -> Page:
    ctx = await get_context()
    page = await ctx.new_page()
    page.set_default_timeout(30_000)
    return page


async def close():
    global _pw, _context
    async with _lock:
        if _context is not None:
            try:
                await _context.close()
            except Exception:
                pass
            _context = None
        if _pw is not None:
            try:
                await _pw.stop()
            except Exception:
                pass
            _pw = None


async def reset():
    """浏览器崩溃后重建上下文（登录态在磁盘 profile 里，不受影响）。"""
    await close()
    await get_context()


def site_url() -> str:
    return config.get("site", "url")


def is_configured() -> bool:
    """关键选择器是否已校准。"""
    return bool(
        config.get("site", "prompt_selector")
        and config.get("site", "submit_selector")
        and config.get("site", "result_image_selector")
    )
