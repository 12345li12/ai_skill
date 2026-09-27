"""``C2d-1 DENY 收口执行链`` —— 结构锁 **LDW-1 – LDW-12** + 永久 probe + 行为 **B1 – B7**。

架构依据：`docs/architecture.md` **2 节 Approval Gate 节点行**（"三种输入的控制流出口（V1 冻结）"：
`DENY → 终态 APPROVAL_DENIED`）· **6.5 终态枚举**（`APPROVAL_DENIED` = 审批未通过，L43）·
**`INV-17`**（终态取自 6 值枚举）· **4.5 双条件**（分支全终止 + 控制流收敛；`C3`：0 分支时条件①真空成立）·
**A18.2**（`[10]` 是 `RunTerminal` 的唯一写者）· **A18.3**（两个收敛事实的唯一合法来源）·
**A13.1 / A13.6**（branch 级终态的写入口边界）· **6.4 / L47**（`[10]` 运行态唯一写者）。
范围、冻结、授权与实现记录见 `tests/behavior/README_step5b.md` **§164 – §167**。

**本模块要守住的六条主张：**

  1. **不重新定义 DENY 语义**：`DENY → APPROVAL_DENIED` 早已冻结；只补机械执行者；
  2. **单一收敛路径**：只经 `[10].close_run`（`LDW-3`）；
  3. **两个收敛事实都是"观测"**（`LDW-6'`）：分别引用 `gate_outcome.` 与 `manager.routing_run.`
     ⇒ **不是** caller assertion、**不是**写死常量；
  4. **架构时序保护**（`LDW-12`）：`branches == ()` 是机械断言，Gate 若被移到派发之后会**报错**；
  5. **零出边 / 零时钟 / 零 `try`**（`LDW-7` / `LDW-9` / `LDW-10`）；
  6. **五字段载体**：`gate_settled` / `branch_count` 是**验证回执**，不是业务数据。

⚠️ **禁止子串扫描**：本文件 docstring 里**故意提及** `dispatch` / `scheduler` / `attempt` /
`close_run` 等词 ⇒ 所有锁一律 **AST 级 / 绑定集级**。
⚠️ 行为测试的**非法输入**构造需要 `_mutate`/直接注入 —— 一律**不覆写** `src/`；
真实源码的一次性变异证据见 README §167。
"""

from __future__ import annotations

import ast
import dataclasses
import pathlib

import pytest

from adaptive_router.agent_manager import AgentManager, AgentManagerError
from adaptive_router.approval_gate_wiring import (
    ApprovalJudgment,
    GateDecision,
    GateDecisionKind,
    GateOutcome,
    settle_gate,
)
from adaptive_router.contracts import AgentRun, BranchState, ContractError, RunId, TerminalState
from adaptive_router.deny_wiring import DenySettlement, DenyWiringError, settle_denied_run

from _deny_wiring_locks import (  # noqa: F401  (复用既有检测器，不抄第二套)
    calls_to,
    clock_call_hits,
    function_return_shapes,
    function_signature_shape,
    imported_module_tokens,
    module_level_function_names,
    referenced_names,
)

WIRING_PATH = pathlib.Path("src/adaptive_router/deny_wiring.py")
WIRING = WIRING_PATH.read_text(encoding="utf-8")

#: `§165` 五字段锁死（顺序与集合都锁）。
EXPECTED_FIELDS = ["run_id", "terminal_state", "gate_settled", "ended_at", "branch_count"]

#: `LDW-3` 白名单：只允许调这一个**合法终态接线入口**（集合**相等**，不是"包含"）。
#: ⚠️ 锁目标 = "合法终态接线入口"，**不是**"直接写入口" —— 见 `§168` 的 seam 调整裁定：
#: `close_run` 的机械调用点全仓唯一（`terminal_wiring.py`，`LTW-1` 不变量），本模块**不得**成为第二处。
ALLOWED_CONVERGENCE_ENTRY = {"converge_run"}

#: `LDW-3` 反向清单：终态写入口与其它运行态写入口一个都不许被调用。
FORBIDDEN_WRITE_ENTRIES = {
    "close_run",
    "commit_started",
    "record_outcome",
    "apply_runtime_control",
    "materialize",
    "resume_from_waiting",
}

