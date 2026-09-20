"""事件采集：ZSvirt 告警 / VM 异常 / 容器事件 / Agent 任务事件。

事件是关联诊断的核心输入。统一为 Signal(kind="event")，
name 为事件类型，payload 携带标题、详情、来源与附加标签。
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from ..store import Signal

# 事件类型注册表：类型 -> (默认级别, 说明, 一般来源)
EVENT_REGISTRY: Dict[str, tuple] = {
    # ---- ZSvirt 平台层
    "zsvirt.gpu_ecc": ("warn", "物理 GPU ECC 错误率超阈值", "ZSvirt"),
    "zsvirt.gpu_temp_high": ("warn", "物理 GPU 温度过高", "ZSvirt"),
    "zsvirt.vgpu_degraded": ("warn", "vGPU 健康状态降级", "ZSvirt"),
    "zsvirt.vgpu_alloc_failed": ("warn", "vGPU 分配失败", "ZSvirt"),
    "zsvirt.host_mem_pressure": ("warn", "宿主机内存压力", "ZSvirt"),
    "zsvirt.host_down": ("crit", "宿主机不可达", "ZSvirt"),
    "zsvirt.vm_migrate_failed": ("warn", "虚拟机热迁移失败", "ZSvirt"),
    "zsvirt.vm_watchdog": ("warn", "虚拟机看门狗超时", "ZSvirt"),
    "zsvirt.vm_balloon": ("warn", "内存气球驱动异常", "ZSvirt"),
    "zsvirt.guest_crash": ("crit", "Guest OS 崩溃", "ZSvirt"),
    # ---- 容器层
    "ctn.oom_killed": ("crit", "容器被 OOM Killer 终止", "container-runtime"),
    "ctn.restart": ("warn", "容器重启", "container-runtime"),
    "ctn.crash_loop": ("crit", "容器 CrashLoopBackOff", "container-runtime"),
    "ctn.healthcheck_fail": ("warn", "健康检查失败", "container-runtime"),
    "ctn.image_pull_fail": ("warn", "镜像拉取失败", "container-runtime"),
    "ctn.evicted": ("crit", "容器因资源被驱逐", "kubelet"),
    "ctn.ready": ("info", "容器就绪", "container-runtime"),
    "ctn.start": ("info", "容器启动", "container-runtime"),
    # ---- Agent / 应用层
    "agent.request_timeout": ("warn", "推理请求超时", "inference"),
    "agent.error_rate_spike": ("warn", "错误率突增", "inference"),
    "agent.p95_violation": ("warn", "P95 延迟违反 SLO", "inference"),
    "agent.queue_full": ("warn", "请求队列打满", "gateway"),
    "agent.task_failed": ("warn", "Agent 任务失败", "agent"),
    "agent.model_load_failed": ("crit", "模型加载失败", "inference"),
    "agent.task_started": ("info", "Agent 任务开始", "agent"),
    "agent.task_scheduled": ("info", "Agent 任务调度", "agent"),
    # ---- 运维
    "ops.maintenance": ("info", "计划维护", "ops"),
}

ALIAS_EVENT = {k: v[0] for k, v in EVENT_REGISTRY.items()}


def make_event(ts: float, kind: str, node: str,
               severity: Optional[str] = None,
               title: str = "", detail: str = "",
               source: str = "zsvirt",
               labels: Optional[Dict[str, str]] = None) -> Signal:
    """构造统一事件信号。"""
    if kind not in EVENT_REGISTRY:
        raise ValueError(f"未知事件类型: {kind}，可用: {sorted(EVENT_REGISTRY)}")
    default_sev, default_title, _ = EVENT_REGISTRY[kind]
    sev = severity or default_sev
    sig = Signal(kind="event", ts=ts, node=node, severity=sev, name=kind,
                 payload={
                     "title": title or default_title,
                     "detail": detail,
                     "source": source,
                     "labels": labels or {},
                 })
    return sig


def event_text(sig: Signal) -> str:
    """事件单行文本（用于诊断叙事与面板）。"""
    p = sig.payload
    labels = p.get("labels") or {}
    extra = f" [{', '.join(f'{k}={v}' for k, v in labels.items())}]" if labels else ""
    return f"[{sig.name}] {p.get('title', '')}{extra} @ {sig.node}"
