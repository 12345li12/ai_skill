"""``Phase B``（``[13]/[14] → [10]`` 接线）—— 结构锁 **LTW-1 – LTW-9** + 行为测试 **B1 – B9**。

架构依据：`docs/architecture.md` 2 节 ``[10] Agent Manager`` 的 **Amendment A18**（FROZEN）：
A18.2（唯一写入口 + 三条禁止）· A18.3（两个收敛事实的来源）· A18.4（四条禁止推断）·
A18.5（V1 完备性声明）· **A18.6（FAIL 侧时序：先关 attempt，再产决策）** · A18.7（决策者 ≠ 状态写者）·
A18.9（`LT-1…LT-6`）· A18.10（`INV-21` / `TEST-STATE-002`）；4.5 双条件 · 6.5 六值 ·
A11.4b（配对事件的身份）· A13.7（分支级四个终态）。
范围与冻结项见 `tests/behavior/README_step5b.md` **§99**（Phase B Implementation Scope Review）。

**本模块要守住的三条主张：**

  1. **唯一终态调用点**：全 ``src/`` 只有一处 ``close_run(``，且它在 ``converge_run`` 里
     ⇒ ``[10]`` 之外没有第二个 Run 终态入口；
  2. **事实不可推**：该调用点的四个实参**必须是同名形参本身**（字面量 / 属性链 / 表达式一律红），
     且接线模块的签名里**不存在** Approval / Recovery 对象 ⇒ A18.4 的四条禁止**无源可推**；
  3. **收尾唯一且不碰 attempt**：``record_outcome(`` 只有一处（``_settle``），
     映射恰好 PASS ⇒ `COMPLETED` / FAIL ⇒ `FAILED`；不 materialize、不 commit、不产决策。

⚠️ **禁止子串扫描**：本文件 docstring 里**故意提及** `RunTerminal` / `close_run` /
`no_pending_*` / `materialize` 等词 ⇒ 所有锁一律 **AST 级**（`calls_to` /
`call_keyword_shapes_by_caller` / `function_signature_shape` / 哈希）。
⚠️ 敏感性 probe 一律在**源码字符串**上完成（``_mutate``），**不覆写** ``src/`` 里的真实文件；
真实源码的一次性变异证据见 README §100.5。
"""

from __future__ import annotations

import hashlib
import pathlib

import pytest

from adaptive_router.agent_manager import AgentManager, AgentManagerError
from adaptive_router.contracts import (
    BranchId,
    BranchReturn,
    BranchState,
    Confidence,
    ConfirmationSource,
    ContextBundle,
    EffortLevel,
    ExecutionValue,
    ModelTier,
    TerminalState,
    ValidationRecord,
    new_agent_id,
    new_branch_id,
    new_run_id,
)
from adaptive_router.dispatch_decisions import DispatchAction, DispatchDecision
from adaptive_router.recovery_wiring import route_failed_branch
from adaptive_router.terminal_wiring import (
    TerminalWiringError,
    converge_run,
    settle_failure,
    settle_pass,
)
from adaptive_router.validation_wiring import BranchValidationOutcome

