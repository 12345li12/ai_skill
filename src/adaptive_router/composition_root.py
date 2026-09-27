"""``C2d-2a 执行编排组合根``（`外部事实 + 配置 → 装配好的执行编排对象图`）。

架构依据：`docs/architecture.md` **1.4 / 6.4 / L47**（`[10]` 运行态唯一写者）·
**A13.2**（两阶段物化）· **INV-03**（`RoutingPlan` 不可变、交给 Adapter）·
**A18.2 / A18.3**（终态与收敛事实的显式来源）· **A11.3c**（首轮 = `Plan.branches` 全部；后续 = `[10]` pending）。

范围、冻结与授权见 `tests/behavior/README_step5b.md` **§170**（C2d-2 Preflight）/ **§171**（C2d-2a Scope Review
+ 实现授权）。

**组合根责任三句话（`§171.3` 冻结）：**

  1. 组合根**只**回答"谁被创建、按什么注入关系被创建" —— 它**不**回答"事实从哪来"、
     **不**决定"跑几轮"、**不**判定"成功或失败"；
  2. **输入** = 外部事实 + 配置；**输出** = 装配好的执行编排对象图；
  3. 缺失的外部事实／配置 ⇒ **显式失败** —— **不得**为任何 seam 编造默认值。

**本模块要守住的边界（逐条）：**

  · **不是** Orchestrator / Router / Policy engine / Pass controller（装配层而已）；
  · **唯一** `AgentManager(` 生产构造点（`LEC-*` 不变量；与 `LTW-1` 同型）；
  · **不制造**任何事实 seam（`RuntimeControlFacts` / `SchedulingRuntimeFacts` / `AdmissionFacts` /
    `ExecutionContextFacts` / `ReusableResultRef` 的生产者**都在外部** —— 本模块只**转交**）；
  · **不跑** pass（不调 `run_pass`）· **不写**运行态 · **不算**终态 · **不碰** `[13]`/`[14]`；
  · **零时钟** · **零 `try`** · **不 import** `adapters`（`adapter` 只作 duck-typed 参数转交）。

⚠️ **两阶段装配是"顺序依赖"的必然结果，不是设计选择**：
   `AgentManager` 的 `branch_order` **必须**等于注入的 `Plan.branches` 顺序（A13.2 的唯一顺序来源），
   而 `Plan` 由决策链产出 ⇒ **先跑决策链拿到 `RoutingPlan`，再物化执行半边**。
   因此：`CompositionRoot` = 决策半边 + 唯一的 `run_id` 生产者；
   其 `materialize(routing)` = 执行半边的**唯一**物化入口（`AgentManager` 在此被构造**一次**）。
⚠️ **禁止子串扫描**：本文件 docstring 里**故意提及** `close_run` / `converge_run` / `run_pass` /
   `recovery` / `resume` 等词 ⇒ 全部锁一律 **AST 级 / 绑定集级**。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Mapping

from .admission_facts import AdmissionFacts
from .agent_manager import AgentManager
from .capabilities import CapabilityRegistry
from .context_builder import ContextBuilder
from .contracts import BranchId, ContextBundle, RunId, RoutingPlan
from .contracts.common import new_run_id
from .execution import ExecutionCompositor
from .execution_context_facts import ExecutionContextFacts
from .reusable_results import ReusableResultRef
from .router import AdaptiveRouter
from .routing_plan import RoutingPlanAssembler
from .runtime_control_facts import RuntimeControlFacts
from .scheduler import Scheduler

__all__ = ["CompositionRoot", "CompositionRootError", "ExecutionGraph"]

#: `FactSource` —— `AdaptiveRouter` 的**唯一事实入口**（调用方提供；本模块**不**实现它）。
FactSource = Callable[[object, object], object]


class CompositionRootError(RuntimeError):
    """``C2d-2a`` 的显式失败：缺外部事实 / 缺配置 / 注入面形状非法 / 顺序依赖被破坏。"""


@dataclass(frozen=True)
class ExecutionGraph:
    """**装配完成的执行半边对象图**（`C2d-2a` 的输出；**实现级载体**，不进 `contracts/`）。

    ⚠️ 它是"**装配结果**"，**不是**编排器：不持有业务决策、不跑 pass、不算终态。
    ⚠️ `agent_manager` / `compositor` 由 :meth:`CompositionRoot.materialize` **构造并持有**。
    ⚠️ `bundles` 是 **A10.4 的派生载体**（每个 `Plan.branches` child **预构造恰好一个**），
      由 :meth:`CompositionRoot.materialize` **原样装入**同一批已构造好的映射
      （**不重算、不复制**）⇒ 它是"**装配结果的一部分**"，**不是**外部注入的事实 seam。
    """

    run_id: RunId
    agent_manager: AgentManager
    compositor: ExecutionCompositor
    routing: RoutingPlan
    bundles: Mapping[BranchId, ContextBundle]


@dataclass
class CompositionRoot:
    """执行编排的**唯一装配点**（`§171.3` 的三句话；`C2d-2a` 的交付物）。

    **外部注入面（全部 keyword-only；缺失即显式失败）**

    | 参数 | 类别 | 现状 |
    |---|---|---|
    | `facts_source` | 决策链的唯一事实入口（`RouterFacts`） | 外部（有生产者） |
    | `capabilities` | 能力快照源 | 外部（`CapabilityRegistry`） |
    | `adapter` | 运行平台动作面（duck-typed：`start`/`append`/`stop`） | **外部**（生产实现属 `C2d-2d`） |
    | `execution_context_facts_by_branch` | 每 child 的四项上下文事实 | **外部**（构造点全仓 = 0） |
    | `confirmed_conclusions_by_branch` | 每 child 的已确认结论 | 外部 |
    | `control_facts_source` | 每次 pass 的运行期控制事实 **二元组** `(facts, ended_at)` | **外部** |
    | `admission_facts_source` | 每 branch 的准入事实 | **外部** |
    | `reusable_result_source` | 每 branch 的可复用结果（无则 `None`） | **外部** |
    | `run_id_factory` | `run_id` 的**唯一**生产者（可注入；**只调用一次**） | 配置 |
    | `total_attempt_limit` | attempt 上限（架构 9.1 = 2） | 配置 |

    ⚠️ 五个事实 seam 的**生产者都在本模块之外**：本模块**只转交**，**不构造**（`LEC` 锁死）。
    ⚠️ `run_id` **只在构造时生成一次**并 memorize：`AgentManager` 与决策链的
       `RoutingPlanAssembler` **共用同一个 `run_id_factory`** ⇒ 二者得到**同一个** `run_id`。
    """

    facts_source: FactSource
    capabilities: CapabilityRegistry
    adapter: object
    execution_context_facts_by_branch: Mapping[BranchId, ExecutionContextFacts]
    confirmed_conclusions_by_branch: Mapping[BranchId, tuple[str, ...]]
    control_facts_source: Callable[[], tuple[RuntimeControlFacts, str]]
    admission_facts_source: Callable[[BranchId], AdmissionFacts]
    reusable_result_source: Callable[[BranchId], ReusableResultRef | None]
    #: **P2**：Host 注入的 Run 级续跑事实来源（`None` = 本轮无推进；`int` = 目标 attempt）。
    #: **required**（⛔ 无默认值 —— 否则 canonical Host wiring 可省略 ingress）。
    continuation_source: Callable[[], int | None]
    total_attempt_limit: int = 2
    run_id_factory: Callable[[], RunId] = new_run_id
    #: `AdaptiveRouter` 的其余可注入节点（默认真实实现）—— 原样转交，**不**在此重写。
    router_overrides: Mapping[str, object] = field(default_factory=dict)

    _run_id: RunId | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        if not callable(self.facts_source):
            raise CompositionRootError(f"facts_source 必须可调用，收到 {self.facts_source!r}")
        if not isinstance(self.capabilities, CapabilityRegistry):
            raise CompositionRootError(
                f"capabilities 必须是 CapabilityRegistry，收到 {self.capabilities!r}"
            )
        if not callable(self.continuation_source):
            raise CompositionRootError(
                f"continuation_source 必须可调用，收到 {self.continuation_source!r}"
            )
        if not callable(self.run_id_factory):
            raise CompositionRootError(f"run_id_factory 必须可调用，收到 {self.run_id_factory!r}")
        if type(self.total_attempt_limit) is not int or self.total_attempt_limit < 1:
            raise CompositionRootError(
                f"total_attempt_limit 必须是 >= 1 的 int，收到 {self.total_attempt_limit!r}"
            )
        for name, source in (
            ("control_facts_source", self.control_facts_source),
            ("admission_facts_source", self.admission_facts_source),
            ("reusable_result_source", self.reusable_result_source),
        ):
            if not callable(source):
                raise CompositionRootError(f"{name} 必须可调用，收到 {source!r}")
        if not isinstance(self.execution_context_facts_by_branch, Mapping):
            raise CompositionRootError(
                "execution_context_facts_by_branch 必须是 Mapping，"
                f"收到 {self.execution_context_facts_by_branch!r}"
            )
        if not isinstance(self.confirmed_conclusions_by_branch, Mapping):
            raise CompositionRootError(
                "confirmed_conclusions_by_branch 必须是 Mapping，"
                f"收到 {self.confirmed_conclusions_by_branch!r}"
            )
        if self.adapter is None:
            raise CompositionRootError(
                "adapter 不得为 None（生产实现属 C2d-2d；本模块只接受**注入**的 duck-typed adapter）"
            )

        # ---- `run_id`：**唯一**一次生成，并被两个消费者共用（`routing` 与 `AgentManager`） ----
        self._run_id = self.run_id_factory()

        # ---- 决策半边：装配 `AdaptiveRouter`（顺序与架构 4.1 一致；本模块不重写其逻辑） ----
        self._assembler = RoutingPlanAssembler(run_id_factory=lambda: self._run_id)
        self._context_builder = ContextBuilder()
        self._scheduler = Scheduler()
        self._router = AdaptiveRouter(
            facts_source=self.facts_source,
            capabilities=self.capabilities,
            assembler=self._assembler,
            **dict(self.router_overrides),
        )

    # ------------------------------------------------------------------
    # 只读访问面（**不产生决策**）
    # ------------------------------------------------------------------

    @property
    def run_id(self) -> RunId:
        """本 Run 的**唯一**标识（构造时生成一次）。"""
        return self._run_id  # type: ignore[return-value]

    @property
    def decision_router(self) -> AdaptiveRouter:
        """决策半边（**唯一**装配好的 `AdaptiveRouter`；`C2d-2b` 的包装对象）。"""
        return self._router

    # ------------------------------------------------------------------
    # 执行半边：**唯一**物化入口
    # ------------------------------------------------------------------

    def materialize(self, routing: RoutingPlan) -> ExecutionGraph:
        """用决策链产出的 `RoutingPlan` 物化执行半边，返回**装配完成的对象图**。

        ⚠️ **顺序依赖在此显式化**：`routing.plan.branches` 是 `branch_order` 的**唯一**来源
           （A13.2）；因此本方法是 `AgentManager` 的**唯一**构造点。
        ⚠️ **不跑 pass**、**不写运行态**、**不算终态** —— 只装配。
        ⚠️ `bundles` 在此构造**一次**并**原样装入**返回的 `ExecutionGraph`
           （A10.4：每个 `Plan.branches` child 预构造恰好一个；0 child ⇒ `{}`）
           ⇒ 消费方**不需要**再算一遍，也不会产生"两份来源"。
        ⚠️ `routing.run_id` 必须与本根生成的 `run_id` **一致**（不一致 ⇒ 显式失败：
           那意味着决策链用了另一个 `run_id`，会让运行态与 `RoutingPlan` 脱钩）。
        """
        if not isinstance(routing, RoutingPlan):
            raise CompositionRootError(f"routing 必须是 RoutingPlan，收到 {routing!r}")
        if routing.run_id != self._run_id:
            raise CompositionRootError(
                f"routing.run_id={routing.run_id!r} 与本组合根的 run_id={self._run_id!r} 不一致"
                "（决策链必须使用**同一个** run_id）"
            )
        bundles: Mapping[BranchId, ContextBundle] = self._context_builder.build(
            plan=routing.plan,
            facts_by_branch=self.execution_context_facts_by_branch,
            confirmed_conclusions_by_branch=self.confirmed_conclusions_by_branch,
        )
        agent_manager = AgentManager(
            run_id=self._run_id,
            branch_order=tuple(spec.branch_id for spec in routing.plan.branches),
            total_attempt_limit=self.total_attempt_limit,
        )
        compositor = ExecutionCompositor(
            scheduler=self._scheduler,
            agent_manager=agent_manager,
            adapter=self.adapter,
            admission_facts_source=self.admission_facts_source,
            reusable_result_source=self.reusable_result_source,
            continuation_source=self.continuation_source,
        )
        return ExecutionGraph(
            run_id=self._run_id,
            agent_manager=agent_manager,
            compositor=compositor,
            routing=routing,
            bundles=bundles,
        )
