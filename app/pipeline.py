"""生成管线（按剧多多真实页面流程）：

打开流程页 → 项目资产 → 角色筛选 → 搜索并打开【指定的】角色资产卡（如 jj）
→ 填创意描述 → 立即生成 → 历史记录区 diff 新图 → 下载原图。

选择器全部来自 config.yaml 的 site 节（已校准）。
"""
from __future__ import annotations

import asyncio
import base64
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qs, parse_qsl, quote, unquote, urlencode, urlparse, urlsplit, urlunsplit

import httpx

from . import browser, config, promptgen


class PipelineError(Exception):
    def __init__(self, message: str, stage: str = "unknown"):
        super().__init__(message)
        self.stage = stage


class GenerationCancelled(Exception):
    """用户点了「终止」：不是失败，是主动停止。"""


# ---------- 取消机制 ----------
# _cancel_requests：收到终止指令的任务 id 集合（任务结束后移除）
# _active：正在跑的任务 -> {"page": 页面, "client": 下载连接}，终止时强制关闭，
#          让所有卡在等待中的 Playwright/httpx 操作立刻报错退出，实现「立刻停止」
_cancel_requests: set[str] = set()
_active: dict[str, dict] = {}


def _cancelled(gen_id: str) -> bool:
    return gen_id in _cancel_requests


async def request_cancel(gen_id: str):
    """终止指定任务：打标记 + 强制关闭它的下载连接。

    流水线模式下页面是多任务共享的，不能在这里关——共享页面上被终止的任务
    由收集循环检测标记后移出，页面留给其他任务继续用。仅单任务独占页面时才直接关。
    """
    _cancel_requests.add(gen_id)
    st = _active.get(gen_id)
    if not st:
        return
    client = st.get("client")
    if client is not None:
        try:
            await client.aclose()
        except Exception:
            pass
    page = st.get("page")
    if page is not None and not st.get("shared"):
        try:
            await page.close()
        except Exception:
            pass


@dataclass
class GenResult:
    image_paths: list[str] = field(default_factory=list)   # 相对项目根的路径
    image_urls: list[str] = field(default_factory=list)    # 原始 URL（原图）
    errors: list[str] = field(default_factory=list)        # 批量生成时个别组失败的信息


async def _report(cb, stage: str, **info):
    if cb:
        try:
            await cb(stage, **info)
        except Exception:
            pass


def _sel(key: str) -> str:
    v = config.get("site", key)
    if not v:
        raise PipelineError(f"选择器 site.{key} 未配置，请先运行 bun run calibrate 校准", stage="config")
    return v


# ---------- 页面操作流程 ----------

async def _check_login(page):
    if "login" in page.url.lower() or "signin" in page.url.lower():
        raise PipelineError("页面跳转到登录页，登录态已失效，请重跑 bun run login", stage="auth")
    try:
        if await page.locator('input[type="password"]').count() > 0:
            raise PipelineError("检测到登录表单，登录态已失效，请重跑 bun run login", stage="auth")
    except PipelineError:
        raise
    except Exception:
        pass


async def _goto_character_grid(page):
    """流程页 → 项目资产步骤 → 角色筛选，到达角色资产网格。"""
    await page.locator("button.step-btn", has_text="项目资产").first.click()
    await page.wait_for_timeout(2500)
    await page.locator("button.filter-btn", has_text="角色").first.click()
    await page.wait_for_timeout(2500)
    try:
        await page.wait_for_selector(".asset-card", timeout=15_000)
    except Exception:
        raise PipelineError("角色资产网格加载失败", stage="navigate")


async def _open_character_card(page, name: str):
    """在角色网格里搜索指定名字的资产卡（如 jj），悬停点「四格图标」打开它的生成面板。

    注意：卡片悬停后有多个按钮——眼睛=预览图片，四格=打开面板，别点错。
    """
    if not name:
        raise PipelineError("未指定角色卡", stage="config")
    # 用顶部搜索框过滤网格，避免在几十张卡里翻找
    search = page.locator('input[placeholder*="搜索资产"]').first
    try:
        await search.wait_for(state="visible", timeout=8000)
        await search.fill(name)
        await page.wait_for_timeout(2000)  # 等过滤生效
    except Exception:
        pass  # 搜索框不可用时退化为直接在全量网格里找

    card = page.locator(".asset-card").filter(
        has=page.locator(f'.card-info .name:text-is("{name}")')
    ).first
    try:
        await card.wait_for(state="visible", timeout=10_000)
    except Exception:
        raise PipelineError(f"找不到角色资产卡「{name}」，请检查名字或在剧多多先创建该角色", stage="select")

    open_btn = _sel("card_open_button")
    opened = False
    for attempt in range(3):
        await card.scroll_into_view_if_needed()
        await card.hover()
        await page.wait_for_timeout(1000)
        try:
            await card.locator(open_btn).first.click()
            await page.wait_for_selector(_sel("char_item"), timeout=12_000)
            opened = True
            break
        except Exception:
            # 可能误触图片预览层，关掉再重试
            try:
                await page.keyboard.press("Escape")
                await page.wait_for_timeout(800)
            except Exception:
                pass
    if not opened:
        raise PipelineError(f"角色「{name}」的生成面板未打开", stage="select")
    await page.wait_for_timeout(1000)
    # 确认面板里当前激活的就是目标角色；不是则在左栏点名（仍是指定角色，不会选别人）
    try:
        active = await page.locator(f'{_sel("char_item")}.active {_sel("char_name")}').first.inner_text()
    except Exception:
        active = ""
    if active.strip() != name:
        item = page.locator(_sel("char_item")).filter(
            has=page.locator(f'{_sel("char_name")}:text-is("{name}")')
        ).first
        await item.scroll_into_view_if_needed()
        await item.click()
        await page.wait_for_timeout(1200)
    # 确保「生成」标签激活（创意描述输入框可见）
    try:
        await page.wait_for_selector(_sel("prompt_selector"), state="visible", timeout=8000)
    except Exception:
        tab = page.locator(".asset-library-picker-tabs >> text=生成").first
        try:
            await tab.click()
            await page.wait_for_selector(_sel("prompt_selector"), state="visible", timeout=8000)
        except Exception:
            raise PipelineError("创意描述输入框不可见（生成标签未激活）", stage="select")