from _terminal_wiring_locks import (
    annotated_assign_names,
    assignment_value_shapes,
    attribute_assignment_targets,
    call_keyword_shapes_by_caller,
    calls_to,
    clock_call_hits,
    forbidden_import_hits,
    function_return_annotation,
    function_signature_shape,
    imported_module_tokens,
    imported_names,
    module_level_container_assignments,
    module_level_function_names,
    referenced_names,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
SRC = REPO_ROOT / "src/adaptive_router"
WIRING_PATH = SRC / "terminal_wiring.py"
WIRING = WIRING_PATH.read_text(encoding="utf-8")

#: LTW-2 —— `close_run` 调用点的**精确 keyword 形态**：四个实参必须与四个形参**同名**。
EXPECTED_CALL_SITE_SHAPES = {
    "converge_run": {
        "terminal_state": ("terminal_state",),
        "no_pending_approval": ("no_pending_approval",),
        "no_pending_recovery": ("no_pending_recovery",),
        "ended_at": ("ended_at",),
    }
}

#: LTW-4 —— 两个入口传给 `_settle` 的**完整** keyword 形态：
#: `state` **恰好**是两个映射（不得发明终态），身份两参**恰好**取自见证（不反查运行态）。
EXPECTED_SETTLE_STATE_SHAPES = {
    "settle_pass": {
        "branch_id": ("checked.branch_return.branch_id",),
        "attempt": ("checked.branch_return.attempt",),
        "state": ("BranchState.COMPLETED",),
        "manager": ("manager",),
        "ended_at": ("ended_at",),
    },
    "settle_failure": {
        "branch_id": ("checked.branch_return.branch_id",),
        "attempt": ("checked.branch_return.attempt",),
        "state": ("BranchState.FAILED",),
        "manager": ("manager",),
        "ended_at": ("ended_at",),
    },
}

#: LTW-5 —— 模块级函数的**精确**签名形状（全 kw-only、无默认值、无 `*args` / `**kwargs`）。
EXPECTED_SIGNATURES = {
    "_require_manager": {
        "positional": ["manager"],
        "kwonly": ["entry"],
        "defaults": [False],
        "vararg": None,
        "kwarg": None,
    },
    "_require_outcome": {
        "positional": ["outcome"],
        "kwonly": ["passed", "entry"],
        "defaults": [False, False],
        "vararg": None,
        "kwarg": None,
    },
    "_settle": {
        "positional": [],
        "kwonly": ["branch_id", "attempt", "state", "manager", "ended_at"],
        "defaults": [False, False, False, False, False],
        "vararg": None,
        "kwarg": None,
    },
    "settle_pass": {
        "positional": [],
        "kwonly": ["outcome", "manager", "ended_at"],
        "defaults": [False, False, False],
        "vararg": None,
        "kwarg": None,
    },
    "settle_failure": {
        "positional": [],
        "kwonly": ["outcome", "manager", "ended_at"],
        "defaults": [False, False, False],
        "vararg": None,
        "kwarg": None,
    },
    "converge_run": {
        "positional": [],
        "kwonly": [
            "manager",
            "terminal_state",
            "no_pending_approval",
            "no_pending_recovery",
            "ended_at",
        ],
        "defaults": [False, False, False, False, False],
        "vararg": None,
        "kwarg": None,
    },
}

#: LTW-5 —— 返回标注（`converge_run` 必须 `-> None`：它**不传事实**）。
EXPECTED_RETURN_ANNOTATIONS = {
    "_require_manager": "AgentManager",
    "_require_outcome": "BranchValidationOutcome",
    "_settle": "BranchId",
    "settle_pass": "BranchId",
    "settle_failure": "BranchId",
    "converge_run": "None",
}

#: LTW-8 —— 受保护 artifact（Phase B **不得**触碰；基线见 README §99.15 / §100.2）。
PROTECTED_ARTIFACTS = {
    "src/adaptive_router/agent_manager.py": (
        "154B171E0F2A7D568FCD61318869EEA05DA0D9A4140640A6A89860D13D0D01C5"
    ),
    "src/adaptive_router/contracts/runtime.py": (
        "6466A4E628754002FB43515A2E2D15ECBF6398AE4204C07A256621673173F379"
    ),
    "src/adaptive_router/validation_wiring.py": (
        "CEB01E26592C4C902524E3F6A928EFF5B14BCCB7D83D0E8216F807B191CD5F52"
    ),
    "src/adaptive_router/recovery_wiring.py": (
        "B56266000A5186B7DC1EE22B3CA3B4F100839F7F47DCAC1C7FBFFE90553C2767"
    ),
    "src/adaptive_router/execution.py": (
        "BBAF6C29F8AD3793811D88D65EB204BF90D1589D3F69A7610E465CD90918C409"
    ),
    "docs/architecture.md": (
        "CB3FCE709C2548EF3C7676886CC921176E5B9C82AA10CDF506F6A18438FA1BB5"
    ),
}

#: LTW-9 —— 依赖白名单（精确集合）与"无源可推"的禁用方向。
#: ⚠️ `from __future__ import annotations` 会贡献两个 token（`__future__` / `annotations`）——
#: 它们是**语言级前向声明**，不是模块依赖 ⇒ 从白名单比对中显式扣除，其余一律精确相等。
FUTURE_TOKENS = {"__future__", "annotations"}
EXPECTED_IMPORT_MODULES = {"agent_manager", "contracts", "validation_wiring"}
EXPECTED_IMPORTED_NAMES = {
    "AgentManager",
    "BranchId",
    "BranchState",
    "TerminalState",
    "BranchValidationOutcome",
}
FORBIDDEN_IMPORT_TOKENS = (
    "recovery",
    "recovery_wiring",
    "recovery_context",
    "validation_judgment",
    "approval_judgment",
    "approval_disclosure",
    "approval_constraints",
    "policy",
    "policy_result",
    "execution",
    "scheduler",
    "adapters",
    "result_handoff",
    "execution_results",
    "agent_start_requests",
    "dispatch_decisions",
    "runtime_control_facts",
    "time",
    "datetime",
    "os",
)


def _mutate(old: str, new: str) -> str:
    """**合成变异源码**（只改字符串，不动 ``src/``）。

    ⚠️ 锚点没命中就**直接失败** —— 否则 probe 会在"变异根本没发生"的情况下假绿。
    """
    variant = WIRING.replace(old, new)
    assert variant != WIRING, f"probe 的锚点未命中，变异未生效：{old!r}"
    return variant


# --------------------------------------------------------------------------
# 测试替身（独立重建最小构造路径，不改既有测试文件）
# --------------------------------------------------------------------------


def bundle(goal: str = "g") -> ContextBundle:
    return ContextBundle(
        goal=goal,
        background=(),
        constraints=(),
        allowed_operations=(),
        forbidden_operations=(),
        output_format="text",
        completion_criteria="done",
        confirmed_conclusions=(),
        excluded=(),
        includes_history=False,
    )


def manager(*branch_ids: BranchId, limit: int = 3) -> AgentManager:
    return AgentManager(
        run_id=new_run_id(), branch_order=tuple(branch_ids), total_attempt_limit=limit
    )


def decide(branch_id: BranchId, action: DispatchAction) -> DispatchDecision:
    return DispatchDecision(
        branch_id=branch_id, action=action, reason=f"test-{action.value}"
    )


def start_and_commit(subject: AgentManager, branch_id: BranchId, attempt: int = 1) -> None:
    """走完两阶段：取 START 许可 → 用显式成功事实提交 RUNNING。"""
    subject.materialize(
        decisions=(decide(branch_id, DispatchAction.START),),
        bundles={branch_id: bundle(f"goal-{branch_id}")},
    )
    subject.commit_started(
        branch_id=branch_id,
        attempt=attempt,
        handle=f"h-{attempt}",
        started_at="2026-09-19T00:00:00Z",
    )


def branch_return(*, branch_id: BranchId, attempt: int = 1) -> BranchReturn:
    return BranchReturn(
        branch_id=branch_id,
        agent_id=new_agent_id(),
        attempt=attempt,
        model=ExecutionValue(
            planned=ModelTier.BALANCED,
            planned_source=ConfirmationSource.PLANNER,
            requested="gpt-5-codex",
            requested_source=ConfirmationSource.REQUEST,
            actual="gpt-5-codex-2025-xx",
            actual_source=ConfirmationSource.PROVIDER_API,
            observed=True,
            confidence=Confidence.HIGH,
        ),
        effort=ExecutionValue(
            planned=EffortLevel.MEDIUM,
            planned_source=ConfirmationSource.PLANNER,
            requested=EffortLevel.MEDIUM,
            requested_source=ConfirmationSource.REQUEST,
            actual=EffortLevel.HIGH,
            actual_source=ConfirmationSource.PROVIDER_API,
            observed=True,
            confidence=Confidence.HIGH,
        ),
        conclusion_summary="done",
        key_evidence=("e1",),
        validation_state="ok",
        residual_risks=(),
        main_agent_todo=(),
    )


def validation_record(*, branch_id: BranchId, passed: bool) -> ValidationRecord:
    return ValidationRecord(
        branch_id=branch_id,
        passed=passed,
        evidence=("e1",),
        failed_criteria=() if passed else ("1) 文档已更新",),
        detail="依据齐全" if passed else "一条未满足",
    )


def outcome(
    *, branch_id: BranchId, attempt: int = 1, passed: bool = True
) -> BranchValidationOutcome:
    """一次真实配对事件的见证（同一次事件的两个对象）。"""
    return BranchValidationOutcome(
        branch_return=branch_return(branch_id=branch_id, attempt=attempt),
        validation=validation_record(branch_id=branch_id, passed=passed),
    )


def run_for(subject: AgentManager, branch_id: BranchId, attempt: int = 1):
    for record in subject.routing_run.branches:
        if record.branch_id == branch_id and record.attempt == attempt:
            return record
    raise AssertionError(f"{branch_id} 的 attempt {attempt} 不存在")


def running(*, passed: bool = True, attempt: int = 1):
    """一个「已 RUNNING 的单分支 Run + 该分支的配对见证」。"""
    branch_id = new_branch_id()
    subject = manager(branch_id)
    start_and_commit(subject, branch_id, attempt=attempt)
    return subject, outcome(branch_id=branch_id, attempt=attempt, passed=passed)


def converge(subject: AgentManager, state: TerminalState = TerminalState.SUCCESS, **over):
    kwargs = {
        "manager": subject,
        "terminal_state": state,
        "no_pending_approval": True,
        "no_pending_recovery": True,
        "ended_at": "T-run-end",
    }
    kwargs.update(over)
    return converge_run(**kwargs)


def close_all(subject: AgentManager) -> None:
    """把所有 RUNNING 分支收尾（走接线层），使 4.5 条件①成立。"""
    for record in subject.routing_run.branches:
        if record.state is BranchState.RUNNING:
            settle_pass(
                outcome=outcome(branch_id=record.branch_id, attempt=record.attempt),
                manager=subject,
                ended_at="T-branch-end",
            )


# ==========================================================================
# LTW-1 —— close_run 的唯一调用点
# ==========================================================================


class TestLTW1SingleCloseRunCallSite:
    def test_close_run_is_called_exactly_once_in_repo(self) -> None:
        hits = []
        for path in sorted(SRC.rglob("*.py")):
            source = path.read_text(encoding="utf-8")
            for _, lineno in calls_to(source, ("close_run",)):
                hits.append(f"{path.relative_to(REPO_ROOT).as_posix()}:{lineno}")
        assert len(hits) == 1, f"close_run 调用点必须全 repo 唯一，实测 {hits}"

    def test_the_single_call_site_is_in_terminal_wiring(self) -> None:
        hits = [
            path
            for path in sorted(SRC.rglob("*.py"))
            if calls_to(path.read_text(encoding="utf-8"), ("close_run",))
        ]
        assert [p.name for p in hits] == ["terminal_wiring.py"]

    def test_the_single_call_site_lives_in_converge_run(self) -> None:
        assert set(call_keyword_shapes_by_caller(WIRING, "close_run")) == {"converge_run"}

    def test_probe_a_second_call_site_goes_red(self) -> None:
        variant = _mutate(
            "    checked.close_run(",
            "    manager.close_run(\n        terminal_state=terminal_state,\n"
            "        no_pending_approval=True,\n        no_pending_recovery=True,\n"
            "        ended_at=ended_at,\n    )\n    checked.close_run(",
        )
        assert len(calls_to(variant, ("close_run",))) == 2


# ==========================================================================
# LTW-2 —— 实参必须是形参本身（A18.4 四条禁止的接线层形式）
# ==========================================================================


class TestLTW2ArgumentsAreParameters:
    def test_close_run_arguments_are_exactly_the_parameter_names(self) -> None:
        assert call_keyword_shapes_by_caller(WIRING, "close_run") == (
            EXPECTED_CALL_SITE_SHAPES
        )

    def test_no_star_star_escape_hatch(self) -> None:
        shapes = call_keyword_shapes_by_caller(WIRING, "close_run")
        assert "**" not in shapes["converge_run"]

    def test_fact_parameters_are_never_rebound(self) -> None:
        """⚠️ 就地补强（§100.2）：**先算出来再传同一个名字**必须同样变红。

        只锁"实参是形参本身"是不够的 —— ``terminal_state = <自算值>`` 之后调用点看上去仍然
        是 ``terminal_state=terminal_state``。本模块的**唯一**局部绑定必须只有守卫收窄的
        ``checked``；任何新增绑定（尤其四个事实与 ``manager``）一律红。
        """
        bound = set(assignment_value_shapes(WIRING)) | set(annotated_assign_names(WIRING))
        assert bound == {"__all__", "checked"}
        shapes = assignment_value_shapes(WIRING)
        assert len(shapes["checked"]) == 4
        assert all(value.startswith("_require_") for value in shapes["checked"])

    def test_probe_rebound_fact_goes_red(self) -> None:
        variant = _mutate(
            "    checked = _require_manager(manager, entry=\"converge_run\")",
            "    terminal_state = TerminalState.SUCCESS\n"
            "    checked = _require_manager(manager, entry=\"converge_run\")",
        )
        bound = set(assignment_value_shapes(variant)) | set(
            annotated_assign_names(variant)
        )
        assert bound != {"__all__", "checked"}

    def test_probe_literal_true_goes_red(self) -> None:
        variant = _mutate(
            "        no_pending_approval=no_pending_approval,",
            "        no_pending_approval=True,",
        )
        shapes = call_keyword_shapes_by_caller(variant, "close_run")
        assert shapes != EXPECTED_CALL_SITE_SHAPES
        assert shapes["converge_run"]["no_pending_approval"] == ("True",)

    def test_probe_derived_expression_goes_red(self) -> None:
        variant = _mutate(
            "        no_pending_recovery=no_pending_recovery,",
            "        no_pending_recovery=no_pending_approval,",
        )
        shapes = call_keyword_shapes_by_caller(variant, "close_run")
        assert shapes["converge_run"]["no_pending_recovery"] == ("no_pending_approval",)

    def test_probe_kwargs_splat_goes_red(self) -> None:
        variant = _mutate(
            "        ended_at=ended_at,\n    )",
            "        ended_at=ended_at,\n        **facts,\n    )",
        )
        shapes = call_keyword_shapes_by_caller(variant, "close_run")
        assert "**" in shapes["converge_run"]


# ==========================================================================
# LTW-3 —— 接线不写 terminal
# ==========================================================================


class TestLTW3NoTerminalWrite:
    def test_no_run_terminal_construction(self) -> None:
        assert calls_to(WIRING, ("RunTerminal",)) == []

    def test_run_terminal_is_not_even_referenced(self) -> None:
        assert "RunTerminal" not in referenced_names(WIRING)

    def test_no_private_terminal_access(self) -> None:
        assert "_terminal" not in referenced_names(WIRING)

    def test_no_attribute_assignment_at_all(self) -> None:
        assert attribute_assignment_targets(WIRING) == ()

    def test_probe_self_made_terminal_goes_red(self) -> None:
        variant = _mutate(
            "    checked = _require_manager(manager, entry=\"converge_run\")",
            "    manager._terminal = RunTerminal(\n"
            "        run_id=manager.routing_run.run_id, state=terminal_state\n"
            "    )\n"
            "    checked = _require_manager(manager, entry=\"converge_run\")",
        )
        assert calls_to(variant, ("RunTerminal",)) != []
        assert attribute_assignment_targets(variant) != ()
        assert "_terminal" in referenced_names(variant)


# ==========================================================================
# LTW-4 —— 唯一收尾点 + 映射固定
# ==========================================================================


class TestLTW4SingleSettlePoint:
    def test_record_outcome_is_called_exactly_once(self) -> None:
        assert len(calls_to(WIRING, ("record_outcome",))) == 1

    def test_the_single_settle_point_is_settle(self) -> None:
        assert set(call_keyword_shapes_by_caller(WIRING, "record_outcome")) == {"_settle"}

    def test_record_outcome_arguments_are_the_parameter_names(self) -> None:
        shapes = call_keyword_shapes_by_caller(WIRING, "record_outcome")["_settle"]
        assert shapes == {
            "branch_id": ("branch_id",),
            "attempt": ("attempt",),
            "state": ("state",),
            "ended_at": ("ended_at",),
        }

    def test_state_mapping_is_exactly_completed_and_failed(self) -> None:
        assert call_keyword_shapes_by_caller(WIRING, "_settle") == (
            EXPECTED_SETTLE_STATE_SHAPES
        )

    def test_probe_second_settle_point_goes_red(self) -> None:
        variant = _mutate(
            "    checked = _require_manager(manager, entry=\"converge_run\")",
            "    manager.record_outcome(\n"
            "        branch_id=branch_id, attempt=1, state=BranchState.COMPLETED,\n"
            "        ended_at=ended_at,\n    )\n"
            "    checked = _require_manager(manager, entry=\"converge_run\")",
        )
        assert len(calls_to(variant, ("record_outcome",))) == 2

    def test_probe_invented_terminal_state_goes_red(self) -> None:
        variant = _mutate("state=BranchState.COMPLETED,", "state=BranchState.ADOPTED,")
        assert call_keyword_shapes_by_caller(variant, "_settle") != (
            EXPECTED_SETTLE_STATE_SHAPES
        )


# ==========================================================================
# LTW-5 —— 全显式签名（无默认 / 无 *args / 无 **kwargs）
# ==========================================================================


class TestLTW5ExplicitSignatures:
    def test_function_inventory_is_exact(self) -> None:
        assert set(module_level_function_names(WIRING)) == set(EXPECTED_SIGNATURES)

    def test_every_signature_is_kw_only_without_defaults(self) -> None:
        for name, expected in EXPECTED_SIGNATURES.items():
            assert function_signature_shape(WIRING, name) == expected, name

    def test_return_annotations_are_explicit(self) -> None:
        for name, expected in EXPECTED_RETURN_ANNOTATIONS.items():
            assert function_return_annotation(WIRING, name) == expected, name

    def test_probe_kwonly_default_goes_red(self) -> None:
        variant = _mutate(
            "    no_pending_approval: bool,", "    no_pending_approval: bool = True,"
        )
        shape = function_signature_shape(variant, "converge_run")
        # kw-only 的默认值允许出现在任意位置（`def f(*, a=True, b)` 合法）⇒ 按位置断言。
        assert shape["defaults"] == [False, False, True, False, False]
        assert shape != EXPECTED_SIGNATURES["converge_run"]

    def test_probe_positional_parameter_goes_red(self) -> None:
        variant = _mutate(
            "def converge_run(\n    *,\n    manager: AgentManager,",
            "def converge_run(\n    manager: AgentManager,\n    *,",
        )
        assert function_signature_shape(variant, "converge_run") != (
            EXPECTED_SIGNATURES["converge_run"]
        )

    def test_probe_var_kwargs_goes_red(self) -> None:
        variant = _mutate(
            "    ended_at: str,\n) -> None:",
            "    ended_at: str,\n    **facts: object,\n) -> None:",
        )
        assert function_signature_shape(variant, "converge_run")["kwarg"] == "facts"


# ==========================================================================
# LTW-6 —— 不读时钟
# ==========================================================================


class TestLTW6NoClock:
    def test_no_clock_calls(self) -> None:
        assert clock_call_hits(WIRING) == []

    def test_ended_at_is_never_assigned(self) -> None:
        assert "ended_at" not in assignment_value_shapes(WIRING)
        assert "ended_at" not in annotated_assign_names(WIRING)

    def test_no_time_module_imported(self) -> None:
        assert imported_module_tokens(WIRING) & {"time", "datetime", "os"} == set()

    def test_probe_self_read_clock_goes_red(self) -> None:
        variant = _mutate(
            "    checked = _require_manager(manager, entry=\"converge_run\")",
            "    ended_at = datetime.now().isoformat()\n"
            "    checked = _require_manager(manager, entry=\"converge_run\")",
        )
        assert clock_call_hits(variant) != []
        assert "ended_at" in assignment_value_shapes(variant)


# ==========================================================================
# LTW-7 —— 不碰 attempt lifecycle / 不产决策
# ==========================================================================


class TestLTW7NoAttemptLifecycle:
    def test_no_materialize_commit_or_control_calls(self) -> None:
        assert calls_to(
            WIRING, ("materialize", "commit_started", "apply_runtime_control")
        ) == []

    def test_no_routing_run_construction(self) -> None:
        assert calls_to(WIRING, ("RoutingRun",)) == []
        assert "RoutingRun" not in referenced_names(WIRING)

    def test_no_recovery_decision_is_produced(self) -> None:
        assert calls_to(WIRING, ("decide_recovery", "route_failed_branch")) == []
        assert referenced_names(WIRING) & {
            "RecoveryDecision",
            "RecoveryOutcome",
            "RecoveryContext",
            "decide_recovery",
            "route_failed_branch",
        } == set()

    def test_module_is_stateless(self) -> None:
        assert module_level_container_assignments(WIRING) == ()
        assert annotated_assign_names(WIRING) == ()

    def test_probe_new_attempt_goes_red(self) -> None:
        variant = _mutate(
            "    checked = _require_manager(manager, entry=\"converge_run\")",
            "    manager.materialize(decisions=(), bundles={})\n"
            "    checked = _require_manager(manager, entry=\"converge_run\")",
        )
        assert calls_to(variant, ("materialize",)) != []

    def test_probe_decision_in_wiring_goes_red(self) -> None:
        variant = _mutate(
            "    checked = _require_outcome(outcome, passed=False, entry=\"settle_failure\")",
            "    checked = _require_outcome(outcome, passed=False, entry=\"settle_failure\")\n"
            "    decide_recovery(context=None)",
        )
        assert calls_to(variant, ("decide_recovery",)) != []


# ==========================================================================
# LTW-8 —— 受保护 artifact 逐字节不变
# ==========================================================================




# ==========================================================================
# LTW-9 —— 依赖白名单（无源可推）
# ==========================================================================


class TestLTW9ImportWhitelist:
    def test_imported_modules_are_exactly_the_whitelist(self) -> None:
        assert imported_module_tokens(WIRING) - FUTURE_TOKENS == EXPECTED_IMPORT_MODULES

    def test_imported_names_are_exactly_the_whitelist(self) -> None:
        assert imported_names(WIRING) - FUTURE_TOKENS == EXPECTED_IMPORTED_NAMES

    def test_no_forbidden_dependency(self) -> None:
        assert forbidden_import_hits(WIRING, FORBIDDEN_IMPORT_TOKENS) == set()

    def test_probe_recovery_import_goes_red(self) -> None:
        variant = _mutate(
            "from .agent_manager import AgentManager",
            "from .agent_manager import AgentManager\nfrom .recovery import decide_recovery",
        )
        assert imported_module_tokens(variant) != EXPECTED_IMPORT_MODULES
        assert forbidden_import_hits(variant, FORBIDDEN_IMPORT_TOKENS) != set()

    def test_probe_approval_import_goes_red(self) -> None:
        variant = _mutate(
            "from .contracts import BranchId, BranchState, TerminalState",
            "from .contracts import BranchId, BranchState, TerminalState\n"
            "from .approval_judgment import judge_approval",
        )
        assert forbidden_import_hits(variant, FORBIDDEN_IMPORT_TOKENS) != set()


# ==========================================================================
# 行为测试 B1 – B9
# ==========================================================================


class TestSettleBehaviour:
    def test_b1_settle_pass_closes_the_attempt_as_completed(self) -> None:
        subject, witness = running(passed=True)
        branch_id = subject.routing_run.branches[0].branch_id
        returned = settle_pass(outcome=witness, manager=subject, ended_at="T-end")
        assert returned == branch_id
        record = run_for(subject, branch_id)
        assert record.state is BranchState.COMPLETED
        assert record.ended_at == "T-end"

    def test_b2_settle_failure_closes_the_attempt_as_failed(self) -> None:
        subject, witness = running(passed=False)
        branch_id = subject.routing_run.branches[0].branch_id
        returned = settle_failure(outcome=witness, manager=subject, ended_at="T-end")
        assert returned == branch_id
        record = run_for(subject, branch_id)
        assert record.state is BranchState.FAILED
        assert record.ended_at == "T-end"

    def test_b3_wrong_entry_is_an_explicit_failure_not_a_wrong_terminal(self) -> None:
        subject, pass_witness = running(passed=True)
        branch_id = subject.routing_run.branches[0].branch_id
        with pytest.raises(TerminalWiringError):
            settle_failure(outcome=pass_witness, manager=subject, ended_at="T-end")
        assert run_for(subject, branch_id).state is BranchState.RUNNING

        subject2, fail_witness = running(passed=False)
        branch_id2 = subject2.routing_run.branches[0].branch_id
        with pytest.raises(TerminalWiringError):
            settle_pass(outcome=fail_witness, manager=subject2, ended_at="T-end")
        assert run_for(subject2, branch_id2).state is BranchState.RUNNING

    def test_b3_non_outcome_and_non_manager_are_explicit_failures(self) -> None:
        subject, witness = running()
        with pytest.raises(TerminalWiringError):
            settle_pass(outcome="nope", manager=subject, ended_at="T-end")  # type: ignore[arg-type]
        with pytest.raises(TerminalWiringError):
            settle_pass(outcome=witness, manager=None, ended_at="T-end")  # type: ignore[arg-type]

    def test_b4_identity_comes_from_the_pairing_event(self) -> None:
        """`attempt` 取自见证（A11.4b），**不反查运行态**：见证说 2 就只能写 2。"""
        subject, _ = running()
        branch_id = subject.routing_run.branches[0].branch_id
        stale = outcome(branch_id=branch_id, attempt=2, passed=True)
        with pytest.raises(AgentManagerError):
            settle_pass(outcome=stale, manager=subject, ended_at="T-end")
        assert run_for(subject, branch_id, attempt=1).state is BranchState.RUNNING

    def test_same_attempt_cannot_be_settled_twice(self) -> None:
        subject, witness = running(passed=True)
        settle_pass(outcome=witness, manager=subject, ended_at="T-end")
        with pytest.raises(AgentManagerError):
            settle_pass(outcome=witness, manager=subject, ended_at="T-end-2")

    def test_b8_settle_never_creates_an_attempt(self) -> None:
        subject, witness = running(passed=False)
        before = subject.routing_run.branches
        settle_failure(outcome=witness, manager=subject, ended_at="T-end")
        after = subject.routing_run.branches
        assert len(after) == len(before)
        assert {r.attempt for r in after} == {1}
        assert subject.routing_run.attempt == 1

    def test_a18_6_fail_side_order_is_composable(self) -> None:
        """**先关 attempt，再产决策**：settle_failure 之后，决策才由 `[14]` 的接线产出。"""
        subject, witness = running(passed=False)
        branch_id = subject.routing_run.branches[0].branch_id
        settle_failure(outcome=witness, manager=subject, ended_at="T-end")
        assert run_for(subject, branch_id).state is BranchState.FAILED
        decision = route_failed_branch(outcome=witness, total_attempt_limit=3)
        assert decision.branch_id == branch_id
        assert decision.should_recover is True


class TestConvergeBehaviour:
    def test_b5_all_facts_present_writes_the_run_terminal(self) -> None:
        subject, _ = running()
        close_all(subject)
        converge(subject, ended_at="T-run-end")
        run = subject.routing_run
        assert run.terminal is not None
        assert run.terminal.state is TerminalState.SUCCESS
        assert run.terminal.run_id == run.run_id
        assert run.control_flow_converged is True
        assert not hasattr(run, "ended_at")  # ended_at 只作审计输入，不落库（LT-3 / P3）

    def test_terminal_states_are_passed_through_verbatim(self) -> None:
        for state in (
            TerminalState.TERMINAL_FAILURE,
            TerminalState.USER_CANCELLED,
            TerminalState.APPROVAL_DENIED,
            TerminalState.ABORTED_NO_CAPABILITY,
            TerminalState.SUPERSEDED,
        ):
            subject, _ = running()
            close_all(subject)
            converge(subject, state)
            assert subject.routing_run.terminal.state is state

    def test_b6_missing_or_false_fact_refuses_the_terminal(self) -> None:
        for appro, recov in ((False, True), (True, False), (False, False)):
            subject, _ = running()
            close_all(subject)
            with pytest.raises(AgentManagerError):
                converge(
                    subject,
                    no_pending_approval=appro,
                    no_pending_recovery=recov,
                )
            assert subject.routing_run.terminal is None
            assert subject.routing_run.control_flow_converged is False

    def test_b6_non_bool_fact_refuses_the_terminal(self) -> None:
        for bad in (1, 0, "true", None):
            subject, _ = running()
            close_all(subject)
            with pytest.raises(AgentManagerError):
                converge(subject, no_pending_approval=bad)
            assert subject.routing_run.terminal is None

    def test_b6_unfinished_branch_refuses_the_terminal(self) -> None:
        subject, _ = running()
        with pytest.raises(AgentManagerError):
            converge(subject)
        assert subject.routing_run.terminal is None

    def test_b7_second_convergence_is_an_explicit_failure(self) -> None:
        subject, _ = running()
        close_all(subject)
        converge(subject)
        with pytest.raises(AgentManagerError):
            converge(subject)
        assert subject.routing_run.terminal.state is TerminalState.SUCCESS

    def test_b9_terminal_state_type_is_not_coerced(self) -> None:
        for bad in ("success", BranchState.COMPLETED, None):
            subject, _ = running()
            close_all(subject)
            with pytest.raises(AgentManagerError):
                converge(subject, terminal_state=bad)  # type: ignore[arg-type]
            assert subject.routing_run.terminal is None

    def test_ended_at_must_be_an_explicit_non_empty_string(self) -> None:
        for bad in ("", "   ", None, 1):
            subject, _ = running()
            close_all(subject)
            with pytest.raises(AgentManagerError):
                converge(subject, ended_at=bad)
            assert subject.routing_run.terminal is None

    def test_manager_guard_is_an_explicit_failure(self) -> None:
        with pytest.raises(TerminalWiringError):
            converge_run(  # type: ignore[arg-type]
                manager=None,
                terminal_state=TerminalState.SUCCESS,
                no_pending_approval=True,
                no_pending_recovery=True,
                ended_at="T",
            )

    def test_missing_fact_is_a_type_error_not_a_default(self) -> None:
        """缺任一 fact ⇒ **调用即失败**（无默认值）—— 「缺省即真」在接线层同样不存在。"""
        subject, _ = running()
        close_all(subject)
        with pytest.raises(TypeError):
            converge_run(  # type: ignore[call-arg]
                manager=subject,
                terminal_state=TerminalState.SUCCESS,
                no_pending_recovery=True,
                ended_at="T",
            )
        assert subject.routing_run.terminal is None

    def test_zero_branch_run_converges_vacuously(self) -> None:
        """4.5 条件①在 0 分支时**真空成立**（C3）—— 接线层不额外发明条件。"""
        subject = manager()
        converge(subject)
        assert subject.routing_run.terminal is not None
        assert subject.routing_run.control_flow_converged is True
