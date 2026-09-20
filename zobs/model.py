"""资源关联模型：宿主机 - GPU/vGPU - 虚拟机 - 容器/进程 - AI 服务/Agent - 租户/项目。

以有向图表达实体与关联，提供祖先/后代/影响范围等查询，支撑
“虚拟机为边界、工作负载为对象”的信号挂载与事件关联。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from .config import EDGE_SCHEMA, LAYER_ORDER, NODE_KINDS


@dataclass
class Node:
    id: str
    kind: str                       # host|gpu|vgpu|vm|container|process|agent|tenant
    name: str = ""
    props: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.name:
            self.name = self.id
        if self.kind not in NODE_KINDS:
            raise ValueError(f"未知节点类型: {self.kind}")

    def label(self, key: str, default: Any = None) -> Any:
        return self.props.get(key, default)

    def to_json(self) -> Dict[str, Any]:
        return {"id": self.id, "kind": self.kind, "name": self.name, "props": self.props}


@dataclass
class Edge:
    src: str
    dst: str
    rel: str
    props: Dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> Dict[str, Any]:
        return {"src": self.src, "dst": self.dst, "rel": self.rel, "props": self.props}


class Topology:
    """资源关联图。信号节点按 id 挂载；边决定故障传播与影响范围。"""

    def __init__(self) -> None:
        self.nodes: Dict[str, Node] = {}
        # out_edges: src -> [(dst, rel)]
        self.out: Dict[str, List[Tuple[str, str]]] = {}
        # in_edges: dst -> [(src, rel)]
        self.inn: Dict[str, List[Tuple[str, str]]] = {}

    # ------------------------------------------------------------- 构建
    def add_node(self, node: Node) -> "Topology":
        self.nodes[node.id] = node
        self.out.setdefault(node.id, [])
        self.inn.setdefault(node.id, [])
        return self

    def add_edge(self, src: str, dst: str, rel: str, props: Optional[Dict[str, Any]] = None,
                 strict: bool = True) -> "Topology":
        if src not in self.nodes or dst not in self.nodes:
            raise KeyError(f"边端点不存在: {src} -> {dst}")
        if strict:
            allowed = [(a, b) for a, b, r, _ in EDGE_SCHEMA if r == rel]
            if not allowed:
                raise ValueError(f"未知关系: {rel}")
            if (self.nodes[src].kind, self.nodes[dst].kind) not in allowed:
                raise ValueError(f"关系 {rel} 不适用于 {self.nodes[src].kind}->{self.nodes[dst].kind}，允许: {allowed}")
        self.out.setdefault(src, []).append((dst, rel))
        self.inn.setdefault(dst, []).append((src, rel))
        return self

    def node(self, nid: str) -> Optional[Node]:
        return self.nodes.get(nid)

    def require(self, nid: str) -> Node:
        if nid not in self.nodes:
            raise KeyError(f"节点不存在: {nid}")
        return self.nodes[nid]

    # ------------------------------------------------------------- 查询
    def children(self, nid: str, rel: Optional[str] = None) -> List[Tuple[str, str]]:
        out = []
        for dst, r in self.out.get(nid, []):
            if rel is None or r == rel:
                out.append((dst, r))
        return out

    def parents(self, nid: str, rel: Optional[str] = None) -> List[Tuple[str, str]]:
        out = []
        for src, r in self.inn.get(nid, []):
            if rel is None or r == rel:
                out.append((src, r))
        return out

    def descendants(self, nid: str, kinds: Optional[Iterable[str]] = None,
                    max_depth: Optional[int] = None) -> Set[str]:
        """向下游传播可达的节点集合（沿资源包含方向）。"""
        kinds = set(kinds) if kinds else None
        seen: Set[str] = set()
        stack: List[Tuple[str, int]] = [(nid, 0)]
        while stack:
            cur, depth = stack.pop()
            if max_depth is not None and depth >= max_depth:
                continue
            for dst, _ in self.children(cur):
                if dst in seen:
                    continue
                if kinds is None or self.nodes[dst].kind in kinds:
                    seen.add(dst)
                stack.append((dst, depth + 1))
        return seen

    def ancestors(self, nid: str, kinds: Optional[Iterable[str]] = None) -> Set[str]:
        """向上游可达的节点集合；kinds 过滤返回的类型（仍沿全图遍历）。"""
        kinds = set(kinds) if kinds else None
        seen: Set[str] = set()
        kept: Set[str] = set()
        stack = list(self.parents(nid))
        while stack:
            src, _ = stack.pop()
            if src in seen:
                continue
            seen.add(src)
            if kinds is None or self.nodes[src].kind in kinds:
                kept.add(src)
            for p, _ in self.parents(src):
                stack.append((p, _))
        return kept

    def propagate(self, nid: str, max_depth: Optional[int] = None) -> Set[str]:
        """故障影响传播集合（含自节点）。

        影响沿资源包含方向下行，但两类边反向传播：
        - vgpu -> vm（vGPU 故障影响绑定它的 VM）
        - 被依赖方 -> 依赖方（embed-384 故障影响依赖它的 rag-svc）
        """
        def imp_children(x: str) -> List[str]:
            out: List[str] = []
            for dst, rel in self.children(x):
                if rel == "binds":       # vm(父) binds vgpu(子) -> 故障从 vgpu 传到 vm
                    out.append(dst)
                elif rel == "depends_on":  # 依赖方(父) -> 被依赖方(子)；故障反向
                    out.append(dst)
                else:
                    out.append(dst)
            for src, rel in self.parents(x):
                if rel == "binds":       # 反向：vgpu -> vm
                    out.append(src)
                elif rel == "depends_on":  # 反向：被依赖方 -> 依赖方
                    out.append(src)
                elif rel == "peer":      # peer 双向（互为主备，任一故障牵连对方）
                    out.append(src)
            return list(dict.fromkeys(out))

        seen: Set[str] = {nid}
        stack: List[Tuple[str, int]] = [(nid, 0)]
        while stack:
            cur, depth = stack.pop()
            if max_depth is not None and depth >= max_depth:
                continue
            for dst in imp_children(cur):
                if dst in seen:
                    continue
                seen.add(dst)
                stack.append((dst, depth + 1))
        return seen

    def upstream(self, nid: str) -> Set[str]:
        """物理层方向（向上游传播的物理/基础设施实体）。"""
        return self.ancestors(nid)

    def path(self, a: str, b: str) -> Optional[List[str]]:
        """BFS 找 a -> b 的一条关联路径（用于生成可解释链路）。"""
        if a == b:
            return [a]
        prev: Dict[str, str] = {a: ""}
        queue = [a]
        while queue:
            cur = queue.pop(0)
            for dst, _ in self.children(cur):
                if dst not in prev:
                    prev[dst] = cur
                    if dst == b:
                        p = [b]
                        while p[-1] != a:
                            p.append(prev[p[-1]])
                        return list(reversed(p))
                    queue.append(dst)
        return None

    def tenants_of(self, nid: str) -> Set[str]:
        return self.ancestors(nid, kinds={"tenant"})

    def subtree(self, nid: str) -> Set[str]:
        """影响范围：节点本身 + 下游（默认不含租户；租户单独聚合）。"""
        return self.descendants(nid) | {nid}

    def impact_scope(self, nid: str) -> Dict[str, Any]:
        """给定故障实体，返回受影响的工作负载对象与租户（含 VM 边界展开）。"""
        sub = self.propagate(nid)
        agents = {x for x in sub if self.nodes[x].kind == "agent"}
        containers = {x for x in sub if self.nodes[x].kind == "container"}
        vms = {x for x in sub if self.nodes[x].kind == "vm"}
        if self.nodes[nid].kind == "vm":
            vms.add(nid)
        # 虚拟机为边界：一旦 VM 受影响，其内全部工作负载视为受影响
        for vm in list(vms):
            if self.nodes[vm].kind != "vm":
                continue
            workloads = self.descendants(vm, kinds={"container", "agent"})
            for w in workloads:
                if self.nodes[w].kind == "container":
                    containers.add(w)
                else:
                    agents.add(w)
        tenants: Set[str] = set()
        for x in agents | containers | vms | {nid}:
            tenants |= self.tenants_of(x)
        return {"agents": sorted(agents), "containers": sorted(containers),
                "vms": sorted(vms), "tenants": sorted(tenants)}

    def find_all(self, kind: str) -> List[Node]:
        return [n for n in self.nodes.values() if n.kind == kind]

    # ------------------------------------------------------------- 序列化
    def to_json(self) -> Dict[str, Any]:
        return {
            "nodes": [n.to_json() for n in self.nodes.values()],
            "edges": [e.to_json() for e in self.edges()],
            "schema": [list(e) for e in EDGE_SCHEMA],
        }

    def edges(self) -> List[Edge]:
        out = []
        for src, lst in self.out.items():
            for dst, rel in lst:
                out.append(Edge(src, dst, rel))
        return out

    @classmethod
    def from_json(cls, data: Dict[str, Any]) -> "Topology":
        t = cls()
        for nd in data["nodes"]:
            t.add_node(Node(**nd))
        for e in data["edges"]:
            t.add_edge(e["src"], e["dst"], e["rel"], strict=False)
        return t

    # ------------------------------------------------------------- 展示
    def tree_text(self, root: str = "") -> str:
        """打印 ASCII 拓扑树。默认从所有 host 开始（也可从任意节点）。"""
        roots = [root] if root else sorted(n.id for n in self.find_all("host"))
        if not roots:
            roots = sorted(self.nodes)
        lines: List[str] = []

        def emit(nid: str, indent: str, prefix: str) -> None:
            node = self.nodes[nid]
            lines.append(f"{indent}{prefix}# {node.id} [{node.kind}] {node.name}")
            kids = sorted(self.children(nid))
            for i, (kid, rel) in enumerate(kids):
                last = i == len(kids) - 1
                lines.append(f"{indent}{'    ' if last else '│   '}└─({rel}) ")
                emit(kid, indent + ("    " if last else "│   "), "")
        for r in roots:
            emit(r, "", "")
        return "\n".join(lines)

    def layer_of(self, nid: str) -> int:
        return LAYER_ORDER.get(self.nodes[nid].kind, 9)


def build() -> Topology:
    """构建默认原型拓扑。"""
    t = Topology()

    def node(nid: str, kind: str, props: Optional[Dict[str, Any]] = None) -> Node:
        n = Node(nid, kind, props=props or {})
        t.add_node(n)
        return n

    # 物理层
    node("h1", "host", {"cpu_cores": 64, "mem_gb": 256, "os": "HCE 2.0", "region": "dc-1"})
    node("h2", "host", {"cpu_cores": 64, "mem_gb": 256, "os": "HCE 2.0", "region": "dc-1"})
    node("gpu-0", "gpu", {"model": "Ascend 910B", "mem_gb": 64, "pci": "0000:3b:00.0"})
    node("gpu-1", "gpu", {"model": "Ascend 910B", "mem_gb": 64, "pci": "0000:5e:00.0"})
    node("gpu-2", "gpu", {"model": "Ascend 310P", "mem_gb": 24, "pci": "0000:af:00.0"})

    # vGPU 切片
    node("vg-0a", "vgpu", {"profile": "910B-1g.16g", "mem_gb": 16, "vm": "vm-01"})
    node("vg-0b", "vgpu", {"profile": "910B-1g.16g", "mem_gb": 16, "vm": "vm-02"})
    node("vg-2a", "vgpu", {"profile": "310P-1c.8g", "mem_gb": 8, "vm": "vm-03"})

    # 虚拟机
    node("vm-01", "vm", {"vcpu": 8, "mem_gb": 32, "os": "openEuler 22.03",
                         "status": "running", "pod": "zsvirt-01"})
    node("vm-02", "vm", {"vcpu": 8, "mem_gb": 32, "os": "openEuler 22.03",
                         "status": "running", "pod": "zsvirt-02"})
    node("vm-03", "vm", {"vcpu": 16, "mem_gb": 64, "os": "EulerOS 21.10",
                         "status": "running", "pod": "zsvirt-03"})

    # 容器
    node("llm-infer-0", "container", {"image": "llm/infer-7b:1.2", "limits": {"cpu": 4, "mem_gb": 16, "gpu": 1},
                                      "status": "running", "restart_policy": "on-failure"})
    node("llm-infer-1", "container", {"image": "llm/infer-7b:1.2", "limits": {"cpu": 4, "mem_gb": 16, "gpu": 1},
                                      "status": "running", "restart_policy": "on-failure"})
    node("embed-svc", "container", {"image": "ai/embed-384:2.0", "limits": {"cpu": 2, "mem_gb": 8},
                                    "status": "running", "restart_policy": "always"})
    node("rag-agent", "container", {"image": "ai/rag-agent:0.9", "limits": {"cpu": 4, "mem_gb": 8},
                                    "status": "running", "restart_policy": "always"})
    node("gateway", "container", {"image": "ai/gateway:1.4", "limits": {"cpu": 4, "mem_gb": 8},
                                  "status": "running", "restart_policy": "always"})
    node("train-job", "container", {"image": "training/llm-pretrain:nightly", "limits": {"cpu": 12, "mem_gb": 48, "gpu": 1},
                                    "status": "running", "restart_policy": "never"})

    # AI 服务 / Agent
    node("infer-7b", "agent", {"type": "inference", "model": "llama2-7b", "slo_p95_ms": 2000,
                               "tenant": "ai-platform", "replicas": 2})
    node("embed-384", "agent", {"type": "inference", "model": "bge-384", "slo_p95_ms": 300,
                                "tenant": "ai-platform"})
    node("rag-svc", "agent", {"type": "agent", "pipeline": "rag-pipeline", "slo_p95_ms": 5000,
                              "tenant": "ai-platform"})
    node("train-svc", "agent", {"type": "training", "job": "pretrain-7b", "tenant": "ml-infra",
                                "phase": "running"})

    # 租户/项目
    node("ai-platform", "tenant", {"sla": "P1", "owner": "platform-team"})
    node("ml-infra", "tenant", {"sla": "P2", "owner": "ml-team"})

    # 关联边
    t.add_edge("h1", "gpu-0", "owns")
    t.add_edge("h1", "gpu-1", "owns")
    t.add_edge("h2", "gpu-2", "owns")
    t.add_edge("gpu-0", "vg-0a", "slices")
    t.add_edge("gpu-0", "vg-0b", "slices")
    t.add_edge("gpu-2", "vg-2a", "slices")
    t.add_edge("h1", "vm-01", "runs")
    t.add_edge("h1", "vm-02", "runs")
    t.add_edge("h2", "vm-03", "runs")
    t.add_edge("vm-01", "vg-0a", "binds")
    t.add_edge("vm-02", "vg-0b", "binds")
    t.add_edge("vm-03", "vg-2a", "binds")
    t.add_edge("vm-01", "llm-infer-0", "runs")
    t.add_edge("vm-01", "llm-infer-1", "runs")
    t.add_edge("vm-01", "embed-svc", "runs")
    t.add_edge("vm-02", "rag-agent", "runs")
    t.add_edge("vm-02", "gateway", "runs")
    t.add_edge("vm-03", "train-job", "runs")
    t.add_edge("llm-infer-0", "infer-7b", "provides")
    t.add_edge("llm-infer-1", "infer-7b", "provides")
    t.add_edge("embed-svc", "embed-384", "provides")
    t.add_edge("rag-agent", "rag-svc", "provides")
    t.add_edge("train-job", "train-svc", "provides")
    t.add_edge("ai-platform", "infer-7b", "owns")
    t.add_edge("ai-platform", "embed-384", "owns")
    t.add_edge("ai-platform", "rag-svc", "owns")
    t.add_edge("ai-platform", "vm-01", "owns")
    t.add_edge("ai-platform", "vm-02", "owns")
    t.add_edge("ai-platform", "h1", "runs")
    t.add_edge("ml-infra", "train-svc", "owns")
    t.add_edge("ml-infra", "vm-03", "owns")
    t.add_edge("ml-infra", "h2", "runs")
    t.add_edge("rag-svc", "embed-384", "depends_on")
    t.add_edge("rag-svc", "infer-7b", "depends_on")
    t.add_edge("vm-01", "vm-02", "peer")
    return t