#: `LDW-7` 零出边：这些标识符一个都不许进入绑定集。
FORBIDDEN_MOTION_NAMES = {
    "route_to",
    "next_node",
    "next_step",
    "dispatch",
    "dispatcher",
    "scheduler",
    "plan",
    "steps",
    "remaining",
}

#: `LDW-11` 构造禁区。
FORBIDDEN_CONSTRUCTIONS = {
    "GateOutcome",
    "GateDecision",
    "RoutingRun",
    "RunTerminal",
    "RecoveryDecision",
    "ProgressClaim",
}

#: `LDW-8` import 白名单（**精确相等**；`terminal_wiring` 是 `§168` 批准的**唯一新增依赖**）。
EXPECTED_IMPORT_TOKENS = {
    "__future__",
    "agent_manager",
    "annotations",
    "approval_gate_wiring",
    "contracts",
    "dataclass",
    "dataclasses",
    "terminal_wiring",
}

RUN_ID = "run_deny"
BRANCH_ID = "br_deny"


def _judgment() -> ApprovalJudgment:
    return ApprovalJudgment(
        needs_approval=True,
        proposed_action="重启服务",
        impact_scope="生产集群",
        main_risk="短时不可用",
        recovery_method="回滚上一版本",
    )


def _outcome(kind: GateDecisionKind) -> GateOutcome:
    modification = "只重启一个副本" if kind is GateDecisionKind.MODIFY else None
    return settle_gate(
        judgment=_judgment(), decision=GateDecision(kind=kind, modification=modification)
    )


def _pending() -> GateOutcome:
    return settle_gate(judgment=_judgment(), decision=None)


def _manager(*, with_branch: bool = False, run_id: RunId = RUN_ID) -> AgentManager:
    manager = AgentManager(run_id=run_id, branch_order=(BRANCH_ID,), total_attempt_limit=2)
    if with_branch:
        manager._records.append(
            AgentRun(
                run_id=run_id,
                branch_id=BRANCH_ID,
                agent_id="ag_deny",
                attempt=1,
                state=BranchState.RUNNING,
                started_at="2026-09-22T00:00:00Z",
                ended_at=None,
                handle="h-deny",
            )
        )
    return manager


def _settlement_keywords(source: str) -> dict[str, str]:
    """**测试侧局部** AST 助手：取出 `converge_run(...)` 调用的 keyword 实参**源码形态**。

    ⚠️ 为什么需要它：既有 ``call_keyword_shapes_by_caller`` **只遍历模块级函数**
    （它自己的实现显式跳过类内方法），且按"被调用者名"分组时**无法**只挑出
    `converge_run` 这一个调用 —— 本助手补上这一段；**只**看 `converge_run`
    这一个被调用者（不做通用化，避免变成第二套判定体系）。
    ⚠️ 目标从 `close_run` 改为 `converge_run` 是 `§168` 批准的 seam 调整：
    `deny_wiring` **不得**成为 `close_run` 的第二处机械调用点（`LTW-1` 不变量）。
    """
    found: dict[str, str] = {}
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else (
            func.id if isinstance(func, ast.Name) else None
        )
        if name != "converge_run":
            continue
        for keyword in node.keywords:
            found[keyword.arg if keyword.arg is not None else "**"] = ast.unparse(keyword.value)
    return found


def _driver_assign_count(source: str) -> int:
    functions = [
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.FunctionDef) and node.name == "settle_denied_run"
    ]
    assert len(functions) == 1
    return sum(1 for node in ast.walk(functions[0]) if isinstance(node, ast.Assign))


def _try_count(source: str) -> int:
    return sum(1 for node in ast.walk(ast.parse(source)) if isinstance(node, ast.Try))


# ======================================================================
# LDW-1 … LDW-12 —— 结构锁
# ======================================================================


class TestLDW1ModuleShape:
    def test_exactly_one_module_level_function(self) -> None:
        assert module_level_function_names(WIRING) == ("settle_denied_run",)

    def test_exactly_three_module_level_objects(self) -> None:
        import adaptive_router.deny_wiring as module

        declared = sorted(
            name
            for name, value in vars(module).items()
            if not name.startswith("__") and getattr(value, "__module__", None) == module.__name__
        )
        assert declared == ["DenySettlement", "DenyWiringError", "settle_denied_run"]
        assert len(declared) == 3

    def test_public_surface(self) -> None:
        import adaptive_router.deny_wiring as module

        assert module.__all__ == ["DenyWiringError", "DenySettlement", "settle_denied_run"]

    def test_probe_fourth_module_object_goes_red(self) -> None:
        assert "    return DenySettlement(" in WIRING
        mutated = WIRING.replace(
            "def settle_denied_run(",
            "def _extra_helper() -> None:\n    return None\n\n\ndef settle_denied_run(",
            1,
        )
        assert mutated != WIRING
        assert module_level_function_names(mutated) == ("_extra_helper", "settle_denied_run")
        assert len(module_level_function_names(mutated)) == 2


