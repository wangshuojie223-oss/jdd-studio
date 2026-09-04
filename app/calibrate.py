"""页面校准工具：打开剧多多流程页，dump 候选元素 + 全页截图，帮助确定选择器。

用法：uv run python -m app.calibrate
输出：debug/calibrate.json（元素清单）+ debug/calibrate.png（全页截图）
"""
from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime

# 校准是肉眼调试工具，强制弹窗（即使服务配置成了无头模式）
os.environ["JDD_HEADLESS"] = "0"

from . import browser, config

DUMP_JS = """() => {
  const vis = el => {
    const r = el.getBoundingClientRect();
    const s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none';
  };
  const css = el => {
    // 生成一个尽量唯一的选择器
    if (el.id) return '#' + CSS.escape(el.id);
    let path = el.tagName.toLowerCase();
    if (el.className && typeof el.className === 'string') {
      const cls = el.className.trim().split(/\\s+/).filter(c => c && !c.startsWith('css-')).slice(0, 3);
      if (cls.length) path += '.' + cls.map(c => CSS.escape(c)).join('.');
    }
    return path;
  };
  const pick = (el, extra) => ({selector: css(el), tag: el.tagName.toLowerCase(), ...extra});

  const textareas = [...document.querySelectorAll('textarea')].filter(vis)
    .map(el => pick(el, {placeholder: el.placeholder || '', rows: el.rows}));
  const editables = [...document.querySelectorAll('[contenteditable="true"]')].filter(vis)
    .map(el => pick(el, {text: (el.innerText || '').slice(0, 50)}));
  const inputs = [...document.querySelectorAll('input[type="text"], input:not([type])')].filter(vis)
    .map(el => pick(el, {placeholder: el.placeholder || ''}));
  const buttons = [...document.querySelectorAll('button, [role="button"], a.ant-btn, div.ant-btn')]
    .filter(vis)
    .map(el => pick(el, {text: (el.innerText || '').trim().slice(0, 30)}))
    .filter(b => b.text);
  const images = [...document.querySelectorAll('img')].filter(vis)
    .map(el => pick(el, {src: (el.src || '').slice(0, 120), w: el.naturalWidth, h: el.naturalHeight}))
    .filter(i => i.w > 100);
  return {url: location.href, title: document.title, textareas, editables, inputs, buttons, images};
}"""


async def main():
    print("打开剧多多流程页（使用已保存的登录态）...")
    page = await browser.new_page()
    try:
        await page.goto(browser.site_url(), wait_until="domcontentloaded", timeout=60_000)
    except Exception as e:
        print(f"⚠️  页面加载异常（可能仍在继续）：{e}")
    print("等待 8 秒让 SPA 渲染完成...")
    await page.wait_for_timeout(8000)

    data = await page.evaluate(DUMP_JS)
    data["time"] = datetime.now().isoformat(timespec="seconds")

    debug_dir = config.resolve_path(config.get("paths", "debug_dir", default="debug"))
    debug_dir.mkdir(parents=True, exist_ok=True)
    (debug_dir / "calibrate.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    await page.screenshot(path=str(debug_dir / "calibrate.png"), full_page=True)

    print(f"\n页面：{data['title']} ({data['url']})")
    print(f"文本框 textarea: {len(data['textareas'])} 个")
    for t in data["textareas"]:
        print(f"  · {t['selector']}  placeholder={t['placeholder']!r}")
    print(f"可编辑元素 contenteditable: {len(data['editables'])} 个")
    for t in data["editables"][:10]:
        print(f"  · {t['selector']}  text={t['text']!r}")
    print(f"按钮: {len(data['buttons'])} 个")
    for b in data["buttons"][:25]:
        print(f"  · {b['selector']}  text={b['text']!r}")
    print(f"大图 img: {len(data['images'])} 个")
    for i in data["images"][:10]:
        print(f"  · {i['selector']}  {i['w']}x{i['h']}  src={i['src'][:80]}")
    print(f"\n完整结果已保存：debug/calibrate.json 和 debug/calibrate.png")
    print("把这两样发给我，我们一起确定选择器填进 config.yaml。")

    await page.close()
    await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
