"""通用工具：时间格式化、指纹、统计与 ANSI 输出。"""

from __future__ import annotations

import hashlib
import json
import math
import statistics
import sys
import time
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

# ---------------------------------------------------------------- 时间
def fmt_ts(ts: float) -> str:
    """epoch 秒 -> HH:MM:SS。"""
    t = time.gmtime(ts)
    return time.strftime("%H:%M:%S", t)


def fmt_dt(ts: float) -> str:
    t = time.gmtime(ts)
    return time.strftime("%Y-%m-%d %H:%M:%S", t)


def now() -> float:
    return time.time()


# ---------------------------------------------------------------- 指纹
def fingerprint(*parts: Any) -> str:
    raw = json.dumps(parts, ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------- 统计
def zscore(value: float, series: Sequence[float]) -> float:
    """相对基线序列的 z-score；序列不足或零方差时返回 0。"""
    if len(series) < 3:
        return 0.0
    mu = statistics.mean(series)
    sd = statistics.pstdev(series)
    if sd <= 1e-9:
        return 0.0 if abs(value - mu) < 1e-9 else 99.0
    return (value - mu) / sd


def trend(series: Sequence[Tuple[float, float]], window: int = 5) -> str:
    """最近趋势：rising|falling|flat。"""
    if len(series) < 2:
        return "flat"
    tail = [v for _, v in series[-window:]]
    if len(tail) < 2:
        return "flat"
    slope = tail[-1] - tail[0]
    span = max(abs(tail[0]), abs(tail[-1]), 1e-9)
    if slope / span > 0.03:
        return "rising"
    if slope / span < -0.03:
        return "falling"
    return "flat"


def pct(x: float) -> str:
    return f"{x * 100:.1f}%"


def merge_severity(a: str, b: str) -> str:
    order = {"ok": 0, "info": 1, "warn": 2, "crit": 3}
    return a if order.get(a, 0) >= order.get(b, 0) else b


# ---------------------------------------------------------------- 终端输出
class Color:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    BLUE = "\033[34m"
    MAGENTA = "\033[35m"
    CYAN = "\033[36m"

    @staticmethod
    def enable() -> bool:
        return sys.stdout.isatty()


def paint(text: str, color: str, bold: bool = False) -> str:
    if not Color.enable():
        return text
    c = (Color.BOLD if bold else "") + color
    return f"{c}{text}{Color.RESET}"


def sev_color(sev: str) -> str:
    return {"ok": Color.GREEN, "info": Color.CYAN, "warn": Color.YELLOW,
            "crit": Color.RED}.get(sev, Color.RESET)


def bar(value: float, width: int = 20, warn: float = 0.85, crit: float = 0.95) -> str:
    """ASCII 进度条。"""
    v = max(0.0, min(1.0, value))
    filled = int(round(v * width))
    body = "█" * filled + "░" * (width - filled)
    if v >= crit:
        c = Color.RED
    elif v >= warn:
        c = Color.YELLOW
    else:
        c = Color.GREEN
    return paint(body, c)


def table(headers: List[str], rows: List[List[str]], title: str = "") -> str:
    """轻量表格。"""
    if not rows:
        return paint(f"（无数据）{title}", Color.DIM)
    widths = [len(h) for h in headers]
    for r in rows:
        for i, cell in enumerate(r):
            widths[i] = max(widths[i], len(cell))
    sep = "+" + "+".join("-" * (w + 2) for w in widths) + "+"
    lines = [sep]
    hdr = "|" + "|".join(f" {h:<{widths[i]}} ") + "|"
    lines.append(hdr)
    lines.append(sep)
    for r in rows:
        lines.append("|" + "|".join(f" {c:<{widths[i]}} ") + "|")
    lines.append(sep)
    return "\n".join(lines)


def truncate(s: str, n: int = 60) -> str:
    return s if len(s) <= n else s[: n - 3] + "..."