async def _select_model(page, model_name: str):
    """切换图像模型（如 Gpt-Image-2）。已是目标模型则跳过。"""
    sel = page.locator(".ant-select").first
    try:
        cur = await sel.inner_text()
        if model_name.lower() in cur.lower():
            return
    except Exception:
        pass
    await sel.click()
    await page.wait_for_timeout(1200)
    opt = page.locator(".ant-select-item-option", has_text=model_name).first
    try:
        await opt.click()
    except Exception:
        await page.keyboard.press("Escape")
        raise PipelineError(f"模型「{model_name}」不在下拉里，请检查 config.yaml 的 site.model_name", stage="config")
    await page.wait_for_timeout(1500)


async def _click_ratio_btn(page, text: str):
    """点参数弹窗里的按钮（精确文本匹配，只点可见的，避免残留弹窗干扰）。"""
    btns = page.locator(f'button.ratio-btn:text-is("{text}")')
    for i in range(await btns.count()):
        if await btns.nth(i).is_visible():
            await btns.nth(i).click()
            await page.wait_for_timeout(300)
            return
    raise PipelineError(f"参数选项「{text}」找不到或不可见", stage="config")


def _count_plan(count_per_group: int) -> tuple[str, int]:
    """每组张数 → (剧多多参数按钮文本, 每组点击生成次数)。

    4 张 = 选「4张」点 1 次；6 张 = 选「3张」连点 2 次（平台没有 6 张选项，3×2 拼出来）。
    """
    return ("3张", 2) if count_per_group == 6 else ("4张", 1)


async def _configure_params(page, count_text: str = "") -> int:
    """按 config 设置模型和生成参数（张数/比例/质量），返回预期图片张数。

    count_text 非空时覆盖 config 的 site.param_count（如 6 张模式要选「3张」）。
    """
    model_name = config.get("site", "model_name")
    if model_name:
        await _select_model(page, model_name)

    count_text = count_text or config.get("site", "param_count", default="")
    ratio = config.get("site", "param_ratio", default="")
    quality = config.get("site", "param_quality", default="")
    expected = 1
    m = re.match(r"(\d+)\s*张", count_text)
    if m:
        expected = int(m.group(1))

    if not (count_text or ratio or quality):
        return expected

    await page.locator("button", has_text="参数配置").first.click()
    await page.wait_for_timeout(1500)
    for text in (count_text, ratio, quality):
        if text:
            await _click_ratio_btn(page, text)
    # 关闭参数弹窗：再点一次「参数配置」，不行就 Escape
    try:
        await page.locator("button", has_text="参数配置").first.click()
        await page.wait_for_timeout(800)
    except Exception:
        pass
    if await page.locator("button.ratio-btn").first.is_visible():
        await page.keyboard.press("Escape")
        await page.wait_for_timeout(800)
    return expected


async def _set_reference_images(page, file_paths: list[str]):
    """设置面板参考图：先清空上一组的旧图，再按顺序上传本组图片。

    上传顺序 = 「参考图片N」的组内编号顺序（参考图区域的序号角标按上传顺序排）。
    file_paths 为空列表时只清空不上传（用于无参考图的组，防止串组）。
    """
    items = page.locator(".ref-image-item")
    for _ in range(15):  # 上限防死循环
        if await items.count() == 0:
            break
        try:
            first = items.first
            await first.hover()
            await page.wait_for_timeout(300)
            await first.locator("button.ref-delete-btn").click(timeout=3000)
        except Exception:
            try:
                await page.locator("button.ref-delete-btn").first.click(force=True)
            except Exception:
                break
        await page.wait_for_timeout(600)

    if not file_paths:
        return
    print(f"[pipeline] 本组上传参考图 {len(file_paths)} 张：{[Path(f).name for f in file_paths]}")
    fi = page.locator('input[type="file"]').first
    await fi.set_input_files(file_paths)
    # 等参考图区域计数达标（每张图上传渲染需要一点时间）
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if await items.count() >= len(file_paths):
            return
        await page.wait_for_timeout(800)
    raise PipelineError(f"参考图上传超时（{await items.count()}/{len(file_paths)} 张）", stage="ref")


