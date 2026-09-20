"""事件关联与根因诊断引擎。

流程：
1. 收集证据：ZSvirt/vm/容器/Agent 事件 + 未恢复告警 + 指标异常 + 工作负载识别结果；
2. 时间聚类：按时间间隔把共现证据聚成 incident 簇；
3. 根因评分：结合层级（优先物理/上游）、严重度、时间先序、直接命中与下游影响，
   给出带置信度的根因候选与备选假设；
4. 影响范围：沿资源关联图求受影响 VM/容器/服务/租户及其状态；
5. 生成可解释叙事与处置建议（来自 runbook、链路证据）。
"""

from __future__ import annotations

import itertools
import time as _time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from .. import util
from ..config import DEFAULT_CONFIG, Config, MAX_ROOT_CANDIDATES, SEVERITY_NUM
from ..detect import WorkloadAnalyzer, WorkloadState
from ..ingest.events import event_text
from ..model import Topology
from ..store import Alert, Storage
from ..alerts.runbook import suggest_for_cluster

# 参与异常检测的关键指标（按节点类型）
ANOMALY_METRICS = {
    "gpu": ["gpu.temp_c", "gpu.ecc_rate", "gpu.util"],
    "vgpu": ["vgpu.util"],
    "vm": ["vm.cpu_usage", "vm.mem_usage"],
    "container": ["ctn.mem_usage", "ctn.cpu_usage"],
    "agent": ["agent.error_rate", "agent.p95_ms", "agent.queue", "agent.req_rate"],
    "host": ["host.mem_usage"],
}

EVIDENCE_WEIGHT = {"event": 1.0, "alert": 1.1, "anomaly": 0.9, "workload": 0.8}


@dataclass
class Evidence:
    kind: str            # event | alert | anomaly | workload
    ts: float
    node: str
    severity: str
    source: str
    payload: Dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> Dict[str, Any]:
        return {"kind": self.kind, "ts": self.ts, "node": self.node,
                "severity": self.severity, "source": self.source, "payload": self.payload}


@dataclass
class Candidate:
    node: str
    kind: str
    score: float
    confidence: float
    first_ts: float
    reasons: List[str] = field(default_factory=list)

    def to_json(self) -> Dict[str, Any]:
        return {"node": self.node, "kind": self.kind, "score": round(self.score, 3),
                "confidence": round(self.confidence, 3), "first_ts": self.first_ts,
                "reasons": self.reasons}


@dataclass
class Diagnosis:
    id: str
    t0: float
    t1: float
    severity: str
    title: str
    root: Optional[Candidate]
    candidates: List[Candidate]
    impact: Dict[str, Any]
    evidence: List[str]
    narrative: str
    suggestions: List[str]
    alert_ids: List[int]
    cluster_size: int

    def to_json(self) -> Dict[str, Any]:
        return {"id": self.id, "t0": self.t0, "t1": self.t1, "severity": self.severity,
                "title": self.title, "root": self.root.to_json() if self.root else None,
                "candidates": [c.to_json() for c in self.candidates],
                "impact": self.impact, "evidence": self.evidence, "narrative": self.narrative,
                "suggestions": self.suggestions, "alert_ids": self.alert_ids,
                "cluster_size": self.cluster_size}