class TestLDW2SingleReturn:
    def test_exactly_one_return_with_report_shape(self) -> None:
        shapes = function_return_shapes(WIRING, "settle_denied_run")
        assert len(shapes) == 1
        assert shapes[0].startswith("DenySettlement(")

    def test_driver_has_zero_local_bindings(self) -> None:
        assert _driver_assign_count(WIRING) == 0

    def test_no_none_return_path(self) -> None:
        assert "return None" not in WIRING

    def test_probe_second_return_goes_red(self) -> None:
        mutated = WIRING.replace(
            "    return DenySettlement(",
            "    if False:\n        return None\n    return DenySettlement(",
            1,
        )
        assert mutated != WIRING
        assert len(function_return_shapes(mutated, "settle_denied_run")) == 2


class TestLDW3SingleConvergenceEntry:
    def test_convergence_entry_whitelist_is_exactly_hit(self) -> None:
        assert (
            {name for name, _ in calls_to(WIRING, ALLOWED_CONVERGENCE_ENTRY)}
            == ALLOWED_CONVERGENCE_ENTRY
        )

    def test_no_write_entry_is_called_directly(self) -> None:
        assert calls_to(WIRING, FORBIDDEN_WRITE_ENTRIES) == []

    def test_probe_direct_close_run_goes_red(self) -> None:
        """**关键 probe**：直接调 `close_run` 会破坏 `LTW-1`（全仓唯一机械调用点）。"""
        mutated = WIRING.replace(
            "    converge_run(",
            "    manager.close_run(",
            1,
        )
        assert mutated != WIRING
        assert calls_to(mutated, {"close_run"}) != []

    def test_probe_second_write_entry_goes_red(self) -> None:
        mutated = WIRING.replace(
            "    return DenySettlement(",
            "    manager.record_outcome(\n"
            f"        branch_id='{BRANCH_ID}', attempt=1, state=BranchState.FAILED, ended_at=ended_at\n"
            "    )\n"
            "    return DenySettlement(",
            1,
        )
        assert mutated != WIRING
        assert calls_to(mutated, FORBIDDEN_WRITE_ENTRIES) != []


class TestLDW4NoAttemptAdvance:
    def test_does_not_call_the_attempt_advance_entry(self) -> None:
        assert calls_to(WIRING, {"commit_started"}) == []

    def test_no_attempt_assignment_in_code(self) -> None:
        functions = [
            node
            for node in ast.walk(ast.parse(WIRING))
            if isinstance(node, ast.FunctionDef) and node.name == "settle_denied_run"
        ]
        targets = {
            target.id
            for node in ast.walk(functions[0])
            if isinstance(node, ast.Assign)
            for target in node.targets
            if isinstance(target, ast.Name)
        }
        assert "attempt" not in targets

    def test_probe_attempt_increment_goes_red(self) -> None:
        mutated = WIRING.replace(
            "    converge_run(",
            "    _probe_attempt = 1\n    converge_run(",
            1,
        )
        assert mutated != WIRING
        assert _driver_assign_count(mutated) == 1


class TestLDW5TerminalStateConstant:
    def test_terminal_state_argument_is_the_frozen_constant(self) -> None:
        keywords = _settlement_keywords(WIRING)
        assert keywords["terminal_state"] == "TerminalState.APPROVAL_DENIED"

    def test_returned_terminal_state_is_the_frozen_constant(self) -> None:
        shapes = function_return_shapes(WIRING, "settle_denied_run")[0]
        assert "terminal_state=TerminalState.APPROVAL_DENIED" in shapes

    def test_no_other_terminal_state_member_is_referenced(self) -> None:
        referenced = {node.attr for node in ast.walk(ast.parse(WIRING)) if isinstance(node, ast.Attribute)}
        assert referenced & {
            "SUCCESS",
            "USER_CANCELLED",
            "TERMINAL_FAILURE",
            "ABORTED_NO_CAPABILITY",
            "SUPERSEDED",
        } == set()

    def test_probe_other_terminal_state_goes_red(self) -> None:
        mutated = WIRING.replace(
            "terminal_state=TerminalState.APPROVAL_DENIED",
            "terminal_state=TerminalState.SUCCESS",
            1,
        )
        assert mutated != WIRING
        assert _settlement_keywords(mutated)["terminal_state"] == "TerminalState.SUCCESS"


