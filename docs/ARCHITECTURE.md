# 架构设计说明

本文档说明 zsvirt-obs 的核心设计决策：资源关联建模、信号模型、关联诊断算法、告警生命周期与降噪机制。

## 1. 总体分层

```
┌────────────────────────────────────────────────────────────┐
│ 展示层  CLI / Web 面板（/api/* JSON）                       │
├────────────────────────────────────────────────────────────┤
│ 分析层  WorkloadAnalyzer（工作负载识别/异常检测）            │
│         AlertManager（规则触发/去重/静默/恢复）              │
│         CorrelationEngine（证据收集→聚类→根因打分→影响范围） │
├────────────────────────────────────────────────────────────┤
│ 存储层  Storage（SQLite）                                   │
│         signals / traces / alerts / silence_rules / topology│
├────────────────────────────────────────────────────────────┤
│ 采集层  ingest（metrics / logs / events / traces / config） │
└────────────────────────────────────────────────────────────┘
```

5 类信号统一为 `Signal(kind, ts, node, severity, name, value, payload, raw, scope)` 信封，写入同一张 `signals` 表；链路单独建表（瀑布展示与采样查询）。

## 2. 资源关联模型（Topology）

### 2.1 节点类型与层级

`LAYER_ORDER = [host, gpu, vgpu, vm, container, process, agent]`，另有一类特殊节点 `tenant`（归属/租户语义，不参与故障层级，仅作为影响范围聚合维度）。

### 2.2 关联边（EDGE_SCHEMA）

| 边 | 方向 | 语义 | 故障传播 |
|---|---|---|---|
| host `owns` gpu | 下行 | 宿主拥有物理 GPU | 下行 |
| gpu `slices` vgpu | 下行 | 物理 GPU 切分 | 下行 |
| host `runs` vm | 下行 | 宿主运行 VM | 下行 |
| vm `binds` vgpu | **vGPU→VM** | VM 绑定 vGPU | **反向传播**（卡故障打穿 VM）|
| vm `runs` container | 下行 | VM 内容器 | 下行 |
| container `runs` process | 下行 | 容器内进程 | 下行 |
| container `provides` agent | 下行 | 容器提供 AI 服务 | 下行 |
| vm `runs` agent | 下行 | VM 直跑 Agent | 下行 |
| tenant `owns` agent/vm | 归属 | 租户拥有 | 不传播（聚合用）|
| tenant `runs` host | 承载 | 租户工作负载承载于宿主 | 不传播（聚合用）|
| agent `depends_on` agent | 依赖 | 服务依赖 | **反向传播**（被依赖方故障→调用方）|
| vm `peer` vm | 对等 | VM 互备/跨 VM 链路 | **双向传播** |

`Topology.propagate(nid)`：从故障节点出发，沿「资源包含下行 + binds/depends_on/peer 反向」得到影响集；`impact_scope(nid)` 再将其收敛到 {vm, container, agent}，并把受影响的 VM 展开为其中全部工作负载，最后沿 tenant 归属边聚合出受影响租户。

## 3. 信号模型

- **指标 metrics**：数值采样，按 15s 间隔；`metric_series / latest_metric / recent_metrics` 供检测与面板拉取。
- **日志 logs**：文本行 + `level` 标签；面板按节点过滤展示，不做全文索引（原型）。
- **事件 events**：注册表驱动的类型化事件（`EVENT_REGISTRY`，约 30 类），含默认严重度与标题模板；事件是关联诊断的主证据。
- **链路 traces**：span 模型（trace_id/span_id/parent/service/op/dur/status/tags），`TraceSampler` 按企业服务拓扑注入采样与故障；`trace_summary` 输出总量、错误率、慢 span 占比。
- **配置 config**：节点属性信封（如容器 limits、Agent 配置版本）。

时钟约定：演示环境统一使用固定纪元 `T0=1700000000`（2023-11-14 22:13:20 UTC），全部“当前时刻”取 `Storage.max_ts()`（数据时钟），避免真实墙钟与演示时间错乱。

## 4. 工作负载识别与异常检测

`WorkloadAnalyzer.analyze(now, win)` 对每个节点输出 `WorkloadState`：

