"""每 child 的上下文事实与已确认结论 —— Host 必须提供的**注入面**（自建）。

契约来源 = `§279.2`。
"""

from __future__ import annotations

from typing import Mapping

from adaptive_router.admission_facts import AdmissionFacts, RelationFact
from adaptive_router.execution_context_facts import ExecutionContextFacts
from adaptive_router.runtime_control_facts import RuntimeControlFacts

from fixtures.facts import BRANCH_IDS, T0


def context_facts(branch_id: str) -> ExecutionContextFacts:
    """给某个 child 的最小可用上下文（自撰内容，不含测试替身）。"""
    return ExecutionContextFacts(
        background=(f"demo 场景背景（{branch_id}）",),
        constraints=("只读演示：不写真实仓库",),
        allowed_operations=("read",),
        forbidden_operations=("write",),
    )


def context_facts_by_branch() -> Mapping[str, ExecutionContextFacts]:
    """⚠️ 必须**精确覆盖** `Plan.branches`（A10.2b：不多不少）。"""
    return {branch_id: context_facts(branch_id) for branch_id in BRANCH_IDS}


def confirmed_conclusions_by_branch() -> Mapping[str, tuple[str, ...]]:
    return {branch_id: () for branch_id in BRANCH_IDS}


def admission_facts_source(_branch_id: str) -> AdmissionFacts:
    """准入事实：全"未知"（⛔ 不替 Core 编造准入判断）。"""
    return AdmissionFacts(
        duplicate=RelationFact(status=None),
        context_duplicate=RelationFact(status=None),
        evidence_sufficient=None,
        insufficient_independent_value=None,
        remaining_small=None,
        dependency_too_strong=None,
        cost_exceeds_benefit=None,
    )


class ControlSource:
    """`control_facts_source`：每次 pass 返回 `(facts, ended_at)`，并记录调用次数。"""

    def __init__(self, *, timestamp: str = T0, stop: bool = False) -> None:
        self.timestamp = timestamp
        self._stop = stop
        self.calls = 0

    def __call__(self) -> tuple[RuntimeControlFacts, str]:
        self.calls += 1
        return RuntimeControlFacts(stop_branches_requested=self._stop), self.timestamp


def reusable_result_source(_branch_id: str) -> None:
    """没有已验证的可复用结果。"""
    return None


def continuation_source() -> None:
    """Run 级续跑事实：**本轮无推进**。

    ⚠️ 该 seam 在 `5B.6.52.3-freeze` 后被定为 **required**（⛔ 无默认值）⇒
    Host **必须显式提供**它。返回 `None` ⇒ `run_pass` 不会调用 `commit_run_attempt`。
    """
    return None