class TestLDW6ExactlyThreeParameters:
    def test_signature_is_kwonly_with_exactly_three_names(self) -> None:
        shape = function_signature_shape(WIRING, "settle_denied_run")
        assert shape["positional"] == []
        assert shape["kwonly"] == ["gate_outcome", "manager", "ended_at"]
        assert shape["defaults"] == [False, False, False]

    def test_no_pending_fact_is_a_parameter(self) -> None:
        shape = function_signature_shape(WIRING, "settle_denied_run")
        assert "no_pending_approval" not in shape["kwonly"]
        assert "no_pending_recovery" not in shape["kwonly"]

    def test_no_terminal_state_parameter(self) -> None:
        shape = function_signature_shape(WIRING, "settle_denied_run")
        assert "terminal_state" not in shape["kwonly"]


class TestLDW6PrimeObservationProvenance:
    def test_convergence_facts_reference_their_observation_sources(self) -> None:
        keywords = _settlement_keywords(WIRING)
        assert keywords["no_pending_approval"] == "gate_outcome.is_pending is False"
        assert keywords["no_pending_recovery"] == "manager.routing_run.branches == ()"

    def test_no_hardcoded_true_in_close_run(self) -> None:
        keywords = _settlement_keywords(WIRING)
        assert keywords["no_pending_approval"] != "True"
        assert keywords["no_pending_recovery"] != "True"

    def test_probe_hardcoded_true_goes_red(self) -> None:
        mutated = WIRING.replace(
            "no_pending_approval=(gate_outcome.is_pending is False)",
            "no_pending_approval=True",
            1,
        )
        assert mutated != WIRING
        assert _settlement_keywords(mutated)["no_pending_approval"] == "True"

    def test_probe_callerside_parameter_goes_red(self) -> None:
        mutated = WIRING.replace(
            "    ended_at: str,\n) -> DenySettlement:",
            "    ended_at: str,\n    no_pending_recovery: bool,\n) -> DenySettlement:",
            1,
        )
        assert mutated != WIRING
        assert "no_pending_recovery" in function_signature_shape(mutated, "settle_denied_run")["kwonly"]


class TestLDW7ZeroOutboundEdges:
    def test_no_motion_identifier_is_bound(self) -> None:
        assert referenced_names(WIRING) & FORBIDDEN_MOTION_NAMES == frozenset()

    def test_no_cross_layer_import(self) -> None:
        assert imported_module_tokens(WIRING) & {
            "resume_wiring",
            "resume_intent",
            "approval_answer",
            "session",
            "entrypoint",
            "execution",
            "router",
            "scheduler",
            "validation",
            "validation_wiring",
            "adapters",
        } == set()

    def test_the_only_authorized_sibling_dependency_is_terminal_wiring(self) -> None:
        """`§168` 批准：`terminal_wiring` 是**唯一**新增依赖（合法终态接线入口）。"""
        sibling_entries = {
            "recovery_wiring",
            "recovery_route_wiring",
            "recovery_regate_wiring",
            "recovery_modify_wiring",
            "reentry_request",
            "validation_wiring",
            "cost.ledger_wiring",
            "ledger_wiring",
        }
        assert imported_module_tokens(WIRING) & sibling_entries == set()
        assert "terminal_wiring" in imported_module_tokens(WIRING)
        assert imported_module_tokens(WIRING) == EXPECTED_IMPORT_TOKENS

    def test_probe_next_node_binding_goes_red(self) -> None:
        mutated = WIRING.replace(
            "    return DenySettlement(",
            "    next_node = 'router'\n    return DenySettlement(",
            1,
        )
        assert mutated != WIRING
        assert referenced_names(mutated) & FORBIDDEN_MOTION_NAMES == {"next_node"}


