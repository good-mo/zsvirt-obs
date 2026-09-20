"""告警运营：评估、聚合（去重计数）、静默、自动恢复。

评估流程（每次周期调用）：
1. 遍历规则 × 节点，产出 Alert（含阈值规则与事件规则）；
2. 异常检测规则（z-score）补充异常告警；
3. 按指纹去重/聚合：同指纹持续触发 -> count+1、last_ts 更新；
4. 应用静默规则：命中 -> 状态置为 silenced（带原因）；
5. 自动恢复：条件消失且超过 throttle 周期 -> resolved。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from .. import util
from ..config import DEFAULT_CONFIG, Config, SILENCE_DEFAULT_SEC
from ..model import Topology
from ..store import Alert, SilenceRule, Storage
from .rules import ANOMALY_RULES, Rule, default_rules
from .runbook import suggest_for_alert


class AlertManager:
    def __init__(self, store: Storage, topo: Topology, cfg: Config = DEFAULT_CONFIG):
        self.store = store
        self.topo = topo
        self.cfg = cfg
        self.rules: List[Rule] = default_rules()

    # ------------------------------------------------------------- 评估
    def evaluate(self, now: Optional[float] = None, window: float = 300.0) -> Dict[str, int]:
        now = now or util.now()
        t0 = now - window
        firing: List[Alert] = []

        for nid, node in self.topo.nodes.items():
            # 1) 规则
            for rule in self.rules:
                if rule.node_kind != node.kind:
                    continue
                a = None
                if rule.metric:
                    a = rule.evaluate_metric(self.store, nid, self.cfg)
                elif rule.event_type:
                    a = rule.evaluate_event(self.store, nid, t0, now, self.cfg)
                if a:
                    a.suggestion = "\n".join(suggest_for_alert(rule.runbook))
                    firing.append(a)
            # 2) 异常检测
            for ar in ANOMALY_RULES:
                for metric in ar["metrics"]:
                    hit = self._anomaly(nid, metric, now)
                    if not hit:
                        continue
                    z, val, mu = hit
                    a = Alert(fingerprint=util.fingerprint("anomaly", nid, metric, now // self.cfg.metric_interval),
                              alert_type="anomaly_metric", severity=ar["severity"], node=nid,
                              status="firing", count=1, first_ts=now, last_ts=now,
                              message=f"指标异常：{nid} {metric} 当前 {val:.3g} 偏离基线 {mu:.3g} (z={z:.1f})",
                              labels={"alert_type": "anomaly_metric", "node": nid, "metric": metric},
                              suggestion="查看该节点指标趋势与关联诊断，确认是否由底层故障引起。")
                    firing.append(a)

        # 3) 去重/聚合入库（先补全租户标签，供静默/聚合/面板使用）
        tenants_of = self.topo.tenants_of
        for a in firing:
            if not a.labels.get("tenant"):
                ts = tenants_of(a.node)
                if ts:
                    a.labels["tenant"] = ",".join(sorted(ts))
        created = updated = 0
        for a in firing:
            existing = self._find_firing(a.fingerprint)
            if existing and a.last_ts - existing.last_ts < self.cfg.throttle:
                a.count = existing.count + 1
                a.first_ts = existing.first_ts
                a.id = existing.id
                a = self.store.upsert_alert(a, increment=True)
                updated += 1
            else:
                self.store.upsert_alert(a)
                created += 1

        # 4) 应用静默（重新评估全部 active 告警）
        silenced = self._apply_silences(now)

        # 5) 自动恢复
        fired = {a.fingerprint for a in firing}
        resolved = self._auto_resolve(fired, now)

        return {"created": created, "updated": updated, "silenced": silenced,
                "resolved": resolved, "firing_now": len(firing)}

    def _find_firing(self, fingerprint: str) -> Optional[Alert]:
        for a in self.store.list_alerts(limit=5000):
            if a.fingerprint == fingerprint and a.status in ("firing", "aggregated"):
                return a
        return None

    def _anomaly(self, node: str, metric: str, now: float):
        from ..detect import anomaly
        return anomaly(self.store, node, metric, now, self.cfg)

    def _apply_silences(self, now: float) -> int:
        rules = self.store.active_silences(now)
        if not rules:
            return 0
        n = 0
        for a in self.store.list_alerts(limit=5000):
            if a.status not in ("firing", "aggregated", "silenced"):
                continue
            for r in rules:
                if r.matches(a):
                    if a.status != "silenced":
                        self.store.update_alert(a.id, status="silenced")
                        n += 1
                    break
        return n

    def _auto_resolve(self, fired_fps: set, now: float) -> int:
        ids = []
        for a in self.store.list_alerts(limit=5000):
            if a.status in ("firing", "aggregated") and a.fingerprint not in fired_fps:
                if now - a.last_ts > self.cfg.throttle * 2:
                    ids.append(a.id)
        return self.store.resolve_alerts(ids, now)

    # ------------------------------------------------------------- 静默管理
    def silence(self, match: Optional[Dict[str, str]] = None, severity: str = "",
                tenant: str = "", duration_sec: float = SILENCE_DEFAULT_SEC,
                reason: str = "") -> SilenceRule:
        rule = SilenceRule(match=match or {}, severity=severity, tenant=tenant,
                           duration_sec=duration_sec, reason=reason or "运维静默")
        rule = self.store.add_silence(rule)
        # 立即生效（以数据时钟为准，演示场景时间戳为固定纪元）
        self._apply_silences(self.store.max_ts())
        return rule

    def unsilence(self, rule_id: int) -> bool:
        ok = self.store.delete_silence(rule_id)
        if ok:
            # 把仍在触发的 silenced 告警恢复为 firing（下次 evaluate 会重新判定）
            for a in self.store.list_alerts(status="silenced", limit=5000):
                self.store.update_alert(a.id, status="firing")
        return ok

    def list_silences(self, active_only: bool = True, now: Optional[float] = None) -> List[SilenceRule]:
        return self.store.list_silences(active_only=active_only, now=now)

    # ------------------------------------------------------------- 查询
    def list(self, status: Optional[str] = None, severity: Optional[str] = None,
             node: Optional[str] = None, alert_type: Optional[str] = None,
             limit: int = 500) -> List[Alert]:
        return self.store.list_alerts(status=status, severity=severity, node=node,
                                      alert_type=alert_type, limit=limit)

    def group_summary(self) -> Dict[str, Any]:
        """按状态/严重级汇总（面板用）。"""
        alerts = self.store.list_alerts(limit=10000)
        out: Dict[str, Any] = {"total": len(alerts), "by_status": {}, "by_severity": {},
                               "aggregated_total": 0}
        for a in alerts:
            out["by_status"][a.status] = out["by_status"].get(a.status, 0) + 1
            out["by_severity"][a.severity] = out["by_severity"].get(a.severity, 0) + 1
            out["aggregated_total"] += a.count
        return out