async def _fill_prompt(page, prompt: str):
    el = page.locator(_sel("prompt_selector")).first
    await el.wait_for(state="visible", timeout=15_000)
    try:
        await el.fill(prompt)
        await page.wait_for_timeout(300)
        if (await el.evaluate("e => e.value") or "") != prompt:
            raise ValueError("fill 未生效")
    except Exception:
        await el.click()
        try:
            await el.fill("")  # 先清空，避免逐字输入叠加在上一组提示词上
        except Exception:
            pass
        await el.press_sequentially(prompt, delay=5)


async def _wait_submit_confirmed(submit_groups: list[list[str]], before: int,
                                 timeout_s: float = 10, gen_id: str = "") -> list[str]:
    """点击生成后等平台 submit 接口返回确认（真正的提交成功信号，比按钮转圈可靠）。

    submit_groups: 全局共享列表，每次 submit 响应到达时追加其 taskIds。
    before: 点击前列表长度；长度增加说明本次提交已被平台接收。
    返回本次下发的 taskIds；空列表 = 平台拒绝（failCount>0），抛错。
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if gen_id and _cancelled(gen_id):
            raise GenerationCancelled("已手动终止")
        if len(submit_groups) > before:
            tids = submit_groups[-1]
            if not tids:
                raise PipelineError("平台返回 0 个生成任务（可能被拒：内容审核/额度/参数问题）", stage="dispatch")
            return tids
        await asyncio.sleep(0.3)
    raise PipelineError(f"点击生成后 {timeout_s:.0f}s 未收到平台确认（submit 无响应），本次点击未生效", stage="dispatch")


async def _wait_dispatch_ready(page, timeout_s: float = 30, gen_id: str = ""):
    """提交确认后，等按钮「退出」loading 且输入框可编辑，再允许投下一组。

    平台点击后转圈 1-5s（处理提交），转完才能安全操作输入框/参考图投下一组。
    每 300ms 轮询（loading 窗口短，轮询太疏会错过），每秒检查取消标记。
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if gen_id and _cancelled(gen_id):
            raise GenerationCancelled("已手动终止")
        try:
            btn_loading = await page.locator(
                f'{_sel("submit_selector")}.ant-btn-loading').count() > 0
        except Exception:
            btn_loading = False
        try:
            ta = page.locator(_sel("prompt_selector")).first
            ta_visible = await ta.is_visible()
            ta_disabled = await ta.is_disabled()
        except Exception:
            ta_visible, ta_disabled = False, True
        if not btn_loading and ta_visible and not ta_disabled:
            await page.wait_for_timeout(500)  # 额外稳定期，防止 UI 闪烁
            return
        await page.wait_for_timeout(300)
    raise PipelineError(f"投递后就绪等待超时（{timeout_s:.0f}s），平台可能不支持快速投递", stage="dispatch")


# ---------- 下载 ----------

def _full_size_url(url: str) -> str:
    """去掉 OSS 缩略图参数 x-oss-process=image/resize,p_30，保留签名，得到原图。"""
    if "x-oss-process" not in url:
        return url
    parts = urlsplit(url)
    qs = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != "x-oss-process"]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(qs), parts.fragment))


def _platform_name(url: str) -> str:
    """剧多多平台原始文件名（OSS 对象路径的最后一段）。取不到/不像图片名则返回空串走兜底命名。"""
    try:
        name = unquote(urlsplit(url).path.rsplit("/", 1)[-1]).strip()
    except Exception:
        return ""
    name = re.sub(r'[\\/:*?"<>|\s]+', "_", name)  # 防目录穿越/非法字符
    if not re.fullmatch(r".+\.(png|jpe?g|webp)", name, re.I):
        return ""
    return name


def _dedupe_name(dest_dir: Path, name: str, used: set[str]) -> Path:
    """同批/同目录重名时追加 _1 _2 后缀（平台名天然唯一，这只是双保险）。"""
    stem, suffix = Path(name).stem, Path(name).suffix
    fp = dest_dir / name
    n = 1
    while name in used or fp.exists():
        name = f"{stem}_{n}{suffix}"
        fp = dest_dir / name
        n += 1
    used.add(name)
    return fp


