"""指标采集：统一指标名注册与落库。

指标命名约定：<层>.<指标>，例如 gpu.util / vm.mem_usage / ctn.restarts / agent.p95_ms。
采集器把结构化样本写入 Storage；真实接入时仅需替换数据源。
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Tuple

from ..store import Signal, Storage

METRIC_REGISTRY: Dict[str, str] = {
    "host.cpu_usage": "宿主机 CPU 使用率",
    "host.mem_usage": "宿主机内存使用率",
    "gpu.util": "GPU 利用率 %",
    "gpu.mem_used_gb": "GPU 显存使用 GB",
    "gpu.temp_c": "GPU 温度 ℃",
    "gpu.ecc_rate": "GPU ECC 错误率",
    "gpu.power_w": "GPU 功耗 W",
    "vgpu.util": "vGPU 利用率 %",
    "vgpu.mem_used_gb": "vGPU 显存使用 GB",
    "vm.cpu_usage": "虚拟机 CPU 使用率",
    "vm.mem_usage": "虚拟机内存使用率",
    "vm.net_rx_mbps": "虚拟机网络接收 Mbps",
    "vm.net_tx_mbps": "虚拟机网络发送 Mbps",
    "ctn.cpu_usage": "容器 CPU 使用率",
    "ctn.mem_usage": "容器内存使用率",
    "ctn.mem_limit_gb": "容器内存上限 GB",
    "ctn.cpu_limit": "容器 CPU 上限(核)",
    "ctn.restarts": "容器重启次数",
    "agent.req_rate": "服务请求速率 rps",
    "agent.error_rate": "服务错误率",
    "agent.p95_ms": "服务 P95 延迟 ms",
    "agent.queue": "等待队列深度",
    "agent.tokens_per_s": "吞吐 tokens/s",
    "agent.progress": "训练进度 %",
}


def feed(store: Storage, ts: float, node: str, metric: str, value: float,
         scope: str = "") -> Signal:
    """写入一条指标样本。"""
    if metric not in METRIC_REGISTRY:
        # 未知指标也允许（保持灵活），仅在告警规则中引用时受限
        pass
    sig = Signal(kind="metric", ts=ts, node=node, severity="ok", name=metric,
                 value=value, scope=scope)
    store.record(sig)
    return sig


def feed_series(store: Storage, node: str, metric: str,
                samples: Iterable[Tuple[float, float]], scope: str = "") -> List[Signal]:
    """批量写入时间序列。"""
    sigs = [Signal(kind="metric", ts=ts, node=node, severity="ok", name=metric,
                   value=v, scope=scope) for ts, v in samples]
    store.record_many(sigs)
    return sigs
