"""剪映专业版驱动（一期：半自动兜底）。

目标交互链（二期全自动，装好剪影后现场联调）：
打开剪映 → 图片放入时间轴 → 画面-基础-AI扩展 → 导出到 review_done/。
二期用 macOS 辅助功能（AX）实现自动点击，本模块接口签名保持不变，上层零改动。
"""
from __future__ import annotations

import subprocess
from pathlib import Path

APP_CANDIDATES = ("剪映专业版", "剪映", "CapCut")


def is_installed() -> bool:
    return any((Path("/Applications") / f"{n}.app").exists() for n in APP_CANDIDATES)


def send_to_expand(pending_dir: Path) -> bool:
    """打开待扩图文件夹；装了剪映则顺带拉起剪映。返回剪映是否已安装。"""
    subprocess.run(["open", str(pending_dir)], check=False)
    if is_installed():
        for n in APP_CANDIDATES:
            if (Path("/Applications") / f"{n}.app").exists():
                subprocess.run(["open", "-a", n], check=False)
                break
        return True
    return False
