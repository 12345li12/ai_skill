"""planner —— [04] Evaluator + [05] Planner。

架构依据：`docs/architecture.md` v1.0 FROZEN
  - 2 节 [04] Evaluator / [05] Planner
  - 1.2 DECISION 层

```text
[04] Evaluator
   输入：用户任务、ProjectContext、CapabilitySnapshot
   输出：**11 维 Evaluation**
   约束：只**读** [03] 的 Snapshot，**不得自行探测**

        ↓

[05] Planner
   输入：Evaluation、ProjectContext、CapabilitySnapshot、
        **当前用户任务中的规划事实**（Amendment A2）、
        **已解析的调度约束** ``EffectivePlanningConstraints``（Amendment A3）
   输出：Plan（执行方式 + 分支定义 + 主线职责）
   职责：**唯一**决定 DIRECT / STAGED_SINGLE_AGENT / MULTI_AGENT
```

⚠️ **Amendment A2**：冻结输出 ``Plan`` 要求"每支完成标准"，
仅凭 11 维 ``Evaluation`` **不可能**产生分支 goal / completion criteria，
因此 [05] 的输入正式增加"当前用户任务中的规划事实"。
该事实由 ``TaskFacts`` 规范化承载 —— 它是**实现级 DTO**，
不是 ``Evaluation`` 的扩展字段，也不改变 11 维冻结契约。

⚠️ 四条边界：

   1. **不制造浮点打分器。** 冻结契约是**离散** 11 维判断；
      ``0.73 complexity`` 这种伪精度违反 L55 精神。
   2. **不探测能力。** Evaluator 只接收 ``CapabilitySnapshot``；
      探测是 [03] 的事。
   3. **不为了"看起来像 Router"而强行建 branch。** 无真实代理、容量不足、
      强依赖、分流收益不足时必须收回到 DIRECT / STAGED（L25 / L29）。
   4. **不产出模型选择。** 档位是 [06] Model Router 的职责。本模块
      **不导入** ``ModelSelection`` —— 那是生命周期在后的节点。

⚠️ **允许注入 ``branch_id_factory``** 以便测试稳定地生成 child ``branch_id``。
   **但不要把 main-thread branch ID 塞给 Planner** ——
   主线程执行单位身份属于后续 ``RoutingPlan`` 装配阶段
   （冻结契约已明确 ``RoutingPlan.main_thread_branch_id`` 为必填）。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Callable

from .contracts import (
    BranchSpec,
    CapabilitySnapshot,
    Evaluation,
    ExecutionMode,
    ImpactScope,
    Ordinal,
    Plan,
    ProjectContext,
    new_branch_id,
)
from .planning_constraints import EffectivePlanningConstraints

__all__ = [
    "TaskScale",
    "EvidenceStrength",
    "NoSplitReason",
    "PlannedSubtask",
    "TaskFacts",
    "Evaluator",
    "Planner",
    "SPLIT_MIN_SUBTASKS",
    "LARGE_SCALE_MODULES",
    "CROSS_MODULE_MODULES",
    "SYSTEM_WIDE_MODULES",
    "HEAVY_CONTEXT_SIZE",
]

#: 真实多代理分流所需的最少独立子任务数（L25）。
SPLIT_MIN_SUBTASKS = 2

#: 受影响模块数达到该值即视为高规模。
LARGE_SCALE_MODULES = 8

#: 受影响模块数达到该值即视为跨模块。
CROSS_MODULE_MODULES = 3

#: 受影响模块数达到该值即视为系统级影响。
SYSTEM_WIDE_MODULES = 8

#: 上下文量大到会影响并行收益的阈值。
HEAVY_CONTEXT_SIZE = 20


class TaskScale(Enum):
    """任务声明规模。**离散**取值，不是浮点评分。"""

    SMALL = "small"
    MEDIUM = "medium"
    LARGE = "large"


class EvidenceStrength(Enum):
    """已有证据的充分度。"""

    STRONG = "strong"
    PARTIAL = "partial"
    WEAK = "weak"


class NoSplitReason(Enum):
    """**[05] Planner 侧**的不分流条件。

    ⚠️ L29 的七种"不分流条件"是**系统级语义**，不等于 [05] 内部必须拥有
       七个 ``NoSplitReason``。归属如下（见 architecture Amendment A2）：

           用户关闭        → [01] SessionGate / entrypoint 在 **Router 外**强制
                             （Step 4 的 BYPASS 已实现；Planner 不得自行判断）
           更高规则限制    → [02] Policy 解析成 ``EffectivePlanningConstraints``，
                             由 [05] 执行成 POLICY_NO_SPLIT（A3 的条件性回收）
           ────────────────────────────────────────────────────────
           简单任务        → 本枚举 SIMPLE_TASK
           强依赖          → 本枚举 STRONG_DEPENDENCY
           收益不足        → 本枚举 INSUFFICIENT_BENEFIT
           容量不足        → 本枚举 INSUFFICIENT_CAPACITY
           能力缺失        → 本枚举 MISSING_CAPABILITY

    ⚠️ ``TEST-PLAN-005`` 的承载是 **[01] + [02] + [05]**，三段通路均已接通
       （[01] 见 Step 4；[02] 见 5B.1；[02]→[05] seam 见 5B.2），
       因此该 Test ID 现为 **COMPLETE**（`tests/behavior/README_step5b.md` §5）。

    另有两个 Planner 侧**内部** blocker（不属于 L29 原文，但同属"不分流"）：

           CONTEXT_DUPLICATION   上下文复制多（L25 的 STAGED 条件之一）
           TOO_FEW_SUBTASKS      候选子任务不足以做真实分流

    ⚠️ 刻意**不设** ``UNSTABLE_COMPLETION_CRITERIA``：
       ``PlannedSubtask`` 在构造期已禁止空 ``completion_criteria``，
       所以那条 blocker **永远不可达**。一个不可达的枚举值只会让
       "七项齐备"看起来像真的。
    """

    SIMPLE_TASK = "simple_task"
    STRONG_DEPENDENCY = "strong_dependency"
    INSUFFICIENT_BENEFIT = "insufficient_benefit"
    INSUFFICIENT_CAPACITY = "insufficient_capacity"
    MISSING_CAPABILITY = "missing_capability"
    # ---- Planner 侧内部 blocker（不属于 L29 原文）----
    CONTEXT_DUPLICATION = "context_duplication"
    TOO_FEW_SUBTASKS = "too_few_subtasks"
    # ---- [02] Policy 已解析约束的执行 blocker（Amendment A3）----
    #: 「更高规则限制」落在 Planner 侧的执行 blocker。
    #: **不是 L29 条件** —— L29 的七种不分流条件归属表不变（A3 钉子 ①）。
    POLICY_NO_SPLIT = "policy_no_split"

    @classmethod
    def l29_conditions(cls) -> frozenset["NoSplitReason"]:
        """L29 原文中**归属于 [05] Planner** 的五种条件。"""
        return frozenset(
            {
                cls.SIMPLE_TASK,
                cls.STRONG_DEPENDENCY,
                cls.INSUFFICIENT_BENEFIT,
                cls.INSUFFICIENT_CAPACITY,
                cls.MISSING_CAPABILITY,
            }
        )

    @property
    def is_l29_condition(self) -> bool:
        return self in self.l29_conditions()


@dataclass(frozen=True)
class PlannedSubtask:
    """一个**候选**子任务：目标 + 完成标准。

    ``read_only`` 由调用方声明（L27 的读写属性）；默认只读，
    因为保守的默认不会误授权写操作。
    """

    goal: str
    completion_criteria: str
    read_only: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.goal, str) or not self.goal.strip():
            raise ValueError("goal 必须是非空字符串")
        if (
            not isinstance(self.completion_criteria, str)
            or not self.completion_criteria.strip()
        ):
            raise ValueError("completion_criteria 必须是非空字符串")
        if not isinstance(self.read_only, bool):
            raise TypeError("read_only 必须是 bool")


@dataclass(frozen=True)
class TaskFacts:
    """任务的**事实输入**。

    ⚠️ 字段集合封闭：只描述任务本身，不含审批 / 成本 / 模型选择。
       无可靠事实的字段保持保守默认值，**不靠猜测**。
    """

    affected_modules: int
    mechanical: bool
    touches_cross_module_contracts: bool
    requires_iterative_debugging: bool
    is_review_task: bool
    subtasks: tuple[PlannedSubtask, ...]
    shared_context_size: int
    has_sequential_dependency: bool
    branch_setup_cost: Ordinal
    evidence: EvidenceStrength
    local_and_reversible: bool

    def __post_init__(self) -> None:
        for name in ("affected_modules", "shared_context_size"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool):
                raise TypeError(f"{name} 必须是 int，收到 {value!r}")
            if value < 0:
                raise ValueError(f"{name} 不得为负")
        for name in (
            "mechanical",
            "touches_cross_module_contracts",
            "requires_iterative_debugging",
            "is_review_task",
            "has_sequential_dependency",
            "local_and_reversible",
        ):
            value = getattr(self, name)
            if not isinstance(value, bool):
                raise TypeError(f"{name} 必须是 bool，收到 {value!r}")
        if not isinstance(self.branch_setup_cost, Ordinal):
            raise TypeError(
                f"branch_setup_cost 必须是 Ordinal，收到 {self.branch_setup_cost!r}"
            )
        if not isinstance(self.evidence, EvidenceStrength):
            raise TypeError(
                f"evidence 必须是 EvidenceStrength，收到 {self.evidence!r}"
            )
        if not isinstance(self.subtasks, tuple):
            raise TypeError("subtasks 必须是 tuple")
        for item in self.subtasks:
            if not isinstance(item, PlannedSubtask):
                raise TypeError(f"subtasks 元素必须是 PlannedSubtask，收到 {item!r}")


# ==========================================================================
# [04] Evaluator
# ==========================================================================


class Evaluator:
    """把任务事实 + 能力快照折算成 **11 维** ``Evaluation``。

    确定性、无副作用、**不探测**。
    """

    def evaluate(
        self,
        facts: TaskFacts,
        *,
        capability: CapabilitySnapshot,
        project: ProjectContext,
    ) -> Evaluation:
        if not isinstance(facts, TaskFacts):
            raise TypeError(f"facts 必须是 TaskFacts，收到 {facts!r}")
        if not isinstance(capability, CapabilitySnapshot):
            raise TypeError("capability 必须是 CapabilitySnapshot")
        if not isinstance(project, ProjectContext):
            raise TypeError("project 必须是 ProjectContext")

        scale = self._scale(facts)
        complexity = self._complexity(facts)
        impact_scope = self._impact_scope(facts)
        risk = self._risk(facts, project)
        independence = self._independence(facts)
        parallel_benefit = self._parallel_benefit(facts, independence)
        concurrency_capacity = self._concurrency_capacity(capability)

        return Evaluation(
            scale=scale,
            complexity=complexity,
            risk=risk,
            impact_scope=impact_scope,
            independence=independence,
            parallel_benefit=parallel_benefit,
            context_volume=self._context_volume(facts),
            scheduling_cost=facts.branch_setup_cost,
            rework_probability=self._rework_probability(facts),
            review_value=Ordinal.HIGH if facts.is_review_task else Ordinal.LOW,
            concurrency_capacity=concurrency_capacity,
            reason=self._reason(
                facts,
                scale=scale,
                complexity=complexity,
                risk=risk,
                impact_scope=impact_scope,
                concurrency_capacity=concurrency_capacity,
            ),
        )

    # ------------------------------------------------------------------

    @staticmethod
    def _scale(facts: TaskFacts) -> Ordinal:
        if facts.affected_modules >= LARGE_SCALE_MODULES:
            return Ordinal.HIGH
        if facts.affected_modules >= CROSS_MODULE_MODULES:
            return Ordinal.MEDIUM
        return Ordinal.LOW

    @staticmethod
    def _complexity(facts: TaskFacts) -> Ordinal:
        """难度维度。

        ⚠️ **"机械明确"必须落在 LOW**，而"常规但非机械"落在 MEDIUM ——
           这个区分是 L33 档位映射的依据（"机械明确低风险 → 低成本/低强度"
           vs "常规编码分析 → 平衡型/中等"）。它属于**事实判断**，
           不是模型选择，因此归 [04]；[06] 只消费这个离散结果。

        ⚠️ **``requires_iterative_debugging`` 本身就是强信号。**
           L33 明确"**复杂调试** → 较强/较高"，因此它在**单模块**场景下
           也必须足以达到 ``HIGH``（否则会落到平衡档，与 L33 冲突）。
           它 +2，单条即可入 HIGH。

        ⚠️ **``mechanical`` 降低内在难度，但不得抹掉更强的信号。**
           机械任务若仍需迭代调试或规模很大，照样抬高复杂度。

        ⚠️ **跨模块不计入本维度**（``touches_cross_module_contracts`` 既不
           阻止 early-return，也不加分）—— 它由 ``impact_scope`` 独立承载，
           再由 Model Router 的 scope floor 保证最终档位不低于 ``STRONG``。
           这样同一个事实只沿**一条**路径影响档位，不被计两次。
        """
        if facts.mechanical and not (
            facts.requires_iterative_debugging
            or facts.affected_modules >= LARGE_SCALE_MODULES
        ):
            # 纯机械任务：无更强信号 → 最低难度
            return Ordinal.LOW

        # **迭代调试是显式强信号**：它必须足以达到 HIGH，
        # 不因 mechanical 起点低而降级（L33："复杂调试 → 较强/较高"）。
        if facts.requires_iterative_debugging:
            return Ordinal.HIGH

        score = 0 if facts.mechanical else 1  # 非机械本身即"常规"起点
        if facts.affected_modules >= LARGE_SCALE_MODULES:
            score += 1

        return Ordinal.HIGH if score >= 3 else Ordinal.MEDIUM

    @staticmethod
    def _impact_scope(facts: TaskFacts) -> ImpactScope:
        if (
            facts.affected_modules >= SYSTEM_WIDE_MODULES
            or facts.touches_cross_module_contracts
        ):
            return ImpactScope.SYSTEM_WIDE
        if facts.affected_modules >= CROSS_MODULE_MODULES:
            return ImpactScope.CROSS_MODULE
        return ImpactScope.LOCAL

    @staticmethod
    def _risk(facts: TaskFacts, project: ProjectContext) -> Ordinal:
        if not facts.local_and_reversible:
            return Ordinal.HIGH
        if facts.touches_cross_module_contracts:
            return Ordinal.MEDIUM
        if project.high_risk_files:
            # 项目声明了高风险文件：只把它当作**事实**提升风险一档，
            # 不做任何权限裁决（那属 Policy / AuthorizationContext）。
            return Ordinal.MEDIUM
        return Ordinal.LOW

    @staticmethod
    def _independence(facts: TaskFacts) -> Ordinal:
        count = len(facts.subtasks)
        if facts.has_sequential_dependency or count < SPLIT_MIN_SUBTASKS:
            return Ordinal.LOW
        return Ordinal.HIGH if count >= 3 else Ordinal.MEDIUM

    @staticmethod
    def _parallel_benefit(facts: TaskFacts, independence: Ordinal) -> Ordinal:
        if facts.has_sequential_dependency:
            return Ordinal.LOW
        if len(facts.subtasks) < SPLIT_MIN_SUBTASKS:
            return Ordinal.LOW
        if facts.branch_setup_cost is Ordinal.HIGH:
            return Ordinal.LOW
        if facts.shared_context_size >= HEAVY_CONTEXT_SIZE:
            # 上下文复制多：分流会把大量上下文复制到每个分支，
            # 收益被抵消。**真正把它压成不分流的是 Planner 的
            # CONTEXT_DUPLICATION blocker**，这里只反映收益受损。
            return Ordinal.MEDIUM
        if independence is Ordinal.HIGH:
            return Ordinal.HIGH
        if facts.branch_setup_cost is Ordinal.MEDIUM:
            return Ordinal.LOW
        return Ordinal.MEDIUM

    @staticmethod
    def _context_volume(facts: TaskFacts) -> Ordinal:
        if facts.shared_context_size >= HEAVY_CONTEXT_SIZE:
            return Ordinal.HIGH
        if facts.shared_context_size > 0:
            return Ordinal.MEDIUM
        return Ordinal.LOW

    @staticmethod
    def _rework_probability(facts: TaskFacts) -> Ordinal:
        return {
            EvidenceStrength.STRONG: Ordinal.LOW,
            EvidenceStrength.PARTIAL: Ordinal.MEDIUM,
            EvidenceStrength.WEAK: Ordinal.HIGH,
        }[facts.evidence]

    @staticmethod
    def _concurrency_capacity(capability: CapabilitySnapshot) -> Ordinal:
        """**只读** [03] 的 Snapshot。

        ⚠️ ``None``（未知）**不当作 0** —— 未知 ≠ 不支持。
        """
        if capability.subagents is False:
            return Ordinal.LOW
        limit = capability.concurrency_limit
        if limit is None:
            return Ordinal.MEDIUM
        if limit <= 1:
            return Ordinal.LOW
        if limit < 4:
            return Ordinal.MEDIUM
        return Ordinal.HIGH

    @staticmethod
    def _reason(
        facts: TaskFacts,
        *,
        scale: Ordinal,
        complexity: Ordinal,
        risk: Ordinal,
        impact_scope: ImpactScope,
        concurrency_capacity: Ordinal,
    ) -> str:
        parts = [
            f"modules={facts.affected_modules}",
            f"scale={scale.value}",
            f"complexity={complexity.value}",
            f"risk={risk.value}",
            f"scope={impact_scope.value}",
            f"subtasks={len(facts.subtasks)}",
            f"evidence={facts.evidence.value}",
            f"capacity={concurrency_capacity.value}",
        ]
        if facts.mechanical:
            parts.append("mechanical")
        if facts.has_sequential_dependency:
            parts.append("sequential")
        if not facts.local_and_reversible:
            parts.append("not-reversible")
        return "; ".join(parts)


# ==========================================================================
# [05] Planner
# ==========================================================================


@dataclass
class Planner:
    """把 ``Evaluation`` 与任务事实折算成 ``Plan``。

    ⚠️ **唯一**决定 DIRECT / STAGED / MULTI_AGENT 的节点。
    ⚠️ 命中任一不分流条件（L29）时必须收回到 DIRECT / STAGED。
    ⚠️ **不产出模型选择** —— 那是 [06] 的职责。
    """

    branch_id_factory: Callable[[], str] = new_branch_id

    def plan(
        self,
        evaluation: Evaluation,
        facts: TaskFacts,
        *,
        capability: CapabilitySnapshot,
        project: ProjectContext,
        constraints: EffectivePlanningConstraints,
    ) -> Plan:
        """产出 ``Plan``。

        ``constraints`` 是 **``[02] Policy`` 已解析的调度约束**（Amendment A3），
        它是**必需输入**：没有默认值、不接受 ``None``。
        "没收到就当作允许分流"是 **fail-open**，会静默绕过更高层限制（A3 钉子 ③）。
        """
        if not isinstance(evaluation, Evaluation):
            raise TypeError(f"evaluation 必须是 Evaluation，收到 {evaluation!r}")
        if not isinstance(facts, TaskFacts):
            raise TypeError(f"facts 必须是 TaskFacts，收到 {facts!r}")
        if not isinstance(capability, CapabilitySnapshot):
            raise TypeError("capability 必须是 CapabilitySnapshot")
        if not isinstance(project, ProjectContext):
            raise TypeError("project 必须是 ProjectContext")
        if not isinstance(constraints, EffectivePlanningConstraints):
            raise TypeError(
                "constraints 必须是 EffectivePlanningConstraints"
                f"（[02] Policy 的已解析约束），收到 {constraints!r}"
                " —— 缺参时**不得**默认视为允许分流"
            )

        blockers = self.no_split_reasons(evaluation, facts, capability=capability)
        if blockers:
            # L29 命中：沿用 5A 冻结行为，**不受** policy 影响
            # （A3 钉子 ②：otherwise-DIRECT 仍 DIRECT、otherwise-STAGED 仍 STAGED）
            return self._single_agent_plan(evaluation, facts, blockers)
        if not constraints.auto_split_allowed:
            # A3 钉子 ②：**只拦截 otherwise-MULTI_AGENT** → 条件性回收
            return self._policy_recycled_plan(evaluation, facts)
        return self._multi_agent_plan(evaluation, facts)

    # ------------------------------------------------------------------

    @staticmethod
    def no_split_reasons(
        evaluation: Evaluation,
        facts: TaskFacts,
        *,
        capability: CapabilitySnapshot,
    ) -> tuple[NoSplitReason, ...]:
        """L29 的不分流条件。返回全部命中项（可审计）。"""
        reasons: list[NoSplitReason] = []

        if evaluation.complexity is Ordinal.LOW and evaluation.scale is Ordinal.LOW:
            reasons.append(NoSplitReason.SIMPLE_TASK)
        if facts.has_sequential_dependency:
            reasons.append(NoSplitReason.STRONG_DEPENDENCY)
        if evaluation.parallel_benefit is Ordinal.LOW:
            reasons.append(NoSplitReason.INSUFFICIENT_BENEFIT)
        if evaluation.scheduling_cost is Ordinal.HIGH:
            reasons.append(NoSplitReason.INSUFFICIENT_BENEFIT)
        if facts.shared_context_size >= HEAVY_CONTEXT_SIZE:
            # L25：**上下文复制多** → 单代理阶段化
            reasons.append(NoSplitReason.CONTEXT_DUPLICATION)
        if len(facts.subtasks) < SPLIT_MIN_SUBTASKS:
            reasons.append(NoSplitReason.TOO_FEW_SUBTASKS)
        if capability.subagents is False:
            reasons.append(NoSplitReason.MISSING_CAPABILITY)
        if evaluation.concurrency_capacity is Ordinal.LOW:
            reasons.append(NoSplitReason.INSUFFICIENT_CAPACITY)

        return tuple(reasons)

    def _single_agent_plan(
        self,
        evaluation: Evaluation,
        facts: TaskFacts,
        blockers: tuple[NoSplitReason, ...],
    ) -> Plan:
        """收回到 0 分支的执行方式。

        L25 的三种 STAGED 条件：**强依赖 / 上下文复制多 / 无真实代理**；
        另加容量不足（环境无法承载分流）。其余情况走 ``DIRECT``
        （简单、局部、不划算）。
        """
        staged_only = {
            NoSplitReason.STRONG_DEPENDENCY,
            NoSplitReason.CONTEXT_DUPLICATION,
            NoSplitReason.MISSING_CAPABILITY,
            NoSplitReason.INSUFFICIENT_CAPACITY,
        }
        mode = (
            ExecutionMode.STAGED_SINGLE_AGENT
            if staged_only.intersection(blockers)
            else ExecutionMode.DIRECT
        )
        return Plan(
            mode=mode,
            main_thread_duty="execute the task on the main thread",
            reason=self._reason(mode, blockers, evaluation),
        )

    def _policy_recycled_plan(self, evaluation: Evaluation, facts: TaskFacts) -> Plan:
        """``[02]`` 禁止分流时的**条件性回收**（A3 钉子 ②）。

        只有走到这里才说明：**L29 一个都没命中**，原本会是 ``MULTI_AGENT``。
        此时按"更高规则限制"收回到 **``STAGED_SINGLE_AGENT`` + 0 分支**。

        ⚠️ 因此本路径**不可能**把原本 ``DIRECT`` 的任务提升成 STAGED：
        命中 L29 的调用在上面就返回了，``POLICY_NO_SPLIT`` 从不与 L29 条件混用
        （这是 A3 明确警告过的错误做法）。
        """
        blockers = (NoSplitReason.POLICY_NO_SPLIT,)
        return Plan(
            mode=ExecutionMode.STAGED_SINGLE_AGENT,
            main_thread_duty="execute the task on the main thread in stages",
            reason=self._reason(ExecutionMode.STAGED_SINGLE_AGENT, blockers, evaluation),
        )

    def _multi_agent_plan(self, evaluation: Evaluation, facts: TaskFacts) -> Plan:
        branches = tuple(
            BranchSpec(
                branch_id=self.branch_id_factory(),
                goal=subtask.goal,
                read_only=subtask.read_only,
                completion_criteria=subtask.completion_criteria,
            )
            for subtask in facts.subtasks
        )
        return Plan(
            mode=ExecutionMode.MULTI_AGENT,
            main_thread_duty="coordinate branches and integrate results",
            reason=self._reason(ExecutionMode.MULTI_AGENT, (), evaluation),
            branches=branches,
        )

    @staticmethod
    def _reason(
        mode: ExecutionMode,
        blockers: tuple[NoSplitReason, ...],
        evaluation: Evaluation,
    ) -> str:
        parts = [f"mode={mode.value}"]
        if blockers:
            parts.append("no-split=" + ",".join(r.value for r in blockers))
        parts.append(f"parallel_benefit={evaluation.parallel_benefit.value}")
        parts.append(f"independence={evaluation.independence.value}")
        parts.append(f"branch_setup_cost={evaluation.scheduling_cost.value}")
        return "; ".join(parts)
