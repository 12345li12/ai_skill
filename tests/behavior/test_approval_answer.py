"""``C2b-2c-2 答案回收 seam`` —— 结构锁 **LGA-1 – LGA-9** + 行为 **B1 – B7**。

架构依据：`docs/architecture.md` **Approval Gate 节点行**（五步图第 4 步"产生新的授权事实" ·
"Gate 只做三件事" · V1 三出口）· **§4.4**（回流必须重入授权判定链）· **`INV-10` / `INV-16`** ·
**A18.2 / A18.3**（`no_pending_approval` 的来源）· **C2b-2a**（`settle_gate` 唯一语义入口）·
**C2b-2b-1 F1**（文本解释权在边界外）。
范围、冻结、授权与实现记录见 `tests/behavior/README_step5b.md` **§144–§147**。

**本模块要守住的六条主张：**

  1. **`settle_gate` 是唯一语义入口**：本边不构造 `GateOutcome`、不复制 Gate 结算逻辑；
  2. **卡片不变**：`GateOutcome` 六字段与 `settle_gate` 签名逐字节不变；
  3. **零取值**：不读 `decision.modification`（人类答案文本不进入本边）；
  4. **只对挂起卡片回收**：已解析 ⇒ 显式失败（防"第二份授权事实"）；
  5. **不写运行态**：不调用 `[10]` 任何方法、不引用 `RoutingRun` / attempt / 终态；
  6. **不接另一边界**：不 import `session` / `entrypoint` / `approval_question`。

⚠️ **禁止子串扫描**：本文件 docstring 里**故意提及** `modification` / `close_run` / `ApprovalQuestion`
等词 ⇒ 所有锁一律 **AST 级**。
⚠️ 敏感性 probe 一律在**源码字符串**上完成（``_mutate``），**不覆写** ``src/`` 里的真实文件；
真实源码的一次性变异证据见 README §147.5。
"""

from __future__ import annotations

import ast
import dataclasses
import hashlib
import pathlib

import pytest

from adaptive_router.approval_answer import (
    ApprovalAnswerError,
    recover_gate_answer,
)
from adaptive_router.approval_gate_wiring import (
    ApprovalGateWiringError,
    GateDecision,
    GateDecisionKind,
    GateOutcome,
)
from adaptive_router.approval_judgment import ApprovalJudgment