async def _download(page, urls: list[str], dest_dir, prefix: str, gen_id: str = "") -> list[str]:
    dest_dir.mkdir(parents=True, exist_ok=True)
    cookies = await page.context.cookies()
    jar = {c["name"]: c["value"] for c in cookies}
    paths: list[str] = []
    async with httpx.AsyncClient(
        timeout=60, follow_redirects=True, cookies=jar, headers={"Referer": page.url}
    ) as client:
        # 注册到活动表：点终止时 request_cancel 会强关这个连接，进行中的下载立刻中断
        if gen_id and gen_id in _active:
            _active[gen_id]["client"] = client
        try:
            used: set[str] = set()  # 本批已用文件名（重名兜底）
            for i, url in enumerate(urls):
                if gen_id and _cancelled(gen_id):
                    raise GenerationCancelled("已手动终止")
                # 文件名：优先沿用剧多多平台原始名（OSS 对象名），取不到才用 角色_组号_序号 兜底
                if url.startswith("data:"):
                    header = url.split(",", 1)[0]
                    m = re.search(r"image/(png|jpeg|webp)", header)
                    ext = "." + (m.group(1).replace("jpeg", "jpg") if m else "png")
                    name = f"{prefix}_{i + 1:02d}{ext}"
                else:
                    full = _full_size_url(url)
                    m = re.search(r"\.(png|jpe?g|webp)$", urlsplit(full).path, re.I)
                    ext = "." + m.group(1).lower().replace("jpeg", "jpg") if m else ".png"
                    name = _platform_name(full) or f"{prefix}_{i + 1:02d}{ext}"
                fp = _dedupe_name(dest_dir, name, used)
                last_err: Exception | None = None
                for attempt in range(3):
                    try:
                        if url.startswith("data:"):
                            fp.write_bytes(base64.b64decode(url.split(",", 1)[1]))
                        else:
                            try:
                                resp = await client.get(full)
                                resp.raise_for_status()
                            except Exception:
                                resp = await client.get(url)  # 原图失败退回缩略图
                                resp.raise_for_status()
                            fp.write_bytes(resp.content)
                        break
                    except Exception as e:
                        if gen_id and _cancelled(gen_id):
                            raise GenerationCancelled("已手动终止")
                        last_err = e
                        await asyncio.sleep(2 * (attempt + 1))
                if fp and fp.exists():
                    paths.append(str(fp.relative_to(config.root())))
                else:
                    print(f"[pipeline] 第 {i + 1} 张下载失败：{last_err}")
        finally:
            if gen_id and gen_id in _active:
                _active[gen_id]["client"] = None
    return paths


# ---------- 流水线（单页面多任务：投递不等收，统一收集各归各账） ----------

@dataclass
class _JobCtx:
    """流水线中一个任务的上下文：页面/嗅探表共享，其余字段每任务独立。"""
    gen_id: str
    prompts: list[str]
    character_name: str
    refs: list[dict] | None              # None=角色图模式不碰参考图区域
    count_per_group: int = 4
    status_cb: object = None
    result: GenResult = field(default_factory=GenResult)
    dispatch_queue: list = field(default_factory=list)   # [(prompt_index, task_ids)]
    collect_of: dict = field(default_factory=dict)       # taskId -> (prompt_index, n_tasks)
    fulfilled: dict = field(default_factory=dict)        # prompt_index -> [url]（已了结，含部分完成）
    grace: dict = field(default_factory=dict)            # 全失败组宽限计数
    phase: str = "queued"                # queued/dispatching/collecting/done/failed/cancelled
    error: str = ""
    character_id: str = ""
    api_url: str = ""
    cand_dir: Path | None = None
    safe_char: str = "gen"
    clicks_per_group: int = 1
    timeout_s: float = 240.0
    collect_timeout: float = 0.0
    collect_started: float = 0.0
    last_growth: float = 0.0
    prev_done: int = 0


async def _switch_character(page, name: str):
    """角色卡面板已打开时切换角色：左栏角色列表点名，并确保「生成」标签可见。"""
    item = page.locator(_sel("char_item")).filter(
        has=page.locator(f'{_sel("char_name")}:text-is("{name}")')
    ).first
    try:
        await item.scroll_into_view_if_needed()
        await item.click()
        await page.wait_for_timeout(1500)
    except Exception:
        raise PipelineError(f"角色列表里找不到「{name}」", stage="select")
    try:
        await page.wait_for_selector(_sel("prompt_selector"), state="visible", timeout=8000)
    except Exception:
        tab = page.locator(".asset-library-picker-tabs >> text=生成").first
        try:
            await tab.click()
            await page.wait_for_selector(_sel("prompt_selector"), state="visible", timeout=8000)
        except Exception:
            raise PipelineError("创意描述输入框不可见（生成标签未激活）", stage="select")


