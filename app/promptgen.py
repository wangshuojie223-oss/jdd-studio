"""提示词生成：加载 film-character-prompter skill 模板，经 AIOnly LLM 输出 N 组造型方案。

模板来源（优先实时读取，杰更新 skill 后立即生效，无需改代码）：
    ~/Library/Application Support/CherryStudio/Data/Skills/film-character-prompter/SKILL.md
读不到时退回本文件里的内置副本（最后同步：2026-08-11）。

用法（CLI 自测）：
    uv run python -m app.promptgen "一个冷酷的女杀手，35 岁左右，东欧血统" [模型名]
"""
from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path

from openai import AsyncOpenAI

from . import config

SKILL_FILE = (
    Path.home()
    / "Library/Application Support/CherryStudio/Data/Skills/film-character-prompter/SKILL.md"
)
POSTER_SKILL_FILE = (
    Path.home()
    / "Library/Application Support/CherryStudio/Data/Skills/film-poster-prompter/SKILL.md"
)


def _load_skill(path: Path, fallback: str) -> str:
    """实时读取 Cherry 技能库里的 skill 原文（剥掉 YAML frontmatter）作为系统提示词。

    文件缺失/内容异常时退回内置副本，保证服务永远可用。
    """
    try:
        text = path.read_text(encoding="utf-8")
        if text.startswith("---"):
            end = text.find("\n---", 3)
            if end != -1:
                text = text[end + 4:]
        text = text.strip()
        if len(text) > 200:  # 内容 sanity check：过短说明文件异常
            return text
    except Exception:
        pass
    return fallback


def load_system_prompt() -> str:
    """角色图模板（film-character-prompter）。"""
    return _load_skill(SKILL_FILE, _FALLBACK_PROMPT)


def load_poster_prompt() -> str:
    """剧本海报模板（film-poster-prompter）。"""
    return _load_skill(POSTER_SKILL_FILE, _FALLBACK_POSTER_PROMPT)