class TestLDW8ImportWhitelist:
    def test_import_tokens_are_exactly_the_whitelist(self) -> None:
        assert imported_module_tokens(WIRING) == EXPECTED_IMPORT_TOKENS

    def test_no_clock_module_imported(self) -> None:
        assert imported_module_tokens(WIRING) & {"datetime", "time", "zoneinfo"} == set()


class TestLDW9NoClock:
    def test_zero_clock_calls(self) -> None:
        assert clock_call_hits(WIRING) == []

    def test_ended_at_is_caller_supplied(self) -> None:
        assert "ended_at" in function_signature_shape(WIRING, "settle_denied_run")["kwonly"]

    def test_probe_clock_read_goes_red(self) -> None:
        mutated = WIRING.replace(
            "    converge_run(",
            "    ended_at = __import__('time').time()\n    converge_run(",
            1,
        )
        assert mutated != WIRING
        assert clock_call_hits(mutated) != []


class TestLDW10NoSwallow:
    def test_zero_try_nodes(self) -> None:
        assert _try_count(WIRING) == 0

    def test_error_type(self) -> None:
        assert issubclass(DenyWiringError, RuntimeError)
        assert not issubclass(DenyWiringError, ContractError)

    def test_probe_try_except_goes_red(self) -> None:
        mutated = WIRING.replace(
            "    converge_run(",
            "    try:\n        pass\n    except Exception:\n        pass\n    converge_run(",
            1,
        )
        assert mutated != WIRING
        assert _try_count(mutated) == 1


class TestLDW11NoContractConstruction:
    def test_no_contract_object_is_constructed(self) -> None:
        assert calls_to(WIRING, FORBIDDEN_CONSTRUCTIONS) == []

    def test_gate_side_is_read_through_the_settled_carrier(self) -> None:
        assert "from .approval_gate_wiring import GateDecisionKind, GateOutcome" in WIRING

    def test_probe_gate_outcome_construction_goes_red(self) -> None:
        mutated = WIRING.replace(
            "    return DenySettlement(",
            "    probe = GateOutcome(needs_approval=True)\n    return DenySettlement(",
            1,
        )
        assert mutated != WIRING
        assert calls_to(mutated, {"GateOutcome"}) != []


class TestLDW12ArchitecturalTimeOrderGuard:
    def test_guard_shape_is_the_frozen_one(self) -> None:
        assert "if manager.routing_run.branches != ():" in WIRING
        assert "manager.routing_run.branches == ()" in WIRING

    def test_branch_count_is_the_constant_zero(self) -> None:
        shapes = function_return_shapes(WIRING, "settle_denied_run")[0]
        assert "branch_count=0" in shapes

    def test_probe_dropped_guard_goes_red(self) -> None:
        mutated = WIRING.replace(
            "    if manager.routing_run.branches != ():",
            "    if False:",
            1,
        )
        assert mutated != WIRING
        assert "if manager.routing_run.branches != ():" not in mutated


# ======================================================================
# 行为 B1 – B7（逐条守卫的**独立可达**断言）
# ======================================================================