from _approval_answer_locks import (
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
WIRING_PATH = SRC / "approval_answer.py"
WIRING = WIRING_PATH.read_text(encoding="utf-8")
GATE_PATH = SRC / "approval_gate_wiring.py"
GATE = GATE_PATH.read_text(encoding="utf-8")

#: LGA-1 —— `GateOutcome` 的六个字段（**只读**断言既有源码）。
EXPECTED_CARD_FIELDS = [
    "needs_approval",
    "proposed_action",
    "impact_scope",
    "main_risk",
    "recovery_method",
    "decision",
]

#: LGA-9 —— 模块级函数清单（恰好三个，不得增删）。
EXPECTED_FUNCTIONS = (
    "_require_judgment",
    "_require_pending_outcome",
    "recover_gate_answer",
)

#: LGA-2 —— `settle_gate` 的**唯一** caller 与它的实参形态。
EXPECTED_SETTLE_SHAPES = {
    "recover_gate_answer": {
        "judgment": ("judgment",),
        "decision": ("decision",),
    }
}

#: LGA-9 —— 依赖白名单（精确集合）。
FUTURE_TOKENS = {"__future__", "annotations"}
EXPECTED_IMPORT_MODULES = {"approval_gate_wiring", "approval_judgment"}
EXPECTED_IMPORTED_NAMES = {"GateDecision", "GateOutcome", "settle_gate", "ApprovalJudgment"}

#: LGA-3 / LGA-5 / LGA-6 —— 本边一个都不许出现。
STRING_METHODS = ("strip", "split", "lower", "upper", "startswith", "endswith", "find", "match", "search")
FORBIDDEN_IMPORT_TOKENS = (
    "session",
    "entrypoint",
    "approval_question",
    "router",
    "policy",
    "agent_manager",
    "scheduler",
    "execution",
    "terminal_wiring",
    "cost",
    "risk",
    "recovery",
    "adapters",
)
CONTROL_CALLS = (
    "materialize",
    "commit_started",
    "record_outcome",
    "close_run",
    "apply_runtime_control",
    "schedule",
)
RUNTIME_NAMES = (
    "RoutingRun",
    "BranchState",
    "no_pending_approval",
    "APPROVAL_DENIED",
    "attempt",
    "modification",
    "ApprovalQuestion",
    "SessionGate",
)

#: LGA-7 —— 受保护 artifact（**二十五件**；不含本文件自身）。
PROTECTED_ARTIFACTS = {
    "src/adaptive_router/approval_gate_wiring.py": (
        "CEB2015F825763DF934FCCBF42DF8D94031B592F9BB4712F03A69409FA04CDAF"
    ),
    "src/adaptive_router/approval_question.py": (
        "04B4274E79F4579DFD1A971CFDE4B0A132368042A7A604259E8ACEBBA7A433E0"
    ),
    "src/adaptive_router/recovery_modify_wiring.py": (
        "D56E81F45858161060941A51E10C0D828D6A8E59441E0025882037E3AEEECCEB"
    ),
    "src/adaptive_router/reentry_request.py": (
        "F836A15BEAA91859CF502B08B23CD115830D5BC1FB76ED8BF7CEA5150A798F14"
    ),
    "src/adaptive_router/recovery_regate_wiring.py": (
        "3CDFB4C1AEB5BDA70769F2CC5B8D14EB465437BBA378FF47E4FBFA67748B14C7"
    ),
    "src/adaptive_router/recovery_route_wiring.py": (
        "B63EFD125DD1039D91F8FE839941D8BAF308C26BF748CA44B1FA79E625622EC3"
    ),
    "src/adaptive_router/terminal_wiring.py": (
        "2B317FEABD72313465CDBE86659E58ED35CBDC24EE7CBD694AEAB357C92C0691"
    ),
    "src/adaptive_router/cost/ledger_wiring.py": (
        "6F0F3DBCA6970F8873115AB672434A49D6227F017A8BCD55F7B6075B431F0B7F"
    ),
    "src/adaptive_router/router.py": (
        "B64F82C52C6FBFA1DC2D8EC5165F3328D0004BBEBA2424B518C02AC3FE1B07D3"
    ),
    "src/adaptive_router/policy.py": (
        "14C5BEDD562E3F99C308F3CCFDA1FA717A08CB3FDA6970FF771B1F4C62ADFA08"
    ),
    "src/adaptive_router/agent_manager.py": (
        "154B171E0F2A7D568FCD61318869EEA05DA0D9A4140640A6A89860D13D0D01C5"
    ),
    "src/adaptive_router/approval_judgment.py": (
        "E38435FD3744D0DE5215C5075A1C3413F960705BA61D906E1584544201C4AAD6"
    ),
    "src/adaptive_router/approval_constraints.py": (
        "4031F4968D22B1C5541DE2E3A99E0B800CC7EED0552D06FBD73467DAB9CE8DBD"
    ),
    "src/adaptive_router/approval_disclosure.py": (
        "C2C4052A6B63C971F63CF3E37D220218A7D8E401D731BDAB13CBEAAF9D2DAE00"
    ),
    "src/adaptive_router/entrypoint.py": (
        "43B635D937D228421B6902CF5B157B79A2476FA6DDB1BCDE7FDB5B65DC00361C"
    ),
    "src/adaptive_router/session.py": (
        "A8483929115C1F197DB20E286550EDC558A392BFDC8603B4D387220F4D78C695"
    ),
    "src/adaptive_router/contracts/decision.py": (
        "CD65DCC9DA19557B158980FC9B2CA70DF3E90BF583FBCC41DD4BD96A09FED93E"
    ),
    "src/adaptive_router/contracts/common.py": (
        "AD9BE8FBA26B6F908CC8F3C197D49CB52D64AF833D14272CECC8FE173D9C4168"
    ),
    "tests/behavior/_approval_question_locks.py": (
        "C647659F7E831000B61356AB001C13E5EA3AA6EB8468EA85119613B42AEB5AB9"
    ),
    "tests/behavior/test_approval_question.py": (
        "533A1595D6D35C2ABE13AAAEDB2079CD0B440A24667F967F78FB3545F2CBB016"
    ),
    "tests/behavior/_recovery_modify_locks.py": (
        "E6A0495005CA86287435A15005044A4B0C3FDA6C9F6DF4B002105100461FBCF0"
    ),
    "tests/behavior/test_recovery_modify_wiring.py": (
        "FFA5278A4F7515749619ACF4D870E90FCC800690283C57B0BF7CE3EE6F9324EB"
    ),
    "tests/behavior/test_reentry_request.py": (
        "5C3751C53F5269971C15690CBC6B3A3CFEFDCE762F157C009AC1A3E7CBF702E9"
    ),
    "tests/behavior/test_router.py": (
        "27BFAA27D11D72C4579479E7AD764C317DFA260E6FC40F140D54F8EEF31C4F28"
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
# 测试替身：判据（`ApprovalJudgment`）+ 挂起 / 已解析卡片
# --------------------------------------------------------------------------


def judgment(*, needs_approval: bool = True) -> ApprovalJudgment:
    """判定链的产物（契约对象直构；判定链本身由 5B.4 覆盖）。"""
    return ApprovalJudgment(
        needs_approval=needs_approval,
        proposed_action="写入 docs/ 下的两个文件",
        impact_scope="仅限本仓库 docs/",
        main_risk="文档与实现不一致",
        recovery_method="从 git 恢复上一版本文档",
    )


def card(*, decision: GateDecision | None = None) -> GateOutcome:
    """由 `judgment()` 的四项 + 给定 decision 直构的卡片（**挂起**当 `decision is None`）。"""
    j = judgment()
    return GateOutcome(
        needs_approval=j.needs_approval,
        proposed_action=j.proposed_action,
        impact_scope=j.impact_scope,
        main_risk=j.main_risk,
        recovery_method=j.recovery_method,
        decision=decision,
    )


APPROVE = GateDecision(kind=GateDecisionKind.APPROVE, modification=None)
DENY = GateDecision(kind=GateDecisionKind.DENY, modification=None)
MODIFY = GateDecision(kind=GateDecisionKind.MODIFY, modification="只允许读，不允许写")


# ==========================================================================
# LGA-1 —— 卡片与卡片边不变
# ==========================================================================


class TestLGA1FrozenCard:
    def test_the_gate_outcome_card_is_unchanged(self) -> None:
        tree = ast.parse(GATE)
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == "GateOutcome":
                fields = [a.target.id for a in node.body if isinstance(a, ast.AnnAssign)]
                assert fields == EXPECTED_CARD_FIELDS
                return
        raise AssertionError("GateOutcome 未找到")

    def test_this_edge_never_constructs_the_card_or_the_decision(self) -> None:
        assert calls_to(WIRING, ("GateOutcome", "GateDecision", "GateDecisionKind")) == []

    def test_probe_seventh_field_goes_red(self) -> None:
        variant = GATE.replace(
            "    recovery_method: str\n    decision: GateDecision | None",
            "    recovery_method: str\n    decision: GateDecision | None\n    gate_id: str",
        )
        assert variant != GATE
        tree = ast.parse(variant)
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == "GateOutcome":
                fields = [a.target.id for a in node.body if isinstance(a, ast.AnnAssign)]
                assert "gate_id" in fields
                return
        raise AssertionError("变异未生效")


# ==========================================================================
# LGA-2 —— `settle_gate` 是唯一语义入口
# ==========================================================================


class TestLGA2SingleSemanticEntry:
    def test_settle_gate_is_called_exactly_once(self) -> None:
        assert len(calls_to(WIRING, ("settle_gate",))) == 1

    def test_the_only_caller_is_the_recovery_entry(self) -> None:
        shapes = call_keyword_shapes_by_caller(WIRING, "settle_gate")
        assert set(shapes) == {"recover_gate_answer"}
        assert shapes == EXPECTED_SETTLE_SHAPES

    def test_there_is_still_exactly_one_gate_outcome_construction_site_in_src(self) -> None:
        hits: list[str] = []
        for path in SRC.rglob("*.py"):
            if "GateOutcome(" in path.read_text(encoding="utf-8"):
                hits.append(path.name)
        assert hits == ["approval_gate_wiring.py"]

    def test_probe_second_settle_call_goes_red(self) -> None:
        variant = _mutate(
            "    return settle_gate(judgment=judgment, decision=decision)",
            "    settle_gate(judgment=judgment, decision=decision)\n"
            "    return settle_gate(judgment=judgment, decision=decision)",
        )
        assert len(calls_to(variant, ("settle_gate",))) == 2

    def test_probe_reimplemented_wrapping_goes_red(self) -> None:
        variant = _mutate(
            "    return settle_gate(judgment=judgment, decision=decision)",
            "    return GateOutcome(needs_approval=True, proposed_action='a', "
            "impact_scope='b', main_risk='c', recovery_method='d', decision=decision)",
        )
        assert calls_to(variant, ("GateOutcome",)) != []


# ==========================================================================
# LGA-3 —— 零取值（不解析人类答案）
# ==========================================================================


class TestLGA3ZeroRead:
    def test_the_answer_text_is_never_referenced(self) -> None:
        assert "modification" not in referenced_names(WIRING)

    def test_no_string_method_is_called(self) -> None:
        assert calls_to(WIRING, STRING_METHODS) == []

    def test_no_regex_module_is_imported(self) -> None:
        assert "re" not in imported_module_tokens(WIRING)

    def test_probe_reading_the_answer_goes_red(self) -> None:
        variant = _mutate(
            "    _require_judgment(judgment)",
            "    _read = decision.modification.strip()\n    _require_judgment(judgment)",
        )
        assert "modification" in referenced_names(variant)
        assert calls_to(variant, ("strip",)) != []


# ==========================================================================
# LGA-4 —— 只对挂起卡片回收
# ==========================================================================


class TestLGA4PendingOnly:
    def test_the_four_guards_are_present(self) -> None:
        assert (
            "not isinstance(judgment, ApprovalJudgment)",
            "ApprovalAnswerError",
        ) in function_raise_guards(WIRING, "_require_judgment")
        assert (
            "judgment.needs_approval is not True",
            "ApprovalAnswerError",
        ) in function_raise_guards(WIRING, "_require_judgment")
        assert (
            "not isinstance(answered, GateOutcome)",
            "ApprovalAnswerError",
        ) in function_raise_guards(WIRING, "_require_pending_outcome")
        assert (
            "answered.is_pending is not True",
            "ApprovalAnswerError",
        ) in function_raise_guards(WIRING, "_require_pending_outcome")

    def test_the_entry_calls_both_guards(self) -> None:
        fn = next(
            n
            for n in ast.parse(WIRING).body
            if isinstance(n, ast.FunctionDef) and n.name == "recover_gate_answer"
        )
        called = [
            c.func.id
            for c in ast.walk(fn)
            if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
        ]
        assert called == ["_require_judgment", "_require_pending_outcome", "settle_gate"]

    def test_probe_dropped_pending_guard_goes_red(self) -> None:
        variant = _mutate(
            "    if answered.is_pending is not True:",
            "    if False:",
        )
        assert (
            "answered.is_pending is not True",
            "ApprovalAnswerError",
        ) not in function_raise_guards(variant, "_require_pending_outcome")


# ==========================================================================
# LGA-5 —— 不写运行态
# ==========================================================================


class TestLGA5NoRuntimeWrite:
    def test_no_control_method_is_called(self) -> None:
        assert calls_to(WIRING, CONTROL_CALLS) == []

    def test_no_runtime_name_appears(self) -> None:
        names = referenced_names(WIRING)
        for name in RUNTIME_NAMES:
            assert name not in names, name

    def test_no_runtime_module_is_imported(self) -> None:
        assert forbidden_import_hits(WIRING, FORBIDDEN_IMPORT_TOKENS) == set()

    def test_probe_runtime_write_goes_red(self) -> None:
        variant = _mutate(
            "    _require_judgment(judgment)",
            "    from .agent_manager import AgentManager\n    _ = AgentManager\n"
            "    _require_judgment(judgment)",
        )
        assert forbidden_import_hits(variant, ("agent_manager",)) != set()


# ==========================================================================
# LGA-6 —— 不接另一边界 / 不接问题载体
# ==========================================================================


class TestLGA6NoOtherBoundary:
    def test_the_other_boundary_and_the_question_carrier_are_not_imported(self) -> None:
        assert forbidden_import_hits(WIRING, ("session", "entrypoint", "approval_question")) == set()

    def test_the_question_carrier_is_not_referenced(self) -> None:
        assert "ApprovalQuestion" not in referenced_names(WIRING)

    def test_probe_entrypoint_import_goes_red(self) -> None:
        variant = _mutate(
            "from .approval_judgment import ApprovalJudgment",
            "from .entrypoint import EntrypointHandlers\nfrom .approval_judgment import ApprovalJudgment",
        )
        assert forbidden_import_hits(variant, ("entrypoint",)) != set()


# ==========================================================================
# LGA-7 —— 受保护 artifact（二十五件）
# ==========================================================================




# ==========================================================================
# LGA-8 / LGA-9 —— 无时钟无状态 / 依赖白名单
# ==========================================================================


class TestLGA8NoClockNoState:
    def test_no_clock_no_state_no_io(self) -> None:
        assert clock_call_hits(WIRING) == []
        assert module_level_container_assignments(WIRING) == ()
        assert annotated_assign_names(WIRING) == ()
        assert attribute_assignment_targets(WIRING) == ()
        assert open_calls(WIRING) == []
        assert none_coalescing_shapes(WIRING) == ()

    def test_the_implementation_body_has_no_loop(self) -> None:
        body = WIRING.split('"""')[2]
        assert "while " not in body
        assert "for " not in body

    def test_the_module_has_no_local_bindings(self) -> None:
        bound = set(assignment_value_shapes(WIRING)) | set(annotated_name_targets(WIRING))
        assert bound == {"__all__"}


class TestLGA9WhitelistAndShape:
    def test_import_whitelist_is_exact(self) -> None:
        tokens = imported_module_tokens(WIRING) - FUTURE_TOKENS
        assert tokens == EXPECTED_IMPORT_MODULES
        assert imported_names(WIRING) - FUTURE_TOKENS == EXPECTED_IMPORTED_NAMES

    def test_function_inventory_signature_and_return(self) -> None:
        assert module_level_function_names(WIRING) == EXPECTED_FUNCTIONS
        assert function_signature_shape(WIRING, "recover_gate_answer") == {
            "positional": [],
            "kwonly": ["judgment", "decision", "answered"],
            "defaults": [False, False, False],
            "vararg": None,
            "kwarg": None,
        }
        assert function_return_annotation(WIRING, "recover_gate_answer") == "GateOutcome"
        assert len(function_return_shapes(WIRING, "recover_gate_answer")) == 1

    def test_probe_extra_import_goes_red(self) -> None:
        variant = _mutate(
            "from .approval_judgment import ApprovalJudgment",
            "import os\nfrom .approval_judgment import ApprovalJudgment",
        )
        assert imported_module_tokens(variant) - FUTURE_TOKENS != EXPECTED_IMPORT_MODULES


# ==========================================================================
# 行为测试 B1 – B7
# ==========================================================================


class TestRecoverGateAnswer:
    def test_b1_a_pending_card_becomes_resolved(self) -> None:
        j = judgment()
        out = recover_gate_answer(judgment=j, decision=APPROVE, answered=card())
        assert out.is_pending is False
        assert out.needs_approval is j.needs_approval
        assert out.proposed_action == j.proposed_action
        assert out.impact_scope == j.impact_scope
        assert out.main_risk == j.main_risk
        assert out.recovery_method == j.recovery_method

    def test_b2_an_already_resolved_card_is_an_explicit_failure(self) -> None:
        with pytest.raises(ApprovalAnswerError):
            recover_gate_answer(judgment=judgment(), decision=APPROVE, answered=card(decision=APPROVE))
        with pytest.raises(ApprovalAnswerError):
            recover_gate_answer(judgment=judgment(), decision=DENY, answered=card(decision=MODIFY))

    def test_b3_the_three_exits_resolve_the_card(self) -> None:
        for decision in (APPROVE, DENY, MODIFY):
            out = recover_gate_answer(judgment=judgment(), decision=decision, answered=card())
            assert out.decision is decision
            assert out.decision.kind is decision.kind

    def test_b4_settle_gate_guards_still_apply(self) -> None:
        bad_modify = GateDecision(kind=GateDecisionKind.MODIFY, modification=None)
        with pytest.raises(ApprovalGateWiringError):
            recover_gate_answer(judgment=judgment(), decision=bad_modify, answered=card())
        bad_approve = GateDecision(kind=GateDecisionKind.APPROVE, modification="x")
        with pytest.raises(ApprovalGateWiringError):
            recover_gate_answer(judgment=judgment(), decision=bad_approve, answered=card())

    def test_b5_wrong_inputs_are_explicit_failures(self) -> None:
        for bad in ("nope", None, 1, object()):
            with pytest.raises(ApprovalAnswerError):
                recover_gate_answer(judgment=bad, decision=APPROVE, answered=card())  # type: ignore[arg-type]
            with pytest.raises(ApprovalAnswerError):
                recover_gate_answer(judgment=judgment(), decision=APPROVE, answered=bad)  # type: ignore[arg-type]
        with pytest.raises(ApprovalAnswerError):
            recover_gate_answer(
                judgment=judgment(needs_approval=False), decision=APPROVE, answered=card()
            )

    def test_b6_the_answer_text_has_zero_effect_on_the_output(self) -> None:
        first = recover_gate_answer(judgment=judgment(), decision=APPROVE, answered=card())
        second = recover_gate_answer(judgment=judgment(), decision=DENY, answered=card())
        assert dataclasses.replace(first, decision=second.decision) == second

    def test_b7_the_return_value_is_settle_gates_own_result(self) -> None:
        from adaptive_router.approval_gate_wiring import settle_gate

        j = judgment()
        expected = settle_gate(judgment=j, decision=APPROVE)
        actual = recover_gate_answer(judgment=j, decision=APPROVE, answered=card())
        assert actual == expected
        assert type(actual) is type(expected)

    def test_the_wiring_error_is_a_runtime_error_not_a_gate_error(self) -> None:
        assert issubclass(ApprovalAnswerError, RuntimeError)
        assert not issubclass(ApprovalAnswerError, ApprovalGateWiringError)