# ---- 内置兜底副本（与 skill 最后同步：2026-08-11；正常运行时不会用到，以 skill 文件为准）----
_FALLBACK_PROMPT = """# 影视角色设计 Prompt 工程师

把导演"戏"的语言翻译成文生图"画面"的语言，一次交付同一角色的 6 组造型方案。

## 流程

1. **解析**：提取性别、年龄、种族、身份、气质、服装、标志性细节。
2. **补全**：永不追问，缺什么补什么。未指明种族默认「以美国人为原型的欧美人物」。补全要服务角色身份（服装说明阶级，神态说明经历）。
3. **覆盖检查**：仅当导演明确指定画面规格（全身/特写/换种族/胶片感等）才改模板，否则模板一字不动。**背景永远纯白，不可覆盖**——导演要求灰底/黑底/实景时仍用纯白背景，并一句话说明。
4. **生成 6 组方案**：
   - 面部基底描述 6 组**逐字一致**——同一角色的六个造型，不是六个人；
   - 每组是一个有名字的造型方向，在**发型梳理、服装、神态**三轴上变化，从保守（前 2 组）到大胆（后 2 组）；
   - 若导演已指定服装/发型，按导演指定项排布方案（如 3 套服装 × 2 种气质），不另起炉灶。
5. **输出格式**：

```
## 角色：{一句话概括}
**面部基底（6 组共用）**：{...}
### 方案一｜{方向名}
{提示词}
> 设计说明：{一句话}
```

每组提示词完整独立、可直接复制。

## 锁定模板

{ } 为变量，其余逐字保留：

```
纯白背景半身正面照，{种族原型}，{年龄段}{性别}，明星级长相，精致五官，黄金比例面部结构，极具辨识度：{面部基底——只写 2~3 个最关键记忆点}。{发型发色}。眼神直视镜头，{一个独特的眼神比喻}。身穿{服装：具体面料、剪裁、色彩}。专业工作室柔光灯，柔和蝴蝶光刻画轮廓，面部光比极小，真人般的肌肤纹理与衣服质感，照片级真实感，拒绝 CG 感、3D 渲染感与塑料感，超高分辨率，大师作品。
```

**男性布光变体**：蝴蝶光在男性面部容易产生过重阴影。男性角色将布光句替换为「专业工作室平光布光，柔和均匀，几乎无面部阴影，面部光比接近零」，模板其余部分不变。

## 规范

- 每组 **250 字以内**，连贯段落、不堆标签。**留白原则**：写完关键记忆点就收笔，其余交给模型自由发挥
- 具体名词 > 抽象形容词（「深灰色高领羊绒衫」>「高级感的衣服」）
- 眼神比喻每组一个、不重复；神态与气质不矛盾
- **表情中性有神采**：面部不露明显情绪（不笑、不怒、不皱眉），但绝不是死人脸——眼神必须有光有戏，感染力靠眼神承担，不靠面部肌肉
- 只输出提示词文本，不生图、不存档

## 边界与反例

**失败分支**：导演要场景/海报/分镜等非人物内容 → 说明本技能只做人物肖像并婉拒；导演指定组数（如「来 3 组」）→ 按指定数量输出，不凑 6 组；一次描述多个角色 → 每个角色独立走完整流程，面部基底与方案互不串用；导演指令与锁定模板冲突 → 以导演最新指令为准，并一句话说明改动点。

**反例黑名单**：❌ 向导演追问补信息 ❌ 6 组面部基底不一致（变成六个人）❌ 删改锁定模板的质感关键词 ❌ 堆砌标签代替连贯段落 ❌ 六组共用同一个眼神比喻 ❌ 使用非纯白背景（灰/黑/实景一律禁止） ❌ 明显表情（大笑/怒容/皱眉） ❌ 死人脸（眼神空洞无光）

## 示例

输入：「一个冷酷的女杀手，35 岁左右，东欧血统」

输出（节选，方案一）：

### 方案一｜冷峻禁欲
纯白背景半身正面照，以东欧女性为原型，35 岁左右，明星级长相，精致五官，黄金比例，极具辨识度：高颧骨与利落下颌线，狭长灰蓝色眼睛，冷静疏离。铂金色长发梳成一丝不苟的低髻。眼神直视镜头，如冰层下的游鲨，平静暗藏杀机。身穿剪裁锋利的黑色羊毛西装，衬衫扣到顶，零配饰。专业工作室柔光灯，柔和蝴蝶光刻画轮廓，面部光比极小，真人般的肌肤纹理与衣服质感，照片级真实感，拒绝 CG 感、3D 渲染感与塑料感，超高分辨率，大师作品。
> 设计说明：零碎发、零配饰，把冷酷外化为极致的秩序感。
"""


