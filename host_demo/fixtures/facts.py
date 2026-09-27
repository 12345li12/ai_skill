"""场景事实 —— Host 必须提供的**决策输入**（自建 · ⛔ 不 import `tests/*`）。

契约来源 = `§279.2`（实现授权申请 · 构造契约），逐字段形态已实测锁定。

场景（自撰）：把一份"跨模块 + 三个独立子任务"的工程任务交给 Core，
使其走 `MULTI_AGENT` 分流并产出多个 child。
"""

from __future__ import annotations

from adaptive_router.capabilities import CapabilityRegistry, CapabilitySnapshot
from adaptive_router.contracts import (
    Confidence,
    EffortLevel,
    ImpactScope,
    Ordinal,
    UnavailableReason,
)
from adaptive_router.planner import EvidenceStrength, PlannedSubtask, TaskFacts
from adaptive_router.policy import PolicyFacts
from adaptive_router.risk import RiskInputs
from adaptive_router.router import RouterFacts

#: 固定的时钟与 id —— 让 demo **可复现**（⛔ 不依赖随机）
T0 = "2026-11-01T00:00:00Z"
RUN_ID = "run_demo_0001"
PROJECT_ROOT = "/demo-project"

#: 固定的 child 序列（与 `facts_by_branch` 的键**同一组**）。
#: ⚠️ 真 `Planner` 产出**随机** `branch_id` ⇒ 必须经 `planner.py` 明许的注入点固定下来
#: （见 `demo_host.StableIdPlanner`）。⛔ 不替换 planner 的任何判断。
BRANCH_IDS = ("br_demo_a", "br_demo_b", "br_demo_c")


def subtask(tag: str) -> PlannedSubtask:
    return PlannedSubtask(
        goal=f"完成子任务 {tag}",
        completion_criteria=f"子任务 {tag} 的产出可判定为完成",
        read_only=True,
    )


def policy_facts() -> PolicyFacts:
    """会话已授权、无任何拒绝面 —— 让 dispatch 走 ENABLED 正路径。"""
    return PolicyFacts(
        session_authorized=True,
        system_denied=(),
        developer_denied=(),
        safety_denied=(),
        platform_denied=(),
        user_denied=(),
        project_denied=(),
        user_granted_scopes=(),
        user_commands=(),
        user_read_only_override=False,
        project_model_preference=None,
        project_approval_preference=None,
        protocol_default_mode="observe",
    )


def task_facts() -> TaskFacts:
    """**足够分流**的任务：跨模块 + 三个独立子任务 + 强证据。"""
    return TaskFacts(
        affected_modules=4,
        mechanical=False,
        touches_cross_module_contracts=True,
        requires_iterative_debugging=True,
        is_review_task=False,
        subtasks=(subtask("A"), subtask("B"), subtask("C")),
        shared_context_size=0,
        has_sequential_dependency=False,
        branch_setup_cost=Ordinal.LOW,
        evidence=EvidenceStrength.STRONG,
        local_and_reversible=True,
    )


def risk_inputs() -> RiskInputs:
    """全部风险信号为假、影响面本地 —— 不触发人工介入。"""
    return RiskInputs(
        broad_or_hard_to_review_change=False,
        hard_to_revert_operation=False,
        production_or_real_users=False,
        shared_infrastructure=False,
        release_deploy_merge_payment=False,
        outbound_message=False,
        external_write=False,
        credentials_privacy_permissions=False,
        major_conclusion_conflict=False,
        scope_expansion=False,
        affected_scope=ImpactScope.LOCAL,
        dependency_depth=0,
        evidence_sufficiency=Confidence.HIGH,
    )


def router_facts() -> RouterFacts:
    return RouterFacts(
        policy=policy_facts(),
        task=task_facts(),
        risk=risk_inputs(),
        recovery_method="restore from backup",
    )


def capability(*, concurrency_limit: int = 4) -> CapabilitySnapshot:
    """固定能力快照（⛔ 不触发真实探测）。"""
    return CapabilitySnapshot(
        subagents=True,
        async_background=True,
        concurrency_limit=concurrency_limit,
        available_models=("demo-model-x",),
        available_effort_levels=(EffortLevel.HIGH,),
        state_wait_append_stop=True,
        hot_switch=False,
        reliable_cost_data=True,
        probed_dimensions=("subagents",),
        unavailable_reason=UnavailableReason.NOT_PROBED,
        probed_at=T0,
    )


def registry(snapshot: CapabilitySnapshot | None = None) -> CapabilityRegistry:
    """把固定快照**预置**进注册表，绕开真实探测。

    ⚠️ 这是既有 idiom（测试侧同样如此）：`CapabilityRegistry` 的 `_snapshot` 是缓存位。
    ⛔ 本 demo **不修改** `CapabilityRegistry`、⛔ 不新增探测路径、⛔ 不改 capability 语义
    —— 只是喂一个确定值（你的 B2 限制）。
    """
    instance = CapabilityRegistry.__new__(CapabilityRegistry)
    instance._snapshot = snapshot if snapshot is not None else capability()  # noqa: SLF001
    instance.last_trigger = None
    return instance
