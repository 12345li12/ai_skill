"""``[09] Scheduler`` —— Amendment A11 的实现（FROZEN）。

架构依据：`docs/architecture.md` 2 节 ``[09] Scheduler``（**Amendment A11**，FROZEN）；
范围、锁清单与 probe 计划见 `tests/behavior/README_step5b.md` §34–§37。

**两个时点（A11.2）。** ``[05]`` 决定 planning split（是否 ``MULTI_AGENT``、有哪些 child）；
本模块只做 **runtime admission** —— 对**已经计划出来、尚未启动**的 child，
决定**现在**是否派发。**它不改 ``Plan``、不重选执行模式、不生成/删除 ``BranchSpec``。**

**四值优先级（A11.8，冻结顺序）：**

    REUSE → REJECT → QUEUE → START

    · 存在**已验证**的可复用结果                    ⇒ REUSE（**不占槽、不调用 [11]**）
    · 否则 显式 blocker 成立，或 capability 明确不支持 ⇒ REJECT
    · 否则 capability / capacity **暂时**阻止派发     ⇒ QUEUE
    · 否则                                          ⇒ START（每个 START 扣减本 pass 本地剩余槽）

**唯一 read-set：** ``plan.branches`` 的 ``branch_id``、``branches_under_admission``、
``bundles`` 与 ``reusable_results_by_branch`` 的**成员关系**、
``admission_facts_by_branch`` 的七槽、``capability`` 的 ``subagents`` / ``concurrency_limit``、
``runtime`` 的 ``active_subagent_count``。

**明确不读：**

- **raw 成本事实**（``[15a]`` 的金额 / 阈值）—— "成本超过收益"**只能**经
  ``AdmissionFacts.cost_exceeds_benefit`` 到达（A11.9）；
- ``bundle`` / ``ref`` 的**任何字段**（只判**成员关系**，A11.1 / A11.3）；
- ``Plan.mode`` / ``goal`` / ``read_only`` / ``completion_criteria`` / ``main_thread_duty``；
- ``BranchState`` 家族（本模块**什么都不写**，A11.9）。

**⏸ 两项 deferred（A11 已冻结，但不在本模块完成）：**

1. "首次 pass = ``Plan.branches`` 全部 child / 后续 pass = ``[10]`` 的 pending-admission ids"
   这条**来源**约束由 **execution / ``[10]`` wiring** 证明；本模块只校验
   "无重复 + ``Plan`` 子集 + facts 精确覆盖"。
2. REUSE 的 **pairing provenance**（"同一次真实 ``[12] → [13]`` 配对事件"）由
   ``ReusableResultSource`` 拥有；本模块只做成员关系判断与结构前提校验。
"""

from __future__ import annotations

from typing import Mapping

from .admission_facts import AdmissionFacts
from .contracts import BranchId, CapabilitySnapshot, ContextBundle, Plan
from .dispatch_decisions import DispatchAction, DispatchDecision
from .reusable_results import ReusableResultRef
from .scheduling_runtime_facts import SchedulingRuntimeFacts

__all__ = ["Scheduler", "SchedulerError"]

_REUSE_REASON = "存在已验证的可复用结果（A11.8 优先级 ①）"
_BLOCKER_REASON = "命中显式运行期 blocker（A11.3 / A11.8 优先级 ②）"
_CAPABILITY_REASON = "capability 明确不支持子代理：subagents is False（A11.6）"
_QUEUE_REASON = "capability / 容量暂时阻止派发（A11.6 / A11.8 优先级 ③）"
_START_REASON = "准入派发（A11.8 优先级 ④）"


class SchedulerError(RuntimeError):
    """``[09]`` 的显式失败：admission 集合 / 精确覆盖 / 结构前提不成立。"""


def _decision(branch_id: BranchId, action: DispatchAction, reason: str) -> DispatchDecision:
    """**唯一**的 ``DispatchDecision`` 构造点（全 ``src/`` 只有这一处）。"""
    return DispatchDecision(branch_id=branch_id, action=action, reason=reason)


def _require_exact_keys(
    actual: Mapping[object, object],
    expected: tuple[BranchId, ...],
    *,
    label: str,
) -> None:
    """**唯一**的精确覆盖校验点（A11.3c ④）：缺一个 / 多一个都显式失败。"""
    if set(actual) == set(expected):
        return
    missing = sorted(set(expected) - set(actual))
    extra = sorted(set(actual) - set(expected))
    raise SchedulerError(
        f"{label} 必须与 branches_under_admission 精确覆盖（缺少 {missing}；多余 {extra}）"
    )


