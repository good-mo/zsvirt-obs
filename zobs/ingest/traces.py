"""分布式链路采集：Span 构造与跨 VM 采样器。

原型支持跨虚拟机/容器的请求链路（gateway -> Agent -> 嵌入/推理服务），
为延迟类根因提供证据。
"""

from __future__ import annotations

import random
import uuid
from typing import Any, Dict, Iterable, List, Optional

from .. import util
from ..store import Storage


def new_trace_id() -> str:
    return uuid.uuid4().hex[:16]


def new_span_id() -> str:
    return uuid.uuid4().hex[:8]


def make_span(trace_id: str, span_id: str, parent: str, service: str, op: str,
              start_ms: float, dur_ms: float, status: str = "ok",
              node: str = "", tags: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return {
        "trace_id": trace_id, "span_id": span_id, "parent": parent,
        "service": service, "op": op, "start_ms": start_ms, "dur_ms": dur_ms,
        "status": status, "node": node, "tags": tags or {},
    }


class TraceSampler:
    """按路由生成分布式链路。start 为 epoch 秒。"""

    def __init__(self, store: Storage) -> None:
        self.store = store

    def emit(self, spans: List[Dict[str, Any]]) -> None:
        self.store.record_trace(spans)

    def request(self, ts: float, route: List[Tuple[str, str, str, str]],
                base_ms: float = 200.0, jitter: float = 0.3,
                fail: Optional[str] = None, slow: Optional[str] = None) -> List[Dict[str, Any]]:
        """route: [(service, op, node, vm)]，按序构成父子 span。

        fail: 置为 error 的 service；slow: 放大时延的 service。
        """
        trace_id = new_trace_id()
        spans: List[Dict[str, Any]] = []
        start = ts * 1000.0
        acc = 0.0
        prev = ""
        n = len(route)
        for i, (service, op, node, vm) in enumerate(route):
            dur = base_ms * random.uniform(1 - jitter, 1 + jitter)
            if slow and service == slow:
                dur *= random.uniform(4, 8)
            span_id = new_span_id()
            status = "error" if (fail and service == fail) else "ok"
            spans.append(make_span(
                trace_id, span_id, prev, service, op, start + acc, dur, status,
                node=node, tags={"vm": vm}))
            acc += dur
            prev = span_id
        # 根 span 修正 start
        if spans:
            spans[0]["start_ms"] = start
        self.emit(spans)
        return spans
