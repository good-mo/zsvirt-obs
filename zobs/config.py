"""全局配置：节点类型、层次、默认阈值与关联参数。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

# ---------------------------------------------------------------- 节点类型
# 资源关联模型的实体类型（自底向上：物理 -> 虚拟 -> 工作负载 -> 业务）
NODE_KINDS = ("host", "gpu", "vgpu", "vm", "container", "process", "agent", "tenant")

# 层次序号：越小越接近物理层（故障向上游传播时，'更低层'更可能是根因）
LAYER_ORDER: Dict[str, int] = {
    "host": 0,
    "gpu": 0,
    "vgpu": 1,
    "vm": 2,
    "container": 3,
    "process": 4,
    "agent": 5,
    "tenant": 6,
}

# 允许的关联边： (父类型, 子类型, 关系名, 说明)
EDGE_SCHEMA = (
    ("host", "gpu", "owns", "宿主机拥有物理 GPU"),
    ("gpu", "vgpu", "slices", "物理 GPU 切分出 vGPU"),
    ("host", "vm", "runs", "宿主机运行虚拟机"),
    ("vm", "vgpu", "binds", "虚拟机绑定 vGPU"),
    ("vm", "container", "runs", "虚拟机内运行容器"),
    ("container", "process", "runs", "容器内进程"),
    ("container", "agent", "provides", "容器提供 AI 服务/Agent"),
    ("vm", "agent", "runs", "虚拟机直接运行 Agent/服务"),
    ("tenant", "agent", "owns", "租户/项目拥有 Agent"),
    ("tenant", "vm", "owns", "租户/项目拥有虚拟机"),
    ("tenant", "host", "runs", "租户工作负载承载于宿主机"),
    ("agent", "agent", "depends_on", "服务间依赖"),
    ("vm", "vm", "peer", "虚拟机间网络对等（跨 VM 链路）"),
)

# ---------------------------------------------------------------- 信号类型
SIGNAL_KINDS = ("metric", "log", "event", "trace", "config")

SEVERITY_ORDER = ("ok", "info", "warn", "crit")
SEVERITY_NUM = {"ok": 0, "info": 1, "warn": 2, "crit": 3}


@dataclass
class Config:
    """原型运行参数；演示与检测逻辑共享。"""

    # 采集
    metric_interval: float = 15.0          # 指标采样间隔(秒)
    horizon: float = 600.0                 # 演示时间窗(秒)

    # 关联诊断
    corr_window: float = 420.0             # 诊断回看窗口(秒)
    corr_gap: float = 90.0                 # 同一 incident 内事件的最大时间间隔(秒)

    # 告警
    throttle: float = 120.0                # 同指纹告警再通知间隔(秒)
    auto_resolve_ok: int = 2               # 连续 N 次采样正常后自动恢复
    anomaly_z: float = 2.8                 # 异常检测 z-score 阈值
    anomaly_min_dev: float = 1e-4           # 异常的最小绝对偏差（相对基线量级兜底）
    anomaly_baseline: float = 180.0        # 异常检测基线窗口(秒)

    # 阈值（warn/crit）
    warn: Dict[str, float] = field(default_factory=lambda: {
        "gpu.temp_c": 80.0, "gpu.temp_c_crit": 88.0,
        "gpu.util_crit": 95.0,
        "gpu.ecc_rate_warn": 1e-4, "gpu.ecc_rate_crit": 1e-3,
        "cpu_usage": 0.85, "mem_usage": 0.85,
        "mem_usage_crit": 0.95,
        "error_rate": 0.02, "error_rate_crit": 0.06,
        "queue_warn": 50.0, "queue_crit": 300.0,
        "container_restarts_warn": 2.0, "container_restarts_crit": 4.0,
        "p95_slo_ratio_warn": 1.0, "p95_slo_ratio_crit": 1.5,
    })
    # 假定 SLO：p95 延迟阈值上限(ms)，Agent 未显式配置时使用
    default_slo_p95_ms: float = 2000.0

    def w(self, key: str) -> float:
        return self.warn.get(key, 0.0)


DEFAULT_CONFIG = Config()

# 静默默认时长(秒)
SILENCE_DEFAULT_SEC = 7200

# 诊断最大候选根因数
MAX_ROOT_CANDIDATES = 3