def _has_blocker(facts: AdmissionFacts) -> bool:
    """七个语义维度的**统一 blocker 极性**（A11.3）：只有 ``is True`` 才构成 veto。

    ``None``（UNKNOWN）**既不等于通过、也不产生 REJECT / REUSE**（A11.3）。
    """
    if facts.duplicate.status is True or facts.context_duplicate.status is True:
        return True
    return (
        facts.evidence_sufficient is True
        or facts.insufficient_independent_value is True
        or facts.remaining_small is True
        or facts.dependency_too_strong is True
        or facts.cost_exceeds_benefit is True
    )


def _available_slots(capability: CapabilitySnapshot, runtime: SchedulingRuntimeFacts) -> int:
    """本 pass 的初始可用槽（A11.6b）：``已知上限 − active_subagent_count``；未知 / 0 ⇒ 0。

    未知 **ceiling** 不得冒充已知（A11.6）：``subagents is None`` 或 ``concurrency_limit is None``
    ⇒ 0 ⇒ 后续一律 QUEUE。
    """
    if capability.subagents is not True:
        return 0
    limit = capability.concurrency_limit
    if limit is None or limit == 0:
        return 0
    return max(limit - runtime.active_subagent_count, 0)


class Scheduler:
    """``[09]`` 的调度决定者：只产出决定，**不写任何运行态**。"""

    def schedule(
        self,
        *,
        plan: Plan,
        branches_under_admission: tuple[BranchId, ...],
        bundles: Mapping[BranchId, ContextBundle],
        admission_facts_by_branch: Mapping[BranchId, AdmissionFacts],
        reusable_results_by_branch: Mapping[BranchId, ReusableResultRef],
        capability: CapabilitySnapshot,
        runtime: SchedulingRuntimeFacts,
    ) -> tuple[DispatchDecision, ...]:
        """对 ``branches_under_admission`` 逐个产出 ``DispatchDecision``。

        输出顺序**一律投影 ``Plan.branches``**（A11.3c ②），与输入 tuple 的顺序无关。
        0 child ⇒ 返回 ``()``（``DIRECT`` / ``STAGED_SINGLE_AGENT`` 的正常结果）。
        """
        admitted = set(branches_under_admission)
        if len(admitted) != len(branches_under_admission):
            raise SchedulerError("branches_under_admission 不得含重复 branch_id（A11.3c）")

        plan_ids = {spec.branch_id for spec in plan.branches}
        outside = sorted(admitted - plan_ids)
        if outside:
            raise SchedulerError(
                f"branches_under_admission 必须是 Plan.branches 的子集（越界 {outside}）"
            )

        _require_exact_keys(
            admission_facts_by_branch,
            branches_under_admission,
            label="admission_facts_by_branch",
        )

        stray = sorted(set(reusable_results_by_branch) - admitted)
        if stray:
            raise SchedulerError(
                "reusable_results_by_branch 只能指向**当前待 admission** 的 child"
                f"（越界 {stray}；A11.4 ③）"
            )

        missing_bundles = sorted(
            branch_id for branch_id in branches_under_admission if branch_id not in bundles
        )
        if missing_bundles:
            raise SchedulerError(
                f"被裁决的 child 必须有 prepared ContextBundle（缺少 {missing_bundles}）"
            )

        remaining_slots = _available_slots(capability, runtime)
        decisions: list[DispatchDecision] = []

        for spec in plan.branches:
            if spec.branch_id not in admitted:
                continue
            facts = admission_facts_by_branch[spec.branch_id]
            if spec.branch_id in reusable_results_by_branch:
                action = DispatchAction.REUSE
                reason = _REUSE_REASON
            elif _has_blocker(facts):
                action = DispatchAction.REJECT
                reason = _BLOCKER_REASON
            elif capability.subagents is False:
                action = DispatchAction.REJECT
                reason = _CAPABILITY_REASON
            elif remaining_slots <= 0:
                action = DispatchAction.QUEUE
                reason = _QUEUE_REASON
            else:
                action = DispatchAction.START
                reason = _START_REASON
                remaining_slots -= 1
            decisions.append(_decision(spec.branch_id, action, reason))

        return tuple(decisions)
