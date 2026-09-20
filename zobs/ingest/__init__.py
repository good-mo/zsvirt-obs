"""多信号采集：指标 / 日志 / 事件 / 分布式链路 的采集器与解析器。

原型环境下采集器既支持“结构化构造”（演示生成），也提供“文本解析”
（面向真实 ZSvirt / 容器运行时接入的形态）。
"""

from . import events  # noqa: F401
from . import logs  # noqa: F401
from . import metrics  # noqa: F401
from . import traces  # noqa: F401

__all__ = ["events", "logs", "metrics", "traces"]