async def _dispatch_all(ctx: _JobCtx, page, submit_groups: list):
    """Phase 2（投递）：填词→点击→submit 确认→按钮就绪→下一组，不等生成结果。

    单组失败不中断本任务（记错误继续）；6 张模式第二次点击失败时降级保住已下发的 3 张。
    """
    total = len(ctx.prompts)
    for i, prompt in enumerate(ctx.prompts):
        if _cancelled(ctx.gen_id):
            raise GenerationCancelled("已手动终止")
        tag = f"第{i + 1}/{total}组"
        all_tids: list[str] = []
        try:
            await _report(ctx.status_cb, "dispatching", gen_id=ctx.gen_id, index=i + 1, total=total)

            # ---- 参考图（海报模式） ----
            if ctx.refs is not None:
                nums = promptgen.extract_ref_numbers(prompt)
                if nums:
                    if max(nums) > len(ctx.refs):
                        raise PipelineError(
                            f"提示词引用了「参考图片{max(nums)}」，但本任务快照里只有 {len(ctx.refs)} 张参考图",
                            stage="config")
                    ordered = sorted(set(nums))
                    if len(ordered) > 10:
                        raise PipelineError("单组参考图超过剧多多 10 张上限", stage="config")
                    files = []
                    for n in ordered:
                        fp = ctx.refs[n - 1].get("path", "")
                        if not fp or not Path(fp).exists():
                            raise PipelineError(
                                f"参考图片{n}（{ctx.refs[n - 1].get('name', '?')}）的文件不存在（可能被删除）",
                                stage="config")
                        files.append(fp)
                    await _set_reference_images(page, files)
                    prompt, _ = promptgen.remap_refs(prompt)
                else:
                    await _set_reference_images(page, [])  # 清空上一组/上一任务残留

            # 填词一次，连点 clicks_per_group 次生成（6 张模式=3张×2次）
            await _fill_prompt(page, prompt)
            for _ in range(ctx.clicks_per_group):
                before = len(submit_groups)
                await page.locator(_sel("submit_selector")).first.click()

                # 信号1：submit 接口返回确认（真正的提交成功证据，带 taskIds）
                tids = await _wait_submit_confirmed(submit_groups, before, timeout_s=10, gen_id=ctx.gen_id)
                all_tids.extend(tids)
                # 信号2：等按钮转完 + 输入框可编辑，再安全点下一次/投下一组
                await _wait_dispatch_ready(page, timeout_s=30, gen_id=ctx.gen_id)
            ctx.dispatch_queue.append((i, all_tids))

        except PipelineError as e:
            if all_tids:
                # 6 张模式第二次点击失败：已下发的 3 张仍入队收集，不浪费
                ctx.dispatch_queue.append((i, all_tids))
                ctx.result.errors.append(f"{tag}只成功投递 {len(all_tids)} 张：{e}")
            else:
                ctx.result.errors.append(f"{tag}投递失败：{e}")
            # 恢复：关弹层（投递阶段面板不会关闭，无需重开角色卡）
            try:
                overlay_open = await page.locator(".ant-image-preview-mask, .ant-modal-wrap").count() > 0
                if overlay_open:
                    await page.keyboard.press("Escape")
                    await page.wait_for_timeout(800)
            except Exception:
                pass


async def _fetch_image_rows(page, api_url: str) -> list[dict]:
    """页面内 fetch images API（平台校验 x-access-token + x-version 自定义头，缺了静默返回空）。"""
    try:
        data = await page.evaluate(
            """(url) => {
                const t = localStorage.getItem('token') || '';
                return fetch(url, {
                    credentials: 'include',
                    headers: {'x-access-token': t, 'x-version': 'v3',
                              'accept': 'application/json, text/plain, */*'}
                }).then(r => r.json()).catch(() => null);
            }""",
            api_url)
        return (data or {}).get("result") or []
    except Exception:
        return []


def _tally_rows(ctx: _JobCtx, rows: list[dict]) -> dict[int, dict]:
    """把 API 行按 taskId 归到本任务的组：{prompt_index: {imgs, dead, reasons}}（仅未了结组）。"""
    groups: dict[int, dict] = {}
    for r in rows:
        hit = ctx.collect_of.get(str(r.get("taskId") or ""))
        if not hit:
            continue
        idx, _n = hit
        if idx in ctx.fulfilled:
            continue
        url = r.get("previewUrl") or r.get("thumbnailUrl")
        st = str(r.get("status") or "").lower()
        g = groups.setdefault(idx, {"imgs": [], "dead": 0, "reasons": set()})
        if url:
            g["imgs"].append(url)
        elif st in ("failed", "fail", "error", "cancelled") or r.get("errorCode"):
            g["dead"] += 1
            # 诊断：把平台给的失败原因（和完整行）打进 server.log，方便事后定位
            reason = r.get("errorMsg") or r.get("failReason") or r.get("errorCode") or st or "未知"
            g["reasons"].add(str(reason))
            print(f"[pipeline] 平台失败任务明细：{json.dumps(r, ensure_ascii=False)[:600]}")
    return groups


def _dead_note(g: dict) -> str:
    """失败备注：张数 + 平台给的失败原因（如有）。"""
    if not g["dead"]:
        return ""
    rs = "、".join(sorted(g.get("reasons") or ()))[:80]
    return f"（{g['dead']} 张平台生成失败{f'：{rs}' if rs else ''}）"


async def _finalize_group(ctx: _JobCtx, page, idx: int, imgs: list[str], note: str = ""):
    """下载一组已完成的图（完整或部分），更新 result/fulfilled。"""
    total = len(ctx.dispatch_queue)
    n_tasks = next((len(t) for i, t in ctx.dispatch_queue if i == idx), 0)
    imgs = [u for u in dict.fromkeys(imgs) if not u.startswith("data:image/svg")]
    if imgs:
        prefix = f"{ctx.safe_char}_p{idx + 1:02d}_{int(time.time())}"
        paths = await _download(page, imgs, ctx.cand_dir, prefix, ctx.gen_id)
        if paths:
            ctx.result.image_paths.extend(paths)
            ctx.result.image_urls.extend(imgs)
            if len(imgs) < n_tasks:
                ctx.result.errors.append(f"第{idx + 1}/{total}组：{len(imgs)}/{n_tasks} 张成功{note}")
        else:
            ctx.result.errors.append(f"第{idx + 1}/{total}组：结果图全部下载失败")
    else:
        ctx.result.errors.append(f"第{idx + 1}/{total}组：全部生成失败{note}")
    ctx.fulfilled[idx] = imgs