class CorrelationEngine:
    def __init__(self, store: Storage, topo: Topology, cfg: Config = DEFAULT_CONFIG):
        self.store = store
        self.topo = topo
        self.cfg = cfg
        self.analyzer = WorkloadAnalyzer(store, topo, cfg)

    # ------------------------------------------------------------- 证据收集
    def collect(self, end_ts: float, window: float) -> List[Evidence]:
        t0 = end_ts - window
        evs: List[Evidence] = []
        active_silences = self.store.active_silences(end_ts)
        self.skipped_evidence = 0

        def silenced(node: str, name: str, labels: Dict[str, str]) -> bool:
            """命中生效中静默规则的证据不参与关联（告警降噪）。

            规则匹配键 alert_type 与事件名做“去分隔符包含”比较，
            使维护窗口规则（如 ctn_restart_high）也能覆盖同源事件（ctn.restart）。
            """
            pseudo = Alert(fingerprint="", alert_type=name, severity="", node=node,
                           labels=labels)
            for r in active_silences:
                if r.severity or r.tenant:
                    # 事件证据无级别标签时按节点匹配即可（有级别/租户约束的规则交给告警层）
                    pass
                hit = True
                for k, v in r.match.items():
                    if k == "node":
                        if v != node:
                            hit = False
                            break
                    elif k == "alert_type":
                        a = v.replace("_", "").replace(".", "")
                        b = name.replace("zsvirt.", "").replace("_", "").replace(".", "")
                        if a not in b and b not in a:
                            hit = False
                            break
                    elif labels.get(k) != v:
                        hit = False
                        break
                if hit:
                    return True
            return False

        # 1) 事件（warn 以上，跳过被静默覆盖的）
        for sig in self.store.events(t0=t0, t1=end_ts, limit=10000):
            if SEVERITY_NUM.get(sig.severity, 0) >= 2:
                if silenced(sig.node, sig.name, sig.payload.get("labels", {})):
                    self.skipped_evidence += 1
                    continue
                evs.append(Evidence("event", sig.ts, sig.node, sig.severity,
                                    event_text(sig), {"name": sig.name}))

        # 2) 未恢复告警（静默状态不参与关联诊断）
        for a in self.store.list_alerts(limit=1000):
            if a.status == "resolved" or a.status == "silenced":
                continue
            if a.last_ts < t0 - self.cfg.corr_gap:
                continue
            evs.append(Evidence("alert", a.last_ts, a.node, a.severity,
                                f"[告警:{a.alert_type}] {util.truncate(a.message, 80)}",
                                {"alert_id": a.id, "name": a.alert_type}))

        # 3) 指标异常（滚动 z-score）
        for nid, node in self.topo.nodes.items():
            for metric in ANOMALY_METRICS.get(node.kind, []):
                hit = self._anomaly(nid, metric, end_ts)
                if hit:
                    z, val, mu = hit
                    evs.append(Evidence("anomaly", end_ts, nid, "warn",
                                        f"{metric} 异常: 当前 {val:.3g} vs 基线 {mu:.3g} (z={z:.1f})",
                                        {"name": metric}))

        # 4) 工作负载识别出的关键问题
        states = self.analyzer.analyze(end_ts, window)
        for nid, st in states.items():
            if st.grade == "crit":
                first = st.issues[0] if st.issues else None
                evs.append(Evidence("workload", end_ts, nid, "crit",
                                    f"{nid}({st.kind}) 状态={st.state}"
                                    + (f"，{first.text}" if first else ""),
                                    {"name": "workload_state"}))
        evs.sort(key=lambda e: e.ts)
        return evs

    def _anomaly(self, node: str, metric: str, now: float) -> Optional[Tuple[float, float, float]]:
        from ..detect import anomaly
        return anomaly(self.store, node, metric, now, self.cfg)

    # ------------------------------------------------------------- 聚类
    def cluster(self, evs: List[Evidence], gap: float) -> List[List[Evidence]]:
        if not evs:
            return []
        groups: List[List[Evidence]] = []
        cur = [evs[0]]
        for e in evs[1:]:
            if e.ts - cur[-1].ts <= gap:
                cur.append(e)
            else:
                groups.append(cur)
                cur = [e]
        groups.append(cur)
        # 合并：间隔略大于 gap 但属于同一故障演化（如告警快照扎堆于评估时刻）的簇
        merged: List[List[Evidence]] = [groups[0]]
        for g in groups[1:]:
            prev = merged[-1]
            if g[0].ts - prev[-1].ts <= gap * 2:
                merged[-1] = prev + g
            else:
                merged.append(g)
        return merged

    # ------------------------------------------------------------- 诊断
    def diagnose_all(self, end_ts: Optional[float] = None, window: Optional[float] = None,
                     min_cluster: int = 2) -> List[Diagnosis]:
        end = end_ts if end_ts is not None else self._max_ts()
        win = window or self.cfg.corr_window
        evs = self.collect(end, win)
        out: List[Diagnosis] = []
        for group in self.cluster(evs, self.cfg.corr_gap):
            if len(group) < min_cluster and not any(e.kind == "alert" for e in group):
                # 孤立低危事件不构成 incident；有告警的孤立簇也诊断（单个故障）
                continue
            diag = self._diagnose_group(group, end, win)
            if diag:
                out.append(diag)
        out.sort(key=lambda d: SEVERITY_NUM.get(d.severity, 0), reverse=True)
        return out

    def diagnose_alert(self, alert_id: int, end_ts: Optional[float] = None,
                       window: Optional[float] = None) -> Optional[Diagnosis]:
        a = self.store.get_alert(alert_id)
        if not a:
            return None
        window = window or self.cfg.corr_window
        end = end_ts if end_ts is not None else max(a.last_ts, self._max_ts())
        diags = self.diagnose_all(end_ts=end, window=window)
        # 找包含该告警的诊断
        for d in diags:
            if alert_id in d.alert_ids:
                return d
        for d in diags:
            if a.node in {c.node for c in d.candidates}:
                return d
        return diags[0] if diags else None

    def _max_ts(self) -> float:
        with self.store._lock:  # pragma: no cover - 内部访问
            row = self.store._conn.execute("SELECT MAX(ts) FROM signals").fetchone()
        return row[0] or _time.time()

    # ------------------------------------------------------------- 单簇诊断
    def _diagnose_group(self, group: List[Evidence], now: float, win: float) -> Diagnosis:
        members = sorted(group, key=lambda e: e.ts)
        t0 = members[0].ts
        t1 = members[-1].ts
        sev = max((e.severity for e in members), key=lambda s: SEVERITY_NUM.get(s, 0))
        cands = self._root_candidates(members)
        root = cands[0] if cands else None
        impact = self._impact_scope(root, members) if root else {}
        alert_ids, alert_types = self._cluster_alerts(members)
        evidence_lines = [f"{util.fmt_ts(e.ts)} [{e.kind}/{e.severity}] {e.node} {e.source}"
                          for e in members[:40]]
        title = self._title(root, members, alert_types)
        suggestions = suggest_for_cluster(alert_types, [c.node for c in cands], self.topo)
        narrative = self._narrative(members, root, cands, impact, t0, t1)
        skipped = getattr(self, "skipped_evidence", 0)
        if skipped:
            narrative += f"\n（告警降噪：已按生效静默规则排除 {skipped} 条维护事件/重复告警证据）"
        diag = Diagnosis(
            id=util.fingerprint("diag", t0, title, root.node if root else ""),
            t0=t0, t1=t1, severity=sev, title=title,
            root=root, candidates=cands, impact=impact,
            evidence=evidence_lines, narrative=narrative, suggestions=suggestions,
            alert_ids=alert_ids, cluster_size=len(members))
        # 回写告警的关联分组（告警聚合证据链）
        if alert_ids:
            gid = diag.id
            for aid in alert_ids:
                self.store.update_alert(aid, correlation_group=gid,
                                        status=self.store.get_alert(aid).status if self.store.get_alert(aid) else "firing")
        return diag

    def _root_candidates(self, members: List[Evidence]) -> List[Candidate]:
        """候选集 = 出现证据的节点 + 其上溯物理/平台层节点，按启发式打分。"""
        member_nodes = {e.node for e in members}
        cands_all: Set[str] = set()
        for nid in member_nodes:
            cands_all.add(nid)
            cands_all |= self.topo.ancestors(nid, kinds={"host", "gpu", "vgpu", "vm"})
        if not cands_all:
            return []
        t_min = min(e.ts for e in members)
        t_max = max(e.ts for e in members)
        span = max(t_max - t_min, 1e-6)
        max_layer = max(self.topo.layer_of(c) for c in cands_all)
        scored: List[Tuple[float, Candidate]] = []
        for c in cands_all:
            direct = [e for e in members if e.node == c]
            downstream = self.topo.propagate(c)
            hitting_down = [e for e in members if e.node in downstream]
            if not (direct or hitting_down):
                continue
            # 严重度
            sev_n = max((SEVERITY_NUM.get(e.severity, 0) for e in direct), default=0) / 3.0
            # 时间先序：下游最早证据越早 -> 越像源
            first_down = min((e.ts for e in hitting_down), default=t_max)
            prec = 1.0 - (first_down - t_min) / span
            # 直接命中密度
            max_direct = max(len([e for e in members if e.node == x]) for x in cands_all) or 1
            spec = len(direct) / max_direct
            # 层级：有直接证据且层越低（越物理）越像根
            layer = self.topo.layer_of(c)
            layer_bonus = (1.0 - layer / max(1, max_layer)) if direct else 0.0
            # 下游影响面（命中簇内下游的比例）
            fanout = len(downstream & member_nodes) / max(1, len(member_nodes))
            score = (0.32 * sev_n + 0.22 * prec + 0.16 * spec + 0.18 * layer_bonus + 0.12 * fanout)
            reasons = []
            if direct:
                reasons.append(f"直接证据 {len(direct)} 条（{max((e.kind for e in direct), key=lambda k: EVIDENCE_WEIGHT.get(k,1))}）")
            if layer_bonus > 0:
                reasons.append(f"位于{self.topo.nodes[c].kind}层，故障向上游传播特征明显")
            if prec > 0.6:
                reasons.append("时间上早于下游故障")
            if fanout > 0.3:
                reasons.append(f"下游影响覆盖 {fanout*100:.0f}% 簇内对象")
            scored.append((score, Candidate(node=c, kind=self.topo.nodes[c].kind, score=score,
                                            confidence=min(1.0, score * 1.15),
                                            first_ts=first_down, reasons=reasons)))
        scored.sort(key=lambda x: (-x[0], x[1].first_ts))
        return [c for _, c in scored[:MAX_ROOT_CANDIDATES]]

    def _impact_scope(self, root: Candidate, members: List[Evidence]) -> Dict[str, Any]:
        scope = self.topo.impact_scope(root.node)
        states = self.analyzer.analyze(members[-1].ts if members else root.first_ts,
                                       self.cfg.corr_window)
        detail: List[Dict[str, Any]] = []
        nodes = set(scope["vms"]) | set(scope["containers"]) | set(scope["agents"])
        for nid in sorted(nodes):
            st = states.get(nid)
            detail.append({"node": nid, "kind": self.topo.nodes[nid].kind,
                           "grade": st.grade if st else "?",
                           "state": st.state if st else ""})
        member_nodes = {e.node for e in members}
        extra = sorted(member_nodes - nodes - {root.node})
        return {"root": root.node, "vms": scope["vms"], "containers": scope["containers"],
                "agents": scope["agents"], "tenants": scope["tenants"],
                "nodes": detail, "related": extra}

    def _cluster_alerts(self, members: List[Evidence]) -> Tuple[List[int], List[str]]:
        ids: List[int] = []
        types: List[str] = []
        for e in members:
            if e.kind == "alert":
                if e.payload.get("alert_id"):
                    ids.append(int(e.payload["alert_id"]))
                if e.payload.get("name"):
                    types.append(str(e.payload["name"]))
        return sorted(set(ids)), sorted(set(types))

    def _title(self, root: Optional[Candidate], members: List[Evidence],
               alert_types: List[str]) -> str:
        sev = max((e.severity for e in members), key=lambda s: SEVERITY_NUM.get(s, 0))
        label = {"crit": "严重", "warn": "警告", "info": "提示"}.get(sev, sev)
        if root and root.kind in ("gpu", "host", "vgpu"):
            return f"{label}故障：{root.node}({root.kind}) 疑似硬件/平台异常波及 AI 工作负载"
        if root:
            return f"{label}故障：{root.node}({root.kind}) 异常影响下游 AI 工作负载"
        return f"{label}事件簇：{len(members)} 条证据共现"

    # ------------------------------------------------------------- 叙事
    def _narrative(self, members: List[Evidence], root: Optional[Candidate],
                   cands: List[Candidate], impact: Dict[str, Any],
                   t0: float, t1: float) -> str:
        lines: List[str] = []
        head = f"时间窗 {util.fmt_ts(t0)}~{util.fmt_ts(t1)}，共关联 {len(members)} 条证据。"
        if root:
            paths: List[str] = []
            for tgt in impact.get("agents", [])[:3]:
                p = self.topo.path(root.node, tgt)
                if p:
                    paths.append(" → ".join(p))
            if paths:
                head += " 关联链路：" + "；".join(paths[:2]) + "."
        lines.append(head)
        if root:
            lines.append(f"根因候选：{root.node}（{root.kind}），置信度 {root.confidence:.2f}。"
                         f"依据：{'；'.join(root.reasons)}。")
            for alt in cands[1:3]:
                why = "备选假设" if alt != root else ""
                lines.append(f"{why}：{alt.node}（{alt.kind}，置信度 {alt.confidence:.2f}）。")
        if impact.get("tenants"):
            lines.append(f"影响范围：租户 {'/'.join(impact['tenants'])}；"
                         f"虚拟机 {len(impact['vms'])} 台、容器 {len(impact['containers'])} 个、"
                         f"AI 服务/Agent {len(impact['agents'])} 个受影响。")
        for nd in impact.get("nodes", [])[:6]:
            lines.append(f"  - {nd['node']} [{nd['kind']}] {nd['grade']}/{nd['state']}")
        for e in members[:6]:
            lines.append(f"  · {util.fmt_ts(e.ts)} {e.node}: {e.source}")
        return "\n".join(lines)


def render_diagnosis(diag: Diagnosis) -> str:
    """终端文本渲染。"""
    sev_c = util.sev_color(diag.severity)
    out = [util.paint(f"◆ {diag.title}", sev_c, bold=True)]
    out.append(diag.narrative)
    out.append(util.paint("处置建议", util.Color.BLUE, bold=True))
    for i, s in enumerate(diag.suggestions, 1):
        out.append(f"  {i}. {s}")
    return "\n".join(out)
