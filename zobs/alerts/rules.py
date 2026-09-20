"""告警规则：阈值规则 + 异常检测规则。

规则作用于存储中的最新指标与事件，产生 AlertRecord（由 AlertManager 聚合去重）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from ..config import DEFAULT_CONFIG, Config
from ..store import Alert, Storage
from ..util import fingerprint as _fp


@dataclass
class Rule:
    name: str
    severity: str                 # warn | crit
    node_kind: str                # 适用节点类型
    metric: str = ""              # 空则走事件规则
    op: str = ">"                 # > >= < <=
    value: float = 0.0
    for_samples: int = 1          # 连续样本数（原型中取最新值判定）
    event_type: str = ""          # 事件规则：匹配的事件类型
    message: str = ""
    runbook: str = ""

    def evaluate_metric(self, store: Storage, node: str, cfg: Config) -> Optional[Alert]:
        hit = store.latest_metric(node, self.metric)
        if not hit:
            return None
        ts, v = hit
        ok = (v > self.value) if self.op == ">" else \
             (v >= self.value) if self.op == ">=" else \
             (v < self.value) if self.op == "<" else (v <= self.value)
        if not ok:
            return None
        labels = {"alert_type": self.name, "node": node, "metric": self.metric}
        return Alert(fingerprint=_fp("alert", self.name, node, self.metric),
                     alert_type=self.name, severity=self.severity, node=node,
                     status="firing", count=1, first_ts=ts, last_ts=ts,
                     message=self.message.format(node=node, metric=self.metric, value=v),
                     labels=labels)

    def evaluate_event(self, store: Storage, node: str, t0: float, t1: float,
                       cfg: Config) -> Optional[Alert]:
        evs = store.events(t0=t0, t1=t1, node=node, kinds=[self.event_type])
        if not evs:
            return None
        ev = evs[-1]
        labels = {"alert_type": self.name, "node": node, "event": self.event_type}
        return Alert(fingerprint=_fp("alert", self.name, node, self.event_type),
                     alert_type=self.name, severity=self.severity, node=node,
                     status="firing", count=len(evs), first_ts=evs[0].ts, last_ts=ev.ts,
                     message=self.message.format(node=node, event=self.event_type,
                                                 count=len(evs)),
                     labels=labels)


# 默认规则集
def default_rules() -> List[Rule]:
    return [
        # ---- 物理/平台层
        Rule("gpu_ecc_threshold", "crit", "gpu", metric="gpu.ecc_rate", op=">=",
             value=DEFAULT_CONFIG.w("gpu.ecc_rate_crit"),
             message="GPU {node} ECC 错误率 {value:.2e} 超临界阈值，疑似硬件故障",
             runbook="gpu_ecc"),
        Rule("gpu_temp_critical", "crit", "gpu", metric="gpu.temp_c", op=">=",
             value=DEFAULT_CONFIG.w("gpu.temp_c_crit"),
             message="GPU {node} 温度 {value:.0f}℃ 超临界",
             runbook="gpu_temp"),
        Rule("zsvirt_vgpu_degraded", "warn", "vgpu", event_type="zsvirt.vgpu_degraded",
             message="vGPU {node} 健康状态降级（{event}）",
             runbook="vgpu_degraded"),
        Rule("zsvirt_host_mem_pressure", "warn", "host", event_type="zsvirt.host_mem_pressure",
             message="宿主机 {node} 内存压力事件",
             runbook="host_mem"),
        Rule("zsvirt_host_down", "crit", "host", event_type="zsvirt.host_down",
             message="宿主机 {node} 不可达",
             runbook="host_down"),
        # ---- VM 层
        Rule("vm_guest_crash", "crit", "vm", event_type="zsvirt.guest_crash",
             message="VM {node} Guest OS 崩溃",
             runbook="vm_crash"),
        Rule("vm_mem_usage_high", "warn", "vm", metric="vm.mem_usage", op=">=",
             value=DEFAULT_CONFIG.w("mem_usage"),
             message="VM {node} 内存使用率 {value:.1%} 偏高",
             runbook="vm_resources"),
        Rule("vm_watchdog", "warn", "vm", event_type="zsvirt.vm_watchdog",
             message="VM {node} 看门狗超时",
             runbook="vm_watchdog"),
        # ---- 容器层
        Rule("ctn_oom_killed", "crit", "container", event_type="ctn.oom_killed",
             message="容器 {node} 被 OOM Killer 终止",
             runbook="container_oom"),
        Rule("ctn_crash_loop", "crit", "container", event_type="ctn.crash_loop",
             message="容器 {node} CrashLoopBackOff",
             runbook="container_oom"),
        Rule("ctn_restart_high", "warn", "container", metric="ctn.restarts", op=">=",
             value=2.0,
             message="容器 {node} 重启次数 {value:.0f} 达到阈值",
             runbook="container_restart"),
        Rule("ctn_mem_usage_crit", "crit", "container", metric="ctn.mem_usage", op=">=",
             value=DEFAULT_CONFIG.w("mem_usage_crit"),
             message="容器 {node} 内存使用率 {value:.1%} 超临界（有 OOM 风险）",
             runbook="container_oom"),
        Rule("ctn_evicted", "crit", "container", event_type="ctn.evicted",
             message="容器 {node} 因资源不足被驱逐",
             runbook="container_oom"),
        # ---- Agent/服务层
        Rule("agent_error_rate_crit", "crit", "agent", metric="agent.error_rate", op=">=",
             value=DEFAULT_CONFIG.w("error_rate_crit"),
             message="服务 {node} 错误率 {value:.1%} 超临界",
             runbook="latency_error"),
        Rule("agent_p95_slo", "warn", "agent", metric="agent.p95_ms", op=">=",
             value=2000.0,
             message="服务 {node} P95 延迟 {value:.0f}ms 接近/超过 SLO",
             runbook="latency_error"),
        Rule("agent_queue_full", "warn", "agent", metric="agent.queue", op=">=",
             value=DEFAULT_CONFIG.w("queue_warn"),
             message="服务 {node} 请求队列 {value:.0f} 堆积",
             runbook="queue"),
        Rule("agent_model_load_failed", "crit", "agent", event_type="agent.model_load_failed",
             message="服务 {node} 模型加载失败",
             runbook="model_load"),
    ]


# 异常检测规则：直接由 AlertManager 对关键指标调用 z-score
ANOMALY_RULES: List[Dict[str, Any]] = [
    {"name": "anomaly_metric", "severity": "warn",
     "metrics": ["gpu.temp_c", "gpu.ecc_rate", "agent.error_rate", "agent.p95_ms", "vm.mem_usage"]},
]


def rule_for_metric(metric: str) -> Optional[Rule]:
    for r in default_rules():
        if r.metric == metric and r.node_kind == "agent":
            return r
    return None