async def _collect_loop(ctxs: list["_JobCtx"], page, status_cb):
    """统一收集循环：轮询所有「在飞」任务的 images API，按 taskId 各归各账，边完成边下载。

    与后续任务的投递并行运行（同一页面，互不干扰）。每个任务独立超时/停滞/宽限判定；
    被终止的任务立刻移出（共享页面不关闭，其他任务不受影响）。
    """
    pending: dict[str, _JobCtx] = {}   # gen_id -> ctx（收集中的任务）

    async def _finish(ctx: _JobCtx):
        if ctx.result.image_paths:
            ctx.phase = "done"
            await _report(status_cb, "job_done", gen_id=ctx.gen_id,
                          images=ctx.result.image_paths, error="；".join(ctx.result.errors))
        else:
            ctx.phase = "failed"
            ctx.error = "；".join(ctx.result.errors) or "全部生成失败"
            await _report(status_cb, "job_failed", gen_id=ctx.gen_id, error=ctx.error)
        pending.pop(ctx.gen_id, None)

    while True:
        # 新完成投递的任务并入收集
        for j in ctxs:
            if j.phase == "collecting" and j.gen_id not in pending:
                pending[j.gen_id] = j
        dispatch_open = any(j.phase in ("queued", "dispatching") for j in ctxs)
        # 取消检查：被终止的任务立刻移出
        for gid in list(pending):
            if _cancelled(gid):
                j = pending.pop(gid)
                j.phase = "cancelled"
                j.error = "已手动终止"
                await _report(status_cb, "job_cancelled", gen_id=gid, error="已手动终止")
        if not pending:
            if dispatch_open:
                await page.wait_for_timeout(1500)  # 后续任务还在投递，等它并入
                continue
            break

        for gid, j in list(pending.items()):
            try:
                rows = await _fetch_image_rows(page, j.api_url)
                groups = _tally_rows(j, rows)
                total = len(j.dispatch_queue)
                # 完成判定：每组 成功图数 + 失败数 >= 下发任务数 → 了结（失败张数不再等）
                for idx, tids in j.dispatch_queue:
                    if idx in j.fulfilled:
                        continue
                    g = groups.get(idx, {"imgs": [], "dead": 0, "reasons": set()})
                    if len(g["imgs"]) + g["dead"] >= len(tids):
                        # 全失败组宽限复验：平台偶发「先报失败后出图」，多观察两轮再盖棺
                        if g["dead"] and not g["imgs"]:
                            j.grace[idx] = j.grace.get(idx, 0) + 1
                            if j.grace[idx] < 3:
                                continue
                        await _finalize_group(j, page, idx, g["imgs"], _dead_note(g))

                # 停滞检测：已完成图总数长时间无增长
                stall_timeout = min(j.timeout_s, 120)
                cur_done = sum(len(g["imgs"]) for g in groups.values()) + sum(len(v) for v in j.fulfilled.values())
                if cur_done > j.prev_done:
                    j.prev_done = cur_done
                    j.last_growth = time.monotonic()
                elif time.monotonic() - j.last_growth > stall_timeout and len(j.fulfilled) < total:
                    # 只对「已有部分图」的最早未完成组做提前了结；0 张的组继续等到总超时
                    for idx, tids in j.dispatch_queue:
                        if idx in j.fulfilled:
                            continue
                        g = groups.get(idx, {"imgs": [], "dead": 0, "reasons": set()})
                        if g["imgs"]:
                            await _finalize_group(j, page, idx, g["imgs"], _dead_note(g) + "（生成停滞，已有图已保留）")
                            j.last_growth = time.monotonic()
                            break

                # 单任务总超时兜底：未了结的组按已有图部分保留，0 张的记超时
                if len(j.fulfilled) < total and time.monotonic() - j.collect_started > j.collect_timeout:
                    rows = await _fetch_image_rows(page, j.api_url)
                    groups = _tally_rows(j, rows)
                    for idx, tids in j.dispatch_queue:
                        if idx in j.fulfilled:
                            continue
                        g = groups.get(idx, {"imgs": [], "dead": 0, "reasons": set()})
                        if g["imgs"]:
                            await _finalize_group(j, page, idx, g["imgs"], _dead_note(g) + "（等待超时，已有图已保留）")
                        else:
                            j.result.errors.append(f"第{idx + 1}/{total}组超时未生成（任务仍在平台队列中，可稍后重试）")
                            j.fulfilled[idx] = []

                await _report(status_cb, "collecting", gen_id=gid,
                              index=len(j.fulfilled), total=len(j.dispatch_queue))
                if len(j.fulfilled) >= len(j.dispatch_queue):
                    await _finish(j)
            except GenerationCancelled:
                j.phase = "cancelled"
                j.error = "已手动终止"
                await _report(status_cb, "job_cancelled", gen_id=gid, error="已手动终止")
                pending.pop(gid, None)

        # 汇总在飞清单给前端（每个任务一条独立进度卡）
        active = [{"gen_id": j.gen_id, "name": j.character_name,
                   "done": len(j.fulfilled), "total": len(j.dispatch_queue)} for j in pending.values()]
        await _report(status_cb, "collect_active", active=active)
        await page.wait_for_timeout(3000)  # 主动轮询间隔