# ---- 海报模板兜底副本（与 film-poster-prompter skill 最后同步：2026-08-12；正常运行以 skill 文件为准）----
_FALLBACK_POSTER_PROMPT = """# 影视海报设计 Prompt 工程师

把剧本全文翻译成 8 组好莱坞电影海报文生图提示词：真人质感、戏剧冲突、标题醒目、严守平台安全区。

## 流程

1. **解析剧本**：提取剧名（英文名）、剧情基调、3~5 个关键人物（姓名、性格、身份、彼此关系）。
2. **标题定名**：主标题用剧本英文名替换模板中的 XXXXX 占位。文档没有英文名 → 你起一个符合基调的英文名，并在剧名行后标注（AI 起名）。
3. **生成 8 组方案**：6 组亮调、2 组暗调。每组一个构图方向（如：群像对峙 / 主角特写+群像剪影 / 三角关系 / 动作瞬间 / 象征意象 / 远景史诗），人物组合与站位必须体现性格、身份与人物关系。
4. **输出格式**：

```
## 剧名：{英文剧名}
**剧本基调（8 组共用）**：{一句话}
### 方案一｜{方向名}（亮调）
{提示词}
> 设计说明：{一句话}
```

每组提示词完整独立、可直接复制；亮调/暗调标注在方案名末尾括号里。

## 锁定元素（每组提示词必须包含）

```
好莱坞电影海报，9:16 竖版，真人实拍质感（拒绝 CG 感、3D 渲染感、游戏画面感），真实欧美人物，电影级布光，细腻震撼，充满戏剧冲突。{人物组合：姓名/外观/服装/站位/彼此关系}。主标题"XXXXX"以{符合剧本调性的光效与装饰元素}呈现，位置靠近画面视觉中心。{亮调：明亮高反差 / 暗调：低-key悬疑}。重要：主要人物与主标题整体位于画面垂直方向 15%~78% 的区间内，垂直方向尽可能居中；画面顶部 15% 与底部 22% 为平台裁剪区，严禁放置人物头部、标题与任何关键视觉元素。画面中禁止出现主标题以外的任何文字。超高分辨率，大师作品。
```

## 参考图（用户消息附带「可用参考图」清单时启用）

- 清单形如「参考图片1=杰克（男主）；参考图片2=艾琳（女主）」，编号与图片一一对应
- 有参考图的角色在提示词中出场时，角色名后必须紧跟限定词：（参考图片N），N 为清单编号——例如「杰克（参考图片1）站在画面左侧」
- 每组只引用该组实际出场角色的编号；没出场的不许引用；没有参考图的角色正常用文字描述外貌
- 编号不得超出清单范围

## 规范

- 每组 **250 字以内**，连贯段落、不堆标签；具体名词 > 抽象形容词
- 亮调 6 组在前、暗调 2 组在后；两组暗调构图方向不得重复亮调已用过的
- 只输出提示词文本，不生图、不存档

## 边界

- 剧本缺人物信息 → 按剧情合理补全，不追问
- 导演指令与本规范冲突 → 以导演最新指令为准，并一句话说明改动点
- 严格遵守输出格式，不要输出格式之外的多余内容
"""


class PromptGenError(Exception):
    pass


# ---------- 参考图编号工具（海报模式） ----------
_REF_RE = re.compile(r"参考图片?\s*(\d+)")


def extract_ref_numbers(prompt: str) -> list[int]:
    """按出现顺序提取「参考图片N」编号（去重；兼容 LLM 少写「片」字或带空格）。"""
    seen: list[int] = []
    for m in _REF_RE.finditer(prompt):
        n = int(m.group(1))
        if n not in seen:
            seen.append(n)
    return seen


def remap_refs(prompt: str) -> tuple[str, dict[int, int]]:
    """把提示词里的全局参考图编号重映射为组内序号（按全局编号升序排为 1..k）。

    单趟替换（re.sub 函数式），两位数编号不会被一位数替换污染。
    返回 (新提示词, {全局编号: 组内编号})。
    """
    used = extract_ref_numbers(prompt)
    mapping = {g: i + 1 for i, g in enumerate(sorted(set(used)))}

    def rep(m: re.Match) -> str:
        g = int(m.group(1))
        return f"参考图片{mapping[g]}" if g in mapping else m.group(0)

    return _REF_RE.sub(rep, prompt), mapping


def parse_schemes(text: str) -> dict:
    """把 skill 格式的输出解析为 {character, face_base, schemes:[{name, prompt, note}]}。

    容错：找不到标准标题时，退化为把全文作为单条提示词返回。
    """
    character = ""
    m = re.search(r"^##\s*(?:角色|剧名)[:：]\s*(.+)$", text, re.M)
    if m:
        character = m.group(1).strip()

    face_base = ""
    m = re.search(r"\*\*(?:面部基底|剧本基调)（[^）]*）\*\*[:：]\s*(.+)", text)
    if m:
        face_base = m.group(1).strip()

    schemes = []
    # 匹配「### 方案一｜方向名」开头的段落
    blocks = re.split(r"^###\s+", text, flags=re.M)
    for block in blocks[1:]:
        lines = block.strip().splitlines()
        if not lines:
            continue
        header = lines[0].strip()  # 形如「方案一｜冷峻禁欲」
        name = header.split("｜", 1)[1].strip() if "｜" in header else header
        body_lines, note = [], ""
        for line in lines[1:]:
            s = line.strip()
            if s.startswith(">"):
                note = s.lstrip("> ").replace("设计说明：", "").replace("设计说明:", "").strip()
            elif s:
                body_lines.append(s)
        prompt = "\n".join(body_lines).strip()
        if prompt:
            schemes.append({"name": name, "prompt": prompt, "note": note})

    if not schemes:
        # 兜底：模型没按格式输出时，全文作为单条方案
        cleaned = text.strip()
        if cleaned:
            schemes.append({"name": "原始输出", "prompt": cleaned, "note": "模型未按模板输出，已原样返回"})
    return {"character": character, "face_base": face_base, "schemes": schemes}


