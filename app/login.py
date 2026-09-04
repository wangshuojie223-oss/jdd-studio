"""首次手动登录脚本：弹出 Chrome 打开剧多多，自动检测登录完成并固化登录态。

用法：uv run python -m app.login   （或双击 工作台管家.command 选菜单 4）
流程：弹出窗口 → 用户在窗口内登录（扫码/密码均可）→ 脚本每 3 秒检测一次，
      检测到离开登录页且渲染出内容即保存登录态退出（最长等待 10 分钟）。
"""
from __future__ import annotations

import asyncio
import os

# 登录必须弹窗（要扫码/输密码），即使服务配置成了无头模式
os.environ["JDD_HEADLESS"] = "0"

from . import browser

POLL_INTERVAL = 3
MAX_WAIT = 600  # 10 分钟


async def _looks_logged_in(page) -> bool:
    url = page.url.lower()
    if "login" in url or "signin" in url:
        return False
    # 页面上还有密码框就认为没登进去
    try:
        pwd = await page.locator('input[type="password"]').count()
        if pwd > 0:
            return False
        # 有实质内容渲染出来
        return await page.evaluate(
            "document.body && document.body.innerText.trim().length > 20"
        )
    except Exception:
        return False


async def main():
    print("=" * 60)
    print("剧多多登录向导")
    print("=" * 60)
    print("✅ Chrome 窗口已弹出（专用 profile，不影响你日常浏览器）")
    print("👉 请在窗口里完成剧多多登录（扫码/账号密码均可）")
    print("   登录成功后脚本会自动检测并保存登录态，无需任何操作。\n")

    ctx = await browser.get_context()
    page = await ctx.new_page()
    await page.goto(browser.site_url(), wait_until="domcontentloaded", timeout=60_000)

    waited = 0
    while waited < MAX_WAIT:
        await page.wait_for_timeout(POLL_INTERVAL * 1000)
        waited += POLL_INTERVAL
        try:
            if await _looks_logged_in(page):
                # 再稳定确认一次，防止误判过渡页
                await page.wait_for_timeout(3000)
                if await _looks_logged_in(page):
                    title = await page.title()
                    print(f"\n🎉 检测到登录成功！当前页面：{title}")
                    print(f"   登录态已保存到 chrome-profile/，以后启动服务自动免登。")
                    await page.close()
                    await browser.close()
                    return
        except Exception as e:
            print(f"   检测异常（继续等待）：{e}")
        if waited % 30 == 0:
            print(f"   …已等待 {waited}s，继续等你登录…")

    print("\n⚠️  等待超时（10 分钟）。登录态可能未保存，请重新登录（工作台管家菜单 4）。")
    await page.close()
    await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