# ---------- 对外入口 ----------

async def run_pipeline(jobs: list[dict], status_cb=None) -> list[_JobCtx]:
    """单页面多任务流水线：逐个任务投递（不等收），全部在飞任务统一轮询收集。

    jobs: [{"gen_id","prompts","character_name","refs","count_per_group"}]
    页面只开一次；第 2 个任务起只点左栏角色名单切换；模型/张数参数每任务各配一次。
    每个任务独立归账、独立成败、独立取消（取消共享页面上的任务不影响其他任务）。
    返回全部任务上下文（含各自的 result/phase）。
    """
    # 预处理 + 建上下文
    ctxs: list[_JobCtx] = []
    for j in jobs:
        prompts = [p.strip() for p in (j.get("prompts") or []) if p and p.strip()]
        if not prompts or not j.get("character_name"):
            continue
        ctx = _JobCtx(gen_id=j["gen_id"], prompts=prompts, character_name=j["character_name"],
                      refs=j.get("refs"), count_per_group=j.get("count_per_group", 4),
                      status_cb=status_cb)
        ctx.cand_dir = config.resolve_path(config.get("paths", "candidates_dir", default="candidates")) / ctx.gen_id
        ctx.safe_char = re.sub(r'[\\/:*?"<>|\s]+', "_", ctx.character_name) or "gen"
        ctx.timeout_s = float(config.get("site", "timeout_seconds", default=240))
        ctxs.append(ctx)
    if not ctxs:
        return ctxs

    await _report(status_cb, "navigating")
    page = await browser.new_page()
    for ctx in ctxs:
        _active[ctx.gen_id] = {"page": page, "client": None, "shared": True}
    try:
        # ---- 导航（页面/登录/网格失败则整批失败） ----
        try:
            await page.goto(browser.site_url(), wait_until="domcontentloaded", timeout=60_000)
            await page.wait_for_timeout(4000)  # 等 SPA 渲染
            await _check_login(page)
            await _goto_character_grid(page)
        except Exception as e:
            for ctx in ctxs:
                ctx.phase = "failed"
                ctx.error = str(e)
                await _report(status_cb, "job_failed", gen_id=ctx.gen_id, error=str(e))
            return ctxs

        # ---- 网络嗅探（覆盖整条流水线）：抓 submit 响应拿 taskIds + characterId ----
        submit_groups: list[list[str]] = []   # 每次 submit 响应追加其 taskIds（按到达顺序）
        character_ids: list[str] = []         # submit 响应里的 characterId（去重保序）

        async def sniff(resp):
            try:
                if "generationTasks/submit" not in resp.url:
                    return
                data = await resp.json()
                r = data.get("result") or {}
                tids = [str(t) for t in (r.get("taskIds") or [])]
                submit_groups.append(tids)
                cid = r.get("characterId")
                if cid and str(cid) not in character_ids:
                    character_ids.append(str(cid))
                print(f"[pipeline] submit 确认：下发 {len(tids)} 个任务（success={r.get('successCount')}, fail={r.get('failCount')}）")
            except Exception:
                submit_groups.append([])  # 响应解析失败也占位，保持与点击次数对齐

        def on_response(resp):
            asyncio.create_task(sniff(resp))

        page.on("response", on_response)
        collect_task: asyncio.Task | None = None
        try:
            first = True
            for ctx in ctxs:
                if _cancelled(ctx.gen_id):
                    ctx.phase = "cancelled"
                    ctx.error = "已手动终止"
                    await _report(status_cb, "job_cancelled", gen_id=ctx.gen_id, error="已手动终止")
                    continue
                try:
                    # Phase 1（导航）：首开走网格搜索，之后只点左栏名单切换
                    if first:
                        await _open_character_card(page, ctx.character_name)
                        first = False
                    else:
                        await _switch_character(page, ctx.character_name)
                    # 模型和生成参数每任务各配一次（6 张模式这里选「3张」，靠双击凑 6 张）
                    count_text, ctx.clicks_per_group = _count_plan(ctx.count_per_group)
                    await _configure_params(page, count_text)
                    # Phase 2（投递）
                    ctx.phase = "dispatching"
                    await _dispatch_all(ctx, page, submit_groups)
                except GenerationCancelled:
                    ctx.phase = "cancelled"
                    ctx.error = "已手动终止"
                    await _report(status_cb, "job_cancelled", gen_id=ctx.gen_id, error="已手动终止")
                    continue
                except Exception as e:
                    # 终止指令导致的杂项报错归为「已终止」而非「失败」
                    ctx.phase = "cancelled" if _cancelled(ctx.gen_id) else "failed"
                    ctx.error = "已手动终止" if _cancelled(ctx.gen_id) else str(e)
                    await _report(status_cb,
                                  "job_cancelled" if _cancelled(ctx.gen_id) else "job_failed",
                                  gen_id=ctx.gen_id, error=ctx.error)
                    continue

                if not ctx.dispatch_queue:
                    ctx.phase = "failed"
                    ctx.error = "；".join(ctx.result.errors) or "全部投递失败"
                    await _report(status_cb, "job_failed", gen_id=ctx.gen_id, error=ctx.error)
                    continue
                # 投递是串行的，最后一个 characterId 即本任务的
                ctx.character_id = character_ids[-1] if character_ids else ""
                if not ctx.character_id:
                    ctx.phase = "failed"
                    ctx.error = "未能从 submit 响应获取角色 ID，无法轮询生成进度"
                    await _report(status_cb, "job_failed", gen_id=ctx.gen_id, error=ctx.error)
                    continue
                # Phase 3（收集）：建 taskId 归账表 + 本任务的 images API 地址
                for idx, tids in ctx.dispatch_queue:
                    for t in tids:
                        ctx.collect_of[t] = (idx, len(tids))
                qs = parse_qs(urlparse(page.url).query)
                project_id = (qs.get("projectId") or [""])[0]
                ctx.api_url = (f"https://cgpt.aictr.co/api/cgpt-ai-drama-gen/pem/characters/images"
                               f"?projectId={project_id}&resourceId={ctx.character_id}"
                               f"&resourceName={quote(ctx.character_name)}&showProviderStatus=true")
                total = len(ctx.dispatch_queue)
                ctx.collect_timeout = float(config.get("site", "collect_timeout_seconds",
                                                         default=ctx.timeout_s * total))
                ctx.phase = "collecting"
                ctx.collect_started = time.monotonic()
                ctx.last_growth = time.monotonic()
                await _report(status_cb, "collecting", gen_id=ctx.gen_id, index=0, total=total)
                # 第一个任务进入收集后启动统一收集循环，与后续任务的投递并行
                if collect_task is None:
                    collect_task = asyncio.create_task(_collect_loop(ctxs, page, status_cb))
            # 全部任务投递完毕 → 等统一收集循环收尾
            if collect_task is not None:
                await collect_task
        finally:
            page.remove_listener("response", on_response)
        return ctxs
    finally:
        for ctx in ctxs:
            _active.pop(ctx.gen_id, None)
            _cancel_requests.discard(ctx.gen_id)
        try:
            await page.close()
        except Exception:
            pass