async def _call_llm(system_prompt: str, user_msg: str, model: str | None) -> dict:
    """LLM 调用核心（两个模式共用）：失败重试 3 次（指数退避），401 直接抛。"""
    cfg = config.llm_config(model)
    if not cfg["api_key"]:
        raise PromptGenError("未找到 AIOnly API key（config.yaml / 环境变量 / Cherry Studio 都没有）")

    client = AsyncOpenAI(base_url=cfg["base_url"], api_key=cfg["api_key"], timeout=90)

    last_err: Exception | None = None
    for attempt, wait in enumerate([0, 2, 5, 10]):
        if wait:
            await asyncio.sleep(wait)
        try:
            resp = await client.chat.completions.create(
                model=cfg["model"],
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_msg},
                ],
                temperature=0.8,
            )
            text = resp.choices[0].message.content or ""
            result = parse_schemes(text)
            result["model"] = cfg["model"]
            result["raw"] = text
            return result
        except Exception as e:  # 429/5xx/网络错误都重试；最后一次抛出
            last_err = e
            status = getattr(e, "status_code", None)
            if status == 401:
                raise PromptGenError(f"API key 无效（401），请检查 AIOnly 配置：{e}") from e
    raise PromptGenError(f"LLM 调用失败（已重试 3 次）：{last_err}")


async def generate_schemes(description: str, model: str | None = None, count: int = 6) -> dict:
    """调用 AIOnly LLM，把角色描述转成 N 组造型提示词。"""
    user_msg = f"角色描述：{description}\n\n请按流程输出 {count} 组造型方案。"
    return await _call_llm(load_system_prompt(), user_msg, model)


async def generate_poster_schemes(script_text: str, model: str | None = None, refs: list[dict] | None = None) -> dict:
    """剧本全文 → 8 组海报提示词（前 6 组亮调、后 2 组暗调）。

    refs：参考图清单 [{"name": "杰克（男主）"}...]，列表顺序即全局编号。
    """
    ref_lines = ""
    if refs:
        listing = "；".join(f"参考图片{i + 1}={r['name']}" for i, r in enumerate(refs))
        ref_lines = (
            f"\n\n可用参考图（共 {len(refs)} 张）：{listing}。"
            "有参考图的角色在提示词中出场时，角色名后必须紧跟（参考图片N）限定词，N 为上面的编号；"
            "每组只引用该组实际出场角色的编号；没有参考图的角色正常用文字描述外貌。"
        )
    user_msg = f"剧本全文：\n{script_text}{ref_lines}\n\n请按流程输出 8 组海报提示词（前 6 组亮调、后 2 组暗调）。"
    return await _call_llm(load_poster_prompt(), user_msg, model)


def _cli():
    description = sys.argv[1] if len(sys.argv) > 1 else "一个冷酷的女杀手，35 岁左右，东欧血统"
    model = sys.argv[2] if len(sys.argv) > 2 else None
    result = asyncio.run(generate_schemes(description, model))
    print(f"角色：{result['character']}  （模型：{result['model']}）")
    print(f"面部基底：{result['face_base']}\n")
    for i, s in enumerate(result["schemes"], 1):
        print(f"--- 方案{i}｜{s['name']} ---")
        print(s["prompt"])
        if s["note"]:
            print(f"> 设计说明：{s['note']}")
        print()


if __name__ == "__main__":
    _cli()
