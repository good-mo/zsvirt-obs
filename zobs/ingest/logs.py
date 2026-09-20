"""日志采集：结构化日志构造 + 文本解析（面向真实接入形态）。

日志行格式（示例）：
  level=error ts=2025-01-01T00:05:03Z vm=vm-01 container=llm-infer-0
  svc=infer-7b msg="CUDA out of memory" oom_killed=true restart_count=2
解析结果统一为 Signal(kind="log")。
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from ..store import Signal

LEVEL_SEV = {"debug": "info", "info": "info", "warn": "warn", "warning": "warn",
             "error": "warn", "fatal": "crit", "critical": "crit"}

_KV_RE = re.compile(r'(\w[\w.]*)=("[^"]*"|\S+)')


def kv_parse(text: str) -> Dict[str, str]:
    """解析 'k=v k2="v v2"' 形式的键值对。"""
    out: Dict[str, str] = {}
    for m in _KV_RE.finditer(text):
        k, v = m.group(1), m.group(2)
        out[k] = v.strip('"')
    return out


def make_log(ts: float, node: str, level: str, msg: str,
             service: str = "", vm: str = "", container: str = "",
             fields: Optional[Dict[str, Any]] = None) -> Signal:
    """构造结构化日志信号。"""
    payload: Dict[str, Any] = {"level": level, "msg": msg, "service": service or node}
    if vm:
        payload["vm"] = vm
    if container:
        payload["container"] = container
    payload.update(fields or {})
    sev = LEVEL_SEV.get(level.lower(), "info")
    return Signal(kind="log", ts=ts, node=node, severity=sev, name=f"log.{level}",
                  payload=payload, raw=f"level={level} vm={vm} container={container} "
                                       f"svc={service} msg={msg!r}")


def parse_log_line(line: str, ts: float, default_node: str = "") -> Optional[Signal]:
    """解析纯文本日志行为结构化信号；解析失败返回 None。

    支持 close 风格 key=value 行。
    """
    if not line.strip():
        return None
    kv = kv_parse(line)
    if not kv:
        return None
    level = kv.get("level", "info")
    msg = kv.get("msg", " ".join(f"{k}={v}" for k, v in kv.items() if k not in ("level", "vm", "container", "svc", "ts")))
    node = kv.get("node") or (f"{kv.get('vm', default_node)}" if kv.get("vm") else default_node)
    sig = make_log(ts, node or default_node, level, msg,
                   service=kv.get("svc", ""), vm=kv.get("vm", ""),
                   container=kv.get("container", ""),
                   fields={k: v for k, v in kv.items()
                           if k not in ("level", "msg", "vm", "container", "svc", "ts", "node")})
    sig.raw = line
    return sig


def severity_of_log(level: str) -> str:
    return LEVEL_SEV.get(level.lower(), "info")


def log_highlights(sig: Signal) -> List[str]:
    """从日志中抽取关键词，供工作负载识别使用。"""
    p = sig.payload
    text = " ".join(str(v) for v in p.values()).lower()
    hits = []
    for kw in ("oom", "out of memory", "cuda error", "nccl", "timeout",
               "crash", "restart", "ecc", "unhealthy", "evict", "fail", "latency"):
        if kw in text:
            hits.append(kw)
    return hits
