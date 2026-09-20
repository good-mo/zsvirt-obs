"""运维处置建议库（runbook）。

按告警类型/节点类型给出面向运维人员的可执行建议；建议会结合关联诊断的
上下文节点（如故障实体、涉及的 VM/容器/服务）生成具体命令。
"""

from __future__ import annotations

from typing import Dict, Iterable, List

from ..model import Topology

RUNBOOKS: Dict[str, Dict[str, object]] = {
    "gpu_ecc": {
        "title": "GPU ECC 错误处理",
        "steps": [
            "确认故障 GPU 与受影响 vGPU/VM：`virsh nodedev-list | grep gpu`，比对关联模型中的 vGPU 切片",
            "查看 ECC 详情：`nvidia-smi -q -d ECC`（或对应驱动工具），区分可纠正/不可纠正错误",
            "评估影响：若不可纠正错误持续增长且下游延迟/错误率劣化，优先将受影响 VM 热迁移隔离：`virsh migrate --live <vm> <target-host>`",
            "若需更换/下线硬件：先按静默规则压制告警并通知租户，再执行下线流程；观察迁移后服务是否恢复",
            "排查驱动/固件：对比同型号正常 GPU 的驱动版本，必要时滚动升级",
        ],
    },
    "gpu_temp": {
        "title": "GPU 过热处理",
        "steps": [
            "检查散热与功耗：`nvidia-smi -q -d TEMPERATURE,POWER`，对比同卡温度基线",
            "确认是否伴随利用率打满：若有高负载推理任务，可限制并发或迁移部分副本到其他 vGPU",
            "温度持续超过临界阈值且无缓解时，按硬件告警流程处理（含租户通知与静默）",
        ],
    },
    "vgpu_degraded": {
        "title": "vGPU 降级处理",
        "steps": [
            "确认底层物理 GPU 状态（ECC/温度/利用率），vGPU 降级通常是上层故障",
            "检查绑定 VM 的 vGPU 驱动（guest 内 `npu-smi info` 类工具）是否报错",
            "必要时热迁移 VM 以切换到健康 vGPU/GPU：`virsh migrate --live <vm> <target>`",
        ],
    },
    "host_mem": {
        "title": "宿主机内存压力",
        "steps": [
            "查看宿主机内存与页缓存：`free -h`、`vmstat 1`，确认是否因 VM 膨胀导致",
            "定位内存占用较高的 VM：`virsh dominfo <vm>`、`virsh dommemstat <vm>`",
            "对气球驱动异常的 VM 重启气球服务或调整内存目标；必要时迁移 VM 平衡负载",
        ],
    },
    "host_down": {
        "title": "宿主机不可达",
        "steps": [
            "确认网络与带外管理通道（BMC/IPMI），`ping` + `virsh list --all`",
            "检查 HA/自愈策略是否触发：受影响 VM 是否已在其他宿主机拉起",
            "若 VM 未自动迁移，按应急预案手动恢复；恢复后核对数据一致性与租户 SLA",
        ],
    },
    "vm_crash": {
        "title": "Guest OS 崩溃",
        "steps": [
            "获取 Guest 崩溃转储：`virsh dump <vm> <path>`，检查 /var/log/messages",
            "核对是否与 vGPU 直通/热迁移/内核驱动相关（对比崩溃时间与平台事件）",
            "恢复 VM 后建议开启看门狗并配置崩溃自动重启策略",
        ],
    },
    "vm_resources": {
        "title": "VM 资源高水位",
        "steps": [
            "定位高占用来源：guest 内 `top`/`/proc/meminfo`，容器维度看 cgroup stat",
            "评估是否扩容 vCPU/内存规格（需停机或热加内存/CPU：`virsh setmem|setvcpus --live`）",
            "检查是否有内存泄漏：观察 mem_usage 趋势是否单调上升（配合异常检测）",
        ],
    },
    "vm_watchdog": {
        "title": "VM 看门狗",
        "steps": [
            "确认 Guest 内核/驱动是否 hang：串口控制台 `virsh console` 尝试获取栈",
            "看门狗连续触发说明 Guest 无响应，评估强制重启对业务的影响窗口",
            "核对是否由底层 GPU/宿主机异常导致（结合关联诊断证据链）",
        ],
    },
    "container_oom": {
        "title": "容器 OOM / 重启",
        "steps": [
            "查看重启原因与退出码：容器运行时 `podman inspect <ctr>` 或 k8s `kubectl describe pod/<pod>` 的 Last State",
            "核对容器 limits 与真实使用：内存超限则调高 limit，或定位内存泄漏（结合指标趋势与日志）",
            "检查是否因下层 GPU/VM 内存不足触发（关联 ECC/宿主机压力事件）",
            "对推理服务建议配置优雅重启与请求重试，规避 OOM 期间的 5xx",
        ],
    },
    "container_restart": {
        "title": "容器频繁重启",
        "steps": [
            "统计重启次数增长速率，判断是否 CrashLoop（<1 分钟内多次）",
            "查看最近一次启动日志定位根因（配置错误/依赖不可用/资源不足）",
            "稳定后可考虑更新 restart_policy 与就绪探针参数",
        ],
    },
    "latency_error": {
        "title": "延迟/错误率劣化",
        "steps": [
            "用分布式链路定位慢调用：按 trace 查看各 span 耗时，区分排队、模型推理、网络与后置服务",
            "核对 GPU 是否争用：vGPU 利用率、并发请求、batch size 等",
            "若伴随底层故障（ECC/温度/容器 OOM）按对应 runbook 处理；否则评估扩容副本或限流",
            "确认错误类型：超时/5xx/推理异常分别对应网关重试、容量规划、模型/驱动排查",
        ],
    },
    "queue": {
        "title": "请求队列堆积",
        "steps": [
            "确认队列积压是否随请求突发（与 req_rate 对比），或由下游慢导致（看链路）",
            "临时手段：限流/熔断保护、增加副本数、扩容 vGPU",
            "长期：为队列设置上限与降级策略（拒绝新请求 vs 排队等待）",
        ],
    },
    "model_load": {
        "title": "模型加载失败",
        "steps": [
            "查看加载日志与显存：模型权重大小 vs vGPU 显存，显存不足需换更大切片",
            "检查模型文件完整性/存储挂载是否异常",
            "热加载失败建议回滚到上一版本镜像并灰度重试",
        ],
    },
}

