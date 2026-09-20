"""zsvirt-obs：面向 ZSvirt 虚拟机内部 AI 工作负载的可观测性原型。

核心思想：
- 虚拟机为边界（VM as boundary）：所有信号都挂到“VM -> 工作负载”的归属树上；
- 工作负载为对象（Workload as object）：资源限制、GPU 使用、推理/Agent 状态的识别；
- 事件关联为核心（Event correlation as core）：ZSvirt 告警、VM 异常与应用事件
  关联成可解释的根因候选、影响范围与处置建议。
"""

__version__ = "0.1.0"
