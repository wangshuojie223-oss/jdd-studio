# 海报/角色模块独立化 + 一键清空参考图 设计（v1.6.0）

日期：2026-08-12
状态：已与杰确认（模型默认=config 改 flash）

## 问题

1. `modelSel` 下拉放在角色描述卡里，切到海报模式时随之隐藏——海报模式看不到/用不了 LLM 选择，且实际读的是这个隐藏共享下拉
2. 提示词修改窗口（schemeCard）两模式共用一张卡，视觉上「海报提示词跑进了角色模块」（状态虽分槽，但卡片共享）
3. 参考图只能逐张删，没有一键清空

## 改动

1. **模型选择独立**：海报区顶部加 `#posterModelSel`；两下拉各自 localStorage 记忆（`modelSel_char` / `modelSel_poster`），首次默认读 config（config.yaml `llm.model` 同步改为 `gemini-3.6-flash`，防 kimi-k3 超时）
2. **提示词窗口拆分**：`#schemeCard` → `#schemeCardChar`（面部基底+角色卡）与 `#schemeCardPoster`（剧本基调+资产卡），各归各模式显隐；`renderSchemes` 目标容器按模式取；工作区 slot 机制不变
3. **一键清空**：参考图行加「🧹 清空」按钮（confirm 确认）→ 新接口 `POST /api/refs/clear`：删全部条目+清单内图片+refs 根目录孤儿图片，**保留 roster.json**（角色表仍可复用）

## 测试

- TDD：test_refs 加 `test_clear_refs`（条目/图片清空、roster.json 保留、清单返回空）
- 前端无单测：启动冒烟两模式各切一遍
- 版本 1.5.0 → 1.6.0 四处
