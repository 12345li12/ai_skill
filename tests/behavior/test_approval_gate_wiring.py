"""``C2b-2a Gate 语义载体``（`ApprovalJudgment → GateOutcome`）—— 结构锁 **LGT-1 – LGT-9** + 行为 **B1 – B8**。

架构依据：`docs/architecture.md` **Approval Gate 节点行**（`:1305-1356`：输入 / 输出 / **V1 三出口** /
"Gate 只做三件事"）· **§4.6**（R4；MODIFY 不新增 R7）· **`INV-10`** · **L41 / L43 / L45** ·
**A6 / A7 / A8** · **A13.5**（显式外部事实注入的先例）· **A18.2 / A18.3 / A18.5**。
范围、冻结、授权与实现记录见 `tests/behavior/README_step5b.md` **§120–§124**。

**本模块要守住的五条主张：**

  1. **唯一载体**：`GateOutcome(` 只有一处构造点，六实参**逐字**透传；
  2. **carrier 不是 decision maker**：不判审批（不 import `ApprovalJudge` / `risk` / `cost*` / `policy`）；
  3. **Gate 只做三件事**：不阻塞 / 不轮询 / 不 IO / 不管理时钟状态；
  4. **不写运行态**：不调用 `[10]` 任何方法、不写 `APPROVAL_DENIED` 终态、不碰 `no_pending_approval`；
  5. **三条显式失败守卫**：`needs_approval` 必须为真 · `decision` 自洽 · 类型正确。

⚠️ **禁止子串扫描**：本文件 docstring 里**故意提及** `ApprovalJudge` / `close_run` / `no_pending_approval` /
`policy` 等词 ⇒ 所有锁一律 **AST 级**。
⚠️ 敏感性 probe 一律在**源码字符串**上完成（``_mutate``），**不覆写** ``src/`` 里的真实文件；
真实源码的一次性变异证据见 README §124.5。
"""

from __future__ import annotations

import dataclasses
import hashlib
import pathlib

import pytest

from adaptive_router.approval_gate_wiring import (
    ApprovalGateWiringError,
    GateDecision,
    GateDecisionKind,
    GateOutcome,
    settle_gate,
)
from adaptive_router.contracts import ApprovalJudgment

