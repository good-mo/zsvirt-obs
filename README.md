# zsvirt-obs

面向 **ZSvirt 虚拟机内 AI 工作负载**的可观测性原型：以**虚拟机为边界、工作负载为对象、事件关联为核心**，统一组织指标、日志、链路、配置与告警证据，输出**可解释的健康状态、影响范围与诊断建议**。

> 纯 Python 标准库实现（无第三方运行时依赖），自带演示场景与数据生成器，可通过 CLI 或 Web 可视化面板使用。

## 核心概念

- **资源关联模型**：`租户/项目 → 宿主机 → GPU/vGPU → 虚拟机 → 容器/进程 → AI 服务/Agent` 的多层资源图，含 11 类带方向语义的关联边（`owns / slices / runs / binds / provides / depends_on / peer`），是信号挂载与故障影响传播的骨架。
- **统一信号信封**：指标、日志、事件、链路、配置五类信号统一存储，按节点挂载（host/gpu/vgpu/vm/container/agent/tenant）。
- **事件关联诊断**：收集告警/事件/指标异常/工作负载状态 → 时间聚类 → 启发式根因打分（严重度、时间先序、直接命中密度、故障层级、下游影响面）→ 输出根因候选、影响范围、证据链与处置建议。
- **告警降噪运营**：指纹去重与聚合、阈值/事件/异常三类告警规则、静默规则（含维护窗口）、自动恢复、面向运维的 runbook 建议。

## 快速开始

```bash
# 1) 生成演示数据（600s 故障剧本 + 预评估告警）
python3 -m zobs.cli --dir data demo init
# 或安装后使用 console script：zobs --dir data demo init

# 2) 查看资源拓扑与工作负载健康
zobs --dir data status
zobs --dir data topo

# 3) 事件关联诊断
zobs --dir data diagnose

# 4) 告警运营
zobs --dir data alerts eval
zobs --dir data alerts list
zobs --dir data alerts silences
zobs --dir data alerts silence --match node=llm-infer-0 --reason="已知问题，待修复"

# 5) 其它信号
zobs --dir data events
zobs --dir data logs
zobs --dir data traces list
zobs --dir data traces show <trace_id>

# 6) Web 可视化面板
zobs --dir data web --port 8000
# 浏览器打开 http://127.0.0.1:8000
```

## 演示剧本

数据生成器（`zobs/demo/scenario.py`）按固定种子生成 10 分钟确定性故障剧本：

| 阶段 | 时间 | 事件 |
|---|---|---|
| P0 健康期 | 0~90s | 全栈正常运行，链路少量采样 |
| P1 GPU 异常 | 90~150s | GPU-0 ECC 错误率升高、温度上升 |
| P2 OOM | 150~210s | llm-infer-0 OOMKilled（170s），vGPU 降级、VM 气球告警 |
| P3 故障传播 | 210~330s | CrashLoop（3 次 OOM）、健康检查失败、推理 P95 超时、请求队列堆积、RAG 任务失败 |
| P4 平台压力+静默 | 330~450s | 宿主机内存压力；GPU-0 硬件更换窗口登记（静默 ECC 告警） |
| P5 持续降级 | 450~600s | 服务降级持续至观察窗口结束 |

另含**噪声剧本**：embed-svc 计划维护滚动重启（被静默规则降噪，不参与关联诊断）。

## 架构总览

```
                    ┌────────────────────────────────────────┐
                    │          Web 面板 (stdlib http)        │
                    │   总览 / 拓扑 / 告警 / 诊断 / 链路 / 日志 │
                    └───────▲──────────────────▲────────────┘
      CLI (argparse) ───────┼──────────────────┼── JSON API
                            │                  │
   ┌───────────┐   ┌────────┴──────┐   ┌───────┴────────┐
   │ 关联诊断   │   │  告警管理      │   │  工作负载识别   │
   │ Correlation│◄──│ AlertManager  │   │ WorkloadAnalyzer│
   │ Engine     │   │ 规则/去重/静默 │   │ 阈值/z-score    │
   └─────┬─────┘   └───────┬───────┘   └───────┬────────┘
         │                 │                   │
         └─────────────────┼───────────────────┘
                           ▼
              ┌────────────────────────┐
              │  Storage (SQLite)      │
              │ signals/traces/alerts/ │
              │ silence_rules/topology │
              └───────────┬────────────┘
                          ▲
              ingest: metrics / logs / events / traces / config
                          │
              demo scenario 数据生成器（确定性种子）
```

## 目录结构

```
zobs/
  config.py            全局配置（阈值、窗口、边 schema、层级）
  model.py             资源关联图（节点/边/影响传播/拓扑构建）
  store.py             SQLite 存储（信号/链路/告警/静默）
  util.py              工具（指纹、zscore、ANSI 表输出）
  ingest/              多信号采集（指标/日志/事件/链路 注册表与生成）
  detect/              工作负载识别与异常检测
  correlate/           事件关联与诊断（证据/聚类/打分/影响范围）
  alerts/              告警规则、runbook、生命周期管理
  demo/                演示场景与数据生成
  web/                 Web 面板（HTTP API + 单文件前端）
  cli.py               命令行入口
tests/                 unittest 测试（33 例）
```

## 测试

```bash
python3 -m unittest discover -s tests -v
```

覆盖：拓扑建模与传播方向、异常检测（含小量级指标）、聚类与根因打分、告警生命周期（静默/聚合/自动恢复）、端到端故障剧本。

## 设计说明

- **影响传播方向**：故障沿资源包含方向下行（GPU→vGPU→VM→容器→Agent），其中 `binds`（vGPU↔VM 绑定）与 `depends_on`（服务依赖）按反向传播，`peer`（VM 互备）双向传播；租户与宿主机为承载关系，故障不沿此上行。
- **时间一致**：演示数据使用固定纪元（2023-11-14 22:13:20），所有“当前时刻”均以信号最大时间戳为准，避免真实时钟与演示时钟混淆；用户在面板中创建静默时按真实时间记录。
- **告警降噪**：指纹（内容 hash）驱动去重与计数聚合；静默规则同时拦截**告警证据**与**同源事件证据**进入关联诊断（维护窗口示例）。

## 已知限制

- 对标量原型：并发/规模按演示级设计；SQLite 存储，未做采样与多写者。
- 根因打分为启发式权重，置信度用于排序而非严格概率。
- 链路采样器为演示用简化实现（固定路由 + 故障注入概率）。