- **容器**：最近一次重启计数（state=Restarting）、内存使用率（`ctn.mem_usage` 相对 limits 的比率，≥0.85 warn / ≥0.95 crit）、CPU 使用率、OOM/健康检查/CrashLoop 事件汇入证据。
- **GPU/vGPU**：利用率、显存、温度、ECC 错误率；结合事件（`zsvirt.gpu_ecc`、`zsvirt.vgpu_degraded`）定级。
- **Agent**：请求量、错误率、P95 时延，按 SLO 目标（`agent.error_rate_crit`、`p95_slo`）判 warn/crit。
- **异常检测**：`anomaly(zs)` 以 `corr_window` 前 45% 为基线（避开故障段），z-score ≥ `anomaly_z` 且绝对偏差 ≥ `max(anomaly_min_dev, |μ|·0.25)` 判异常；`anomaly_min_dev=1e-4` 保证小量级指标（如 GPU ECC 错误率 1e-4~1e-3）也能被检测，同时防止基线为 0 时对噪声过敏感。

## 5. 事件关联与诊断（CorrelationEngine）

### 5.1 证据收集（collect）

输入窗口 `[end-window, end]`，收集四类证据：

1. **事件**：severity ≥ warn，且未被**生效静默规则**覆盖（降噪：维护窗口事件不污染诊断）；
2. **告警**：status ∈ {firing, aggregated}，且未被静默；
3. **指标异常**：z-score 判定为异常的节点/指标对；
4. **工作负载状态**：WorkloadAnalyzer 输出的 warn/crit 状态。

命中静默的证据记为 `skipped_evidence`，在结果叙事中说明“已降噪 N 条，不参与关联”，保证可解释性。

### 5.2 时间聚类（cluster）

按证据时间戳排序，相邻间隔 ≤ `corr_gap(90s)` 并簇；随后**合并 pass**：相邻两簇首尾间隔 ≤ `2×corr_gap` 时合并，以吸收评估时刻指标告警的扎堆延迟，避免同一事件被拆成多个 incident。

### 5.3 根因打分（_score / _root_candidates）

对每个候选节点 `x`（簇内直接证据节点 ∪ 其 ancestors{host,gpu,vgpu,vm}）计算：

```
score = 0.32·severity_n + 0.22·precedence + 0.16·specificity
      + 0.18·layer_bonus + 0.12·fanout
```

- `severity_n`：直接证据最大严重度归一；
- `precedence`：首个直接/下游证据的时间先序（越早越高）；
- `specificity`：直接命中证据数 / 簇内最大直接命中数；
- `layer_bonus`：按 LAYER_ORDER 自上而下递减（硬件层根因加分）；
- `fanout`：`propagate(x)` 覆盖簇内证据的比率（下游影响面）。

置信度 = 最高分与次高分之差按阀值映射；输出 `<MAX_ROOT_CANDIDATES>` 个候选 + 中文叙事 + 影响范围（租户/VM/容器/Agent）+ 证据链 + 处置建议（runbook 按关键字匹配）。

## 6. 告警生命周期与降噪

### 6.1 生命周期

```
firing ──(指纹去重, count++)──▶ firing(count=n)
  │  ▲
  │  │ 匹配静默规则
  ▼  └─────────────────────────▶ silenced
  │                                │
  └──(last_ts - first_ts ≥ 2×throttle 且状态不再触发)──▶ resolved
```

- **指纹**：`sha1(alert_type|node|关键 labels)`，同内容告警不重复建行，只累加计数；
- **静默**：`SilenceRule{match{node,alert_type,label…}, until_ts}`；`AlertManager.silence()` 立即生效（以数据时钟 `store.max_ts()` 为“现在”）；
- **恢复**：评估时若超 2×throttle 无新证据，自动置 resolved。

### 6.2 降噪策略

1. **聚合**：指纹合并 → 面板显示 count；
2. **静默**：告警级（不进入关联证据）+ 证据级（同源事件同样被跳过）；
3. **阈值/异常**：阈值规则针对稳定量级；z-score 异常针对小量级漂移；
4. **处置建议**：每个告警类型映射 runbook 步骤（`alerts/runbook.py`）。

## 7. Web 面板

`ThreadingHTTPServer` + 原生 JS 单页（无构建）：总览（统计卡、链路健康）、资源拓扑（SVG 分层图、点击节点看指标/事件）、告警（筛选表 + 静默表单）、诊断（incident 卡：叙事/置信度/影响范围/证据链/处置建议）、链路（瀑布图）、日志、事件。所有数据经 `/api/*.json`。

## 8. 扩展点

- **新信号源**：在 `ingest/` 注册类型与生成器，写入 `Signal` 即可进入统一管线；
- **新节点/边**：扩展 `EDGE_SCHEMA` 与 `build()`，传播与影响范围自动适配；
- **诊断策略**：替换 `CorrelationEngine._score` 权重或新增证据类型；
- **真实接入**：将 `demo/scenario.py` 替换为真实采集器（ZSvirt API、k8s、Agent SDK），存储层无需改动。