class TestBehaviorDenySettlement:
    def test_B1_deny_settles_the_run(self) -> None:
        manager = _manager()
        assert manager.routing_run.branches == ()
        settlement = settle_denied_run(
            gate_outcome=_outcome(GateDecisionKind.DENY), manager=manager, ended_at="T1"
        )
        assert settlement == DenySettlement(
            run_id=RUN_ID,
            terminal_state=TerminalState.APPROVAL_DENIED,
            gate_settled=True,
            ended_at="T1",
            branch_count=0,
        )
        assert manager.routing_run.terminal.state is TerminalState.APPROVAL_DENIED
        assert manager.routing_run.control_flow_converged is True
        assert manager.routing_run.branches == ()

    def test_B2_pending_outcome_is_rejected(self) -> None:
        manager = _manager()
        with pytest.raises(DenyWiringError):
            settle_denied_run(gate_outcome=_pending(), manager=manager, ended_at="T2")
        assert manager.routing_run.terminal is None

    def test_B3_approve_is_rejected_not_fallback(self) -> None:
        manager = _manager()
        with pytest.raises(DenyWiringError):
            settle_denied_run(
                gate_outcome=_outcome(GateDecisionKind.APPROVE), manager=manager, ended_at="T3"
            )
        assert manager.routing_run.terminal is None

    def test_B4_modify_is_rejected_not_fallback(self) -> None:
        manager = _manager()
        with pytest.raises(DenyWiringError):
            settle_denied_run(
                gate_outcome=_outcome(GateDecisionKind.MODIFY), manager=manager, ended_at="T4"
            )
        assert manager.routing_run.terminal is None

    def test_B5_live_branch_is_rejected_not_silently_settled(self) -> None:
        manager = _manager(with_branch=True)
        assert len(manager.routing_run.branches) == 1
        with pytest.raises(DenyWiringError):
            settle_denied_run(
                gate_outcome=_outcome(GateDecisionKind.DENY), manager=manager, ended_at="T5"
            )
        assert manager.routing_run.terminal is None
        assert manager.routing_run.control_flow_converged is False

    def test_B6_second_settlement_is_rejected_by_the_manager(self) -> None:
        manager = _manager()
        settle_denied_run(
            gate_outcome=_outcome(GateDecisionKind.DENY), manager=manager, ended_at="T6"
        )
        with pytest.raises(AgentManagerError):
            settle_denied_run(
                gate_outcome=_outcome(GateDecisionKind.DENY), manager=manager, ended_at="T6b"
            )

    def test_B7_bad_ended_at_is_rejected(self) -> None:
        for stamp in ("", "   ", 7, None):
            manager = _manager()
            with pytest.raises(DenyWiringError):
                settle_denied_run(
                    gate_outcome=_outcome(GateDecisionKind.DENY),
                    manager=manager,
                    ended_at=stamp,
                )
            assert manager.routing_run.terminal is None

    def test_type_guards(self) -> None:
        with pytest.raises(DenyWiringError):
            settle_denied_run(gate_outcome=object(), manager=_manager(), ended_at="T8")
        with pytest.raises(DenyWiringError):
            settle_denied_run(
                gate_outcome=_outcome(GateDecisionKind.DENY), manager=object(), ended_at="T8"
            )


class TestBehaviorNoBranchWrite:
    def test_run_settlement_does_not_write_branch_state(self) -> None:
        manager = _manager()
        settle_denied_run(
            gate_outcome=_outcome(GateDecisionKind.DENY), manager=manager, ended_at="T9"
        )
        assert manager.routing_run.branches == ()
        assert manager.active_subagent_count == 0

    def test_run_level_attempt_projection_is_untouched(self) -> None:
        manager = _manager()
        settle_denied_run(
            gate_outcome=_outcome(GateDecisionKind.DENY), manager=manager, ended_at="T10"
        )
        assert manager.routing_run.attempt == 1
        assert manager.routing_run.total_attempt_limit == 2


class TestProbeHygiene:
    def test_every_declared_anchor_hits(self) -> None:
        anchors = [
            "def settle_denied_run(",
            "    return DenySettlement(",
            "terminal_state=TerminalState.APPROVAL_DENIED",
            "no_pending_approval=(gate_outcome.is_pending is False)",
            "no_pending_recovery=(manager.routing_run.branches == ())",
            "    if manager.routing_run.branches != ():",
        ]
        for anchor in anchors:
            assert anchor in WIRING, f"锚点未命中：{anchor!r}"

    def test_source_is_parseable_without_bom(self) -> None:
        assert WIRING_PATH.read_bytes()[:3] != b"\xef\xbb\xbf"
        ast.parse(WIRING)

    def test_dto_fields_are_exactly_the_frozen_five(self) -> None:
        assert [f.name for f in dataclasses.fields(DenySettlement)] == EXPECTED_FIELDS
        assert DenySettlement.__dataclass_params__.frozen is True

    def test_probe_sixth_field_goes_red(self) -> None:
        names = [f.name for f in dataclasses.fields(DenySettlement)]
        assert names + ["next_node"] != EXPECTED_FIELDS
        assert len(names + ["next_node"]) == 6

    def test_dto_has_no_motion_field(self) -> None:
        assert {f.name for f in dataclasses.fields(DenySettlement)} & FORBIDDEN_MOTION_NAMES == set()

    def test_receipt_fields_are_not_business_fields(self) -> None:
        """`gate_settled` / `branch_count` 是**验证回执**（`§165` C 裁定）—— 必须存在于 DTO 且值恒定。"""
        manager = _manager()
        settlement = settle_denied_run(
            gate_outcome=_outcome(GateDecisionKind.DENY), manager=manager, ended_at="T11"
        )
        assert settlement.gate_settled is True
        assert settlement.branch_count == 0