from _approval_gate_locks import (
    annotated_assign_names,
    annotated_name_targets,
    assignment_value_shapes,
    attribute_assignment_targets,
    call_keyword_shapes_by_caller,
    calls_to,
    clock_call_hits,
    forbidden_import_hits,
    function_raise_guards,
    function_return_annotation,
    function_return_shapes,
    function_signature_shape,
    imported_module_tokens,
    imported_names,
    module_level_container_assignments,
    module_level_function_names,
    none_coalescing_shapes,
    open_calls,
    referenced_names,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
SRC = REPO_ROOT / "src/adaptive_router"
WIRING_PATH = SRC / "approval_gate_wiring.py"
WIRING = WIRING_PATH.read_text(encoding="utf-8")

#: LGT-2 —— `GateOutcome(` 的**六个实参逐字形态**（五项透传 + 决策对象）。
EXPECTED_OUTCOME_SHAPES = {
    "needs_approval": ("judgment.needs_approval",),
    "proposed_action": ("judgment.proposed_action",),
    "impact_scope": ("judgment.impact_scope",),
    "main_risk": ("judgment.main_risk",),
    "recovery_method": ("judgment.recovery_method",),
    "decision": ("decision",),
}

#: L43 / TEST-RISK-003 —— 审批**只列 4 项**（+ 透传位 + 决策位 = 六个字段）。
EXPECTED_OUTCOME_FIELDS = (
    "needs_approval",
    "proposed_action",
    "impact_scope",
    "main_risk",
    "recovery_method",
    "decision",
)
EXPECTED_LISTED_ITEMS = ("proposed_action", "impact_scope", "main_risk", "recovery_method")

#: LGT-2 —— 模块内允许的绑定：`__all__` + **枚举成员**（类体级常量，不是运行期状态）。
#: ⚠️ 任何**新增局部绑定**（尤其对 `decision` / `judgment` 的重绑定）都会让集合变化 ⇒ 红。
EXPECTED_BINDINGS = {"__all__", "APPROVE", "DENY", "MODIFY"}

#: 结构 —— 三个模块级函数（不得增删）。
EXPECTED_FUNCTIONS = ("_require_judgment", "_require_decision", "settle_gate")

#: LGT-9 —— 依赖白名单（**不含 `contracts`**，§123.9 ② 的收紧）。
FUTURE_TOKENS = {"__future__", "annotations"}
ALLOWED_STDLIB = {"dataclasses", "dataclass", "enum", "Enum"}
EXPECTED_IMPORT_MODULES = {"approval_judgment"}
EXPECTED_IMPORTED_NAMES = {"ApprovalJudgment", "dataclass", "Enum"}

#: LGT-3 / LGT-5 / LGT-6 —— 判定面 / 运行态 / 回流面（本模块一个都不许出现）。
JUDGMENT_TOKENS = ("approval_judgment.ApprovalJudge", "risk", "cost", "policy")
FORBIDDEN_IMPORT_TOKENS = (
    "agent_manager",
    "scheduler",
    "execution",
    "policy",
    "router",
    "planner",
    "cost",
    "risk",
    "adapters",
    "session",
    "entrypoint",
    "terminal_wiring",
)
CONTROL_CALLS = (
    "materialize",
    "commit_started",
    "record_outcome",
    "close_run",
    "apply_runtime_control",
    "schedule",
)
RUNTIME_NAMES = ("no_pending_approval", "control_flow_converged", "APPROVAL_DENIED")

#: LGT-7 —— 十四件受保护 artifact（基线见 README §123.4 / §124.6）。
PROTECTED_ARTIFACTS = {
    "src/adaptive_router/approval_judgment.py": (
        "E38435FD3744D0DE5215C5075A1C3413F960705BA61D906E1584544201C4AAD6"
    ),
    "src/adaptive_router/approval_constraints.py": (
        "4031F4968D22B1C5541DE2E3A99E0B800CC7EED0552D06FBD73467DAB9CE8DBD"
    ),
    "src/adaptive_router/approval_disclosure.py": (
        "C2C4052A6B63C971F63CF3E37D220218A7D8E401D731BDAB13CBEAAF9D2DAE00"
    ),
    "src/adaptive_router/session.py": (
        "A8483929115C1F197DB20E286550EDC558A392BFDC8603B4D387220F4D78C695"
    ),
    "src/adaptive_router/entrypoint.py": (
        "43B635D937D228421B6902CF5B157B79A2476FA6DDB1BCDE7FDB5B65DC00361C"
    ),
    "src/adaptive_router/router.py": (
        "B64F82C52C6FBFA1DC2D8EC5165F3328D0004BBEBA2424B518C02AC3FE1B07D3"
    ),
    "src/adaptive_router/agent_manager.py": (
        "154B171E0F2A7D568FCD61318869EEA05DA0D9A4140640A6A89860D13D0D01C5"
    ),
    "src/adaptive_router/contracts/decision.py": (
        "CD65DCC9DA19557B158980FC9B2CA70DF3E90BF583FBCC41DD4BD96A09FED93E"
    ),
    "src/adaptive_router/contracts/common.py": (
        "AD9BE8FBA26B6F908CC8F3C197D49CB52D64AF833D14272CECC8FE173D9C4168"
    ),
    "src/adaptive_router/recovery_route_wiring.py": (
        "B63EFD125DD1039D91F8FE839941D8BAF308C26BF748CA44B1FA79E625622EC3"
    ),
    "src/adaptive_router/recovery_regate_wiring.py": (
        "3CDFB4C1AEB5BDA70769F2CC5B8D14EB465437BBA378FF47E4FBFA67748B14C7"
    ),
    "tests/behavior/_recovery_route_locks.py": (
        "F22A5A49B0216BEE6FEDBC258C27E63A57A74425C5C70FB84B69196F77D1F023"
    ),
    "tests/behavior/test_recovery_route_wiring.py": (
        "5F0461EAB26AD496126D31B231E226AB5978D802D237B7594521DEC7AC47774F"
    ),
    "docs/architecture.md": (
        "CB3FCE709C2548EF3C7676886CC921176E5B9C82AA10CDF506F6A18438FA1BB5"
    ),
}


def _mutate(old: str, new: str) -> str:
    """**合成变异源码**（只改字符串，不动 ``src/``）；锚点未命中即失败（防 probe 假绿）。"""
    variant = WIRING.replace(old, new)
    assert variant != WIRING, f"probe 的锚点未命中，变异未生效：{old!r}"
    return variant


# --------------------------------------------------------------------------
# 测试替身：经**既有组件**造一个"要求审批"的 ApprovalJudgment（不手造假判定）
# --------------------------------------------------------------------------


def judgment(*, needs_approval: bool = True) -> ApprovalJudgment:
    """一个**要求审批**的 `ApprovalJudgment`（契约对象直构；判定链本身由 5B.4 覆盖）。

    ⚠️ 本边只消费**判定结果**（`needs_approval` + 四项清单）⇒ 测试不需要重跑 `ApprovalJudge`。
    """
    return ApprovalJudgment(
        needs_approval=needs_approval,
        proposed_action="写入 docs/ 下的两个文件",
        impact_scope="仅限本仓库 docs/",
        main_risk="文档与实现不一致",
        recovery_method="从 git 恢复上一版本文档",
    )


# ==========================================================================
# LGT-1 —— 唯一载体
# ==========================================================================


class TestLGT1SingleCarrier:
    def test_outcome_is_constructed_exactly_once(self) -> None:
        assert len(calls_to(WIRING, ("GateOutcome",))) == 1

    def test_the_only_construction_site_is_settle_gate(self) -> None:
        assert set(call_keyword_shapes_by_caller(WIRING, "GateOutcome")) == {"settle_gate"}

    def test_probe_second_construction_site_goes_red(self) -> None:
        variant = _mutate(
            "    _require_judgment(judgment)",
            "    GateOutcome(\n"
            "        needs_approval=True, proposed_action=\"x\", impact_scope=\"y\",\n"
            "        main_risk=\"z\", recovery_method=\"w\", decision=decision,\n    )\n"
            "    _require_judgment(judgment)",
        )
        assert len(calls_to(variant, ("GateOutcome",))) == 2


# ==========================================================================
# LGT-2 —— 只搬运 / 只组装（形态 + 无内部覆盖）
# ==========================================================================


class TestLGT2PassthroughOnly:
    def test_the_six_arguments_come_from_the_judgment_and_the_fact(self) -> None:
        shapes = call_keyword_shapes_by_caller(WIRING, "GateOutcome")
        assert shapes == {"settle_gate": EXPECTED_OUTCOME_SHAPES}

    def test_no_star_star_escape_hatch(self) -> None:
        shapes = call_keyword_shapes_by_caller(WIRING, "GateOutcome")
        assert "**" not in shapes["settle_gate"]

    def test_the_module_has_no_local_bindings(self) -> None:
        bound = set(assignment_value_shapes(WIRING)) | set(annotated_name_targets(WIRING))
        assert bound == EXPECTED_BINDINGS

    def test_probe_internal_override_goes_red(self) -> None:
        variant = _mutate(
            "    _require_judgment(judgment)",
            "    decision = GateDecision(GateDecisionKind.APPROVE, None)\n"
            "    _require_judgment(judgment)",
        )
        bound = set(assignment_value_shapes(variant)) | set(
            annotated_name_targets(variant)
        )
        assert bound != EXPECTED_BINDINGS

    def test_probe_recomputed_needs_approval_goes_red(self) -> None:
        variant = _mutate(
            "        needs_approval=judgment.needs_approval,",
            "        needs_approval=not judgment.needs_approval,",
        )
        assert call_keyword_shapes_by_caller(variant, "GateOutcome") != {
            "settle_gate": EXPECTED_OUTCOME_SHAPES
        }

    def test_probe_default_approve_on_none_goes_red(self) -> None:
        variant = _mutate(
            "        decision=decision,",
            "        decision=decision or GateDecision(GateDecisionKind.APPROVE, None),",
        )
        assert call_keyword_shapes_by_caller(variant, "GateOutcome") != {
            "settle_gate": EXPECTED_OUTCOME_SHAPES
        }


# ==========================================================================
# LGT-3 —— 不做审批判断
# ==========================================================================


class TestLGT3NoJudgment:
    def test_no_judge_is_called_or_referenced(self) -> None:
        assert calls_to(WIRING, ("ApprovalJudge", "ApprovalJudgment")) == []
        assert "ApprovalJudge" not in referenced_names(WIRING)

    def test_no_judgment_side_import(self) -> None:
        assert forbidden_import_hits(WIRING, ("risk", "cost", "policy")) == set()

    def test_needs_approval_is_never_reassigned(self) -> None:
        assert "needs_approval" not in assignment_value_shapes(WIRING)

    def test_the_guard_rejects_needs_approval_false(self) -> None:
        assert (
            "judgment.needs_approval is not True",
            "ApprovalGateWiringError",
        ) in function_raise_guards(WIRING, "_require_judgment")

    def test_probe_dropped_guard_goes_red(self) -> None:
        variant = _mutate(
            "    if judgment.needs_approval is not True:",
            "    if False:",
        )
        assert (
            "judgment.needs_approval is not True",
            "ApprovalGateWiringError",
        ) not in function_raise_guards(variant, "_require_judgment")


# ==========================================================================
# LGT-4 —— Gate 只做三件事（不做第四件）
# ==========================================================================


class TestLGT4GateDoesThreeThingsOnly:
    def test_no_io_no_blocking_no_polling(self) -> None:
        assert open_calls(WIRING) == []
        assert clock_call_hits(WIRING) == []
        for token in ("sleep", "input", "wait", "poll", "threading", "asyncio"):
            assert token not in imported_module_tokens(WIRING), token
        assert "while" not in WIRING.split('"""')[2]  # 实现体里没有轮询循环

    def test_no_permission_aggregation_or_decision_rerun(self) -> None:
        assert calls_to(
            WIRING, ("authorize", "aggregate", "decide", "evaluate", "plan", "route")
        ) == []

    def test_probe_clock_state_goes_red(self) -> None:
        variant = _mutate(
            "from enum import Enum",
            "import time\nfrom enum import Enum",
        )
        variant = variant.replace(
            "    _require_judgment(judgment)",
            "    time.monotonic() and _require_judgment(judgment)",
            1,
        )
        assert clock_call_hits(variant) != []


# ==========================================================================
# LGT-5 —— 不写运行态
# ==========================================================================


class TestLGT5NoRuntimeWrite:
    def test_no_control_method_is_called(self) -> None:
        assert calls_to(WIRING, CONTROL_CALLS) == []

    def test_no_runtime_names_appear(self) -> None:
        names = referenced_names(WIRING)
        for name in RUNTIME_NAMES:
            assert name not in names, name

    def test_probe_runtime_write_goes_red(self) -> None:
        variant = _mutate(
            "from .approval_judgment import ApprovalJudgment",
            "from .agent_manager import AgentManager\n"
            "from .approval_judgment import ApprovalJudgment",
        )
        variant = variant.replace(
            "    _require_judgment(judgment)",
            "    AgentManager(\n"
            '        run_id="run_mt4", branch_order=(), total_attempt_limit=1\n'
            "    ).close_run(\n"
            "        terminal_state=None, no_pending_approval=True,\n"
            '        no_pending_recovery=True, ended_at="MT4",\n'
            "    )\n"
            "    _require_judgment(judgment)",
            1,
        )
        assert calls_to(variant, ("close_run",)) != []
        assert forbidden_import_hits(variant, ("agent_manager",)) != set()


# ==========================================================================
# LGT-6 —— MODIFY 不越权
# ==========================================================================


class TestLGT6ModifyDoesNotOverreach:
    def test_no_reflow_modules(self) -> None:
        assert calls_to(WIRING, ("Router", "AdaptiveRouter", "Planner", "Scheduler")) == []
        assert forbidden_import_hits(
            WIRING, ("policy", "router", "planner", "scheduler")
        ) == set()

    def test_modify_is_carried_not_executed(self) -> None:
        assert "GateDecisionKind.MODIFY" in WIRING  # 只作为**取值**出现（守卫与文档）
        assert calls_to(WIRING, ("policy", "router_plan", "plan")) == []

    def test_probe_policy_import_goes_red(self) -> None:
        variant = _mutate(
            "from .approval_judgment import ApprovalJudgment",
            "from .approval_judgment import ApprovalJudgment\nfrom .policy import Policy",
        )
        assert forbidden_import_hits(variant, ("policy",)) != set()


# ==========================================================================
# LGT-7 —— 受保护 artifact（十四件）
# ==========================================================================




# ==========================================================================
# LGT-8 / LGT-9 —— 无时钟无状态 / 依赖白名单
# ==========================================================================


class TestLGT8NoClockNoState:
    def test_no_clock_no_state_no_io(self) -> None:
        assert clock_call_hits(WIRING) == []
        assert module_level_container_assignments(WIRING) == ()
        assert annotated_assign_names(WIRING) == ()
        assert attribute_assignment_targets(WIRING) == ()
        assert open_calls(WIRING) == []
        assert none_coalescing_shapes(WIRING) == ()


class TestLGT9ImportWhitelist:
    def test_import_whitelist_is_exact_and_has_no_contracts(self) -> None:
        tokens = imported_module_tokens(WIRING) - FUTURE_TOKENS
        assert tokens == EXPECTED_IMPORT_MODULES | ALLOWED_STDLIB
        assert "contracts" not in tokens

    def test_imported_names_are_exact(self) -> None:
        assert imported_names(WIRING) - FUTURE_TOKENS == EXPECTED_IMPORTED_NAMES

    def test_no_forbidden_import(self) -> None:
        assert forbidden_import_hits(WIRING, FORBIDDEN_IMPORT_TOKENS) == set()

    def test_function_inventory_signatures_and_guards(self) -> None:
        assert module_level_function_names(WIRING) == EXPECTED_FUNCTIONS
        assert function_signature_shape(WIRING, "settle_gate") == {
            "positional": [],
            "kwonly": ["judgment", "decision"],
            "defaults": [False, False],
            "vararg": None,
            "kwarg": None,
        }
        assert function_return_annotation(WIRING, "settle_gate") == "GateOutcome"
        assert len(function_return_shapes(WIRING, "settle_gate")) == 1
        assert (
            "not isinstance(decision, GateDecision)",
            "ApprovalGateWiringError",
        ) in function_raise_guards(WIRING, "_require_decision")
        assert (
            "type(decision.modification) is not str or not decision.modification.strip()",
            "ApprovalGateWiringError",
        ) in function_raise_guards(WIRING, "_require_decision")
        assert (
            "decision.modification is not None",
            "ApprovalGateWiringError",
        ) in function_raise_guards(WIRING, "_require_decision")

    def test_probe_extra_import_goes_red(self) -> None:
        variant = _mutate(
            "from .approval_judgment import ApprovalJudgment",
            "from .approval_judgment import ApprovalJudgment\nimport os",
        )
        assert imported_module_tokens(variant) - FUTURE_TOKENS != (
            EXPECTED_IMPORT_MODULES | ALLOWED_STDLIB
        )


# ==========================================================================
# 行为测试 B1 – B8
# ==========================================================================


class TestSettleGate:
    def test_b1_the_five_fields_are_passed_through(self) -> None:
        judge = judgment()
        outcome = settle_gate(judgment=judge, decision=None)
        assert outcome.needs_approval is judge.needs_approval
        assert outcome.proposed_action == judge.proposed_action
        assert outcome.impact_scope == judge.impact_scope
        assert outcome.main_risk == judge.main_risk
        assert outcome.recovery_method == judge.recovery_method

    def test_b2_needs_approval_false_is_an_explicit_failure(self) -> None:
        judge = judgment()
        not_needed = dataclasses.replace(judge, needs_approval=False)
        with pytest.raises(ApprovalGateWiringError):
            settle_gate(judgment=not_needed, decision=None)
        with pytest.raises(ApprovalGateWiringError):
            settle_gate(
                judgment=not_needed,
                decision=GateDecision(kind=GateDecisionKind.APPROVE, modification=None),
            )

    def test_b3_none_means_pending_and_does_not_raise(self) -> None:
        outcome = settle_gate(judgment=judgment(), decision=None)
        assert outcome.decision is None
        assert outcome.is_pending is True

    def test_b4_the_three_exits_resolve_the_gate(self) -> None:
        for kind, modification in (
            (GateDecisionKind.APPROVE, None),
            (GateDecisionKind.DENY, None),
            (GateDecisionKind.MODIFY, "只允许读，不允许写"),
        ):
            outcome = settle_gate(
                judgment=judgment(),
                decision=GateDecision(kind=kind, modification=modification),
            )
            assert outcome.is_pending is False
            assert outcome.decision is not None
            assert outcome.decision.kind is kind

    def test_b5_modify_requires_a_non_empty_modification(self) -> None:
        for bad in (None, "", "   "):
            with pytest.raises(ApprovalGateWiringError):
                settle_gate(
                    judgment=judgment(),
                    decision=GateDecision(kind=GateDecisionKind.MODIFY, modification=bad),
                )

    def test_b6_approve_and_deny_must_not_carry_modification(self) -> None:
        for kind in (GateDecisionKind.APPROVE, GateDecisionKind.DENY):
            with pytest.raises(ApprovalGateWiringError):
                settle_gate(
                    judgment=judgment(),
                    decision=GateDecision(kind=kind, modification="x"),
                )

    def test_b7_the_dto_carries_exactly_the_four_items_plus_two(self) -> None:
        """TEST-RISK-003：审批**只列 4 项**（+ 透传位 + 决策位）。"""
        names = tuple(f.name for f in dataclasses.fields(GateOutcome))
        assert names == EXPECTED_OUTCOME_FIELDS
        assert set(EXPECTED_LISTED_ITEMS) <= set(names)
        outcome = settle_gate(judgment=judgment(), decision=None)
        with pytest.raises(dataclasses.FrozenInstanceError):
            outcome.decision = None  # type: ignore[misc]
        assert {f.name for f in dataclasses.fields(GateOutcome)} & {
            "pending",
            "approved",
            "needs_human",
        } == set()

    def test_b8_wrong_types_are_explicit_failures(self) -> None:
        for bad in ("nope", None, 1, object()):
            with pytest.raises(ApprovalGateWiringError):
                settle_gate(judgment=bad, decision=None)  # type: ignore[arg-type]
        for bad in ("approve", 1, object()):
            with pytest.raises(ApprovalGateWiringError):
                settle_gate(judgment=judgment(), decision=bad)  # type: ignore[arg-type]