DEFAULT_RUNBOOK = {
    "title": "通用排查",
    "steps": [
        "以关联诊断的根因实体为中心，沿证据链逐层确认（物理→虚拟→容器→服务）",
        "查看告警聚合与静默状态，确认是否已有运维处置进行中",
        "处置后观察指标/错误率/延迟是否收敛，再关闭告警（勿在根因未明时直接断言）",
    ],
}


def suggest_for_cluster(alert_types: Iterable[str], nodes: List[str],
                        topo: Topology) -> List[str]:
    """为诊断簇生成处置建议（按告警类型对应的 runbook 展开）。"""
    out: List[str] = []
    seen_books: List[str] = []
    for at in alert_types:
        book = None
        for key, rb in RUNBOOKS.items():
            if key in at or at in key:
                book = rb
                break
        if not book:
            continue
        if book["title"] in seen_books:
            continue
        seen_books.append(book["title"])
        for step in book["steps"]:
            node_hint = nodes[0] if nodes else "?"
            out.append(f"【{book['title']}】步骤：{step.replace('<vm>', node_hint)}")
    if not out:
        out = [f"{s}" for s in DEFAULT_RUNBOOK["steps"]]
    return out[:8]


def suggest_for_alert(alert_type: str) -> List[str]:
    book = RUNBOOKS.get(alert_type, DEFAULT_RUNBOOK)
    return [f"【{book['title']}】{s}" for s in book["steps"]][:6]