async def run_generation_batch(gen_id: str, prompts: list[str], character_name: str = "",
                               status_cb=None, refs: list[dict] | None = None,
                               count_per_group: int = 4) -> GenResult:
    """兼容旧调用：单任务跑流水线，返回该任务的结果。"""
    prompts = [p.strip() for p in prompts if p and p.strip()]
    if not prompts:
        raise PipelineError("提示词不能为空", stage="config")
    if not character_name:
        raise PipelineError("未指定角色卡", stage="config")

    async def cb(stage, **info):
        if status_cb:
            await _report(status_cb, stage, **info)

    ctxs = await run_pipeline(
        [{"gen_id": gen_id, "prompts": prompts, "character_name": character_name,
          "refs": refs, "count_per_group": count_per_group}], status_cb=cb)
    ctx = ctxs[0]
    if ctx.phase == "cancelled":
        raise GenerationCancelled(ctx.error or "已手动终止")
    if ctx.phase == "failed":
        raise PipelineError(ctx.error or "全部生成失败", stage="batch")
    return ctx.result


async def run_generation(gen_id: str, prompt: str, character_name: str = "", status_cb=None) -> GenResult:
    """单组生成（兼容旧调用）：等价于只有一组的批量生成。"""
    return await run_generation_batch(gen_id, [prompt], character_name, status_cb)


async def list_characters() -> list[str]:
    """读取角色资产网格里的全部角色名（供控制台下拉选择，每次生成前让杰挑选）。"""
    page = await browser.new_page()
    try:
        await page.goto(browser.site_url(), wait_until="domcontentloaded", timeout=60_000)
        await page.wait_for_timeout(4000)
        await _check_login(page)
        await _goto_character_grid(page)
        names: list[str] = []
        # 网格是懒加载的，滚动几轮收集全部角色名
        for _ in range(8):
            batch = await page.locator(".asset-card .card-info .name").evaluate_all(
                "els => els.map(e => (e.innerText || '').trim()).filter(Boolean)"
            )
            for n in batch:
                if n not in names:
                    names.append(n)
            before = len(names)
            await page.evaluate("window.scrollBy(0, 1200)")
            await page.wait_for_timeout(800)
            batch = await page.locator(".asset-card .card-info .name").evaluate_all(
                "els => els.map(e => (e.innerText || '').trim()).filter(Boolean)"
            )
            for n in batch:
                if n not in names:
                    names.append(n)
            if len(names) == before:
                break  # 滚动后没有新增，说明到底了
        return names
    finally:
        try:
            await page.close()
        except Exception:
            pass
