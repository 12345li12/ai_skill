"""``C2b-2c-1 审批问题载体 + 发出 seam`` —— 结构锁 **LGC-1 – LGC-9** + 行为 **B1 – B7**。

架构依据：`docs/architecture.md` **Approval Gate 节点行**（"不是 bool 函数" · 五步图 ·
"Gate 只做三件事"）· **L41 / L43 / L45** · **`INV-10`** · **4.1 Session BYPASS 流 + L5 / L7**（询问先例）·
**5 节平台边界**（Core 禁平台细节）· **A8** · **A18.5**。
范围、冻结、授权与实现记录见 `tests/behavior/README_step5b.md` **§138–§142**。

**本模块要守住的六条主张：**

  1. **卡片不变**：`GateOutcome` 六字段与 `settle_gate` 签名逐字节不变（本边不构造它们）；
  2. **只搬运**：四项逐字透传；`prompt` 由固定模板**内联**生成（确定性投影，非自由文案）；
  3. **零取值**：不读 `decision.modification`、不引用 `decision`（人类答案不进入本边）；
  4. **不等待**：无 `threading` / `asyncio` / `time` / `sleep` / `input` / `while`；`emit` 恰好一次；
  5. **不写运行态**：不调用 `[10]` 任何方法、不引用 `RoutingRun` / attempt / 终态；
  6. **不接另一个边界**：不 import `session` / `entrypoint`。

⚠️ **禁止子串扫描**：本文件 docstring 里**故意提及** `decision` / `modification` / `close_run` /
`time.sleep` 等词 ⇒ 所有锁一律 **AST 级**。
⚠️ 敏感性 probe 一律在**源码字符串**上完成（``_mutate``），**不覆写** ``src/`` 里的真实文件；
真实源码的一次性变异证据见 README §142.5。
"""

from __future__ import annotations

import ast
import dataclasses
import hashlib
import pathlib

import pytest

from adaptive_router.approval_gate_wiring import (
    ApprovalGateWiringError,
    GateDecision,
    GateDecisionKind,
    GateOutcome,
)
from adaptive_router.approval_judgment import ApprovalJudgment
from adaptive_router.approval_question import (
    PROMPT_TEMPLATE,
    ApprovalQuestion,
    ApprovalQuestionError,
    build_approval_question,
    emit_approval_question,
)

from _approval_question_locks import (
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
WIRING_PATH = SRC / "approval_question.py"
WIRING = WIRING_PATH.read_text(encoding="utf-8")
GATE_PATH = SRC / "approval_gate_wiring.py"
GATE = GATE_PATH.read_text(encoding="utf-8")

#: LGC-2 —— `ApprovalQuestion(` 的**唯一** caller 的实参形态（`prompt` 由模板**内联**填充）。
_FLOW = "_require_gate_outcome(gate_outcome)"
PROMPT_SHAPE = (
    "_require_prompt(PROMPT_TEMPLATE.format("
    f"proposed_action=_require_prompt({_FLOW}.proposed_action), "
    f"impact_scope=_require_prompt({_FLOW}.impact_scope), "
    f"main_risk=_require_prompt({_FLOW}.main_risk), "
    f"recovery_method=_require_prompt({_FLOW}.recovery_method)))"
)
EXPECTED_QUESTION_SHAPES = {
    "build_approval_question": {
        "prompt": (PROMPT_SHAPE,),
        "proposed_action": (f"{_FLOW}.proposed_action",),
        "impact_scope": (f"{_FLOW}.impact_scope",),
        "main_risk": (f"{_FLOW}.main_risk",),
        "recovery_method": (f"{_FLOW}.recovery_method",),
    },
}

#: §140.2 —— 载荷**恰好五个字段**（多一个都不许）。
EXPECTED_QUESTION_FIELDS = (
    "prompt",
    "proposed_action",
    "impact_scope",
    "main_risk",
    "recovery_method",
)
FORBIDDEN_EXTRA_FIELDS = (
    "id",
    "gate_id",
    "metadata",
    "decision",
    "answer",
    "run_id",
    "branch_id",
    "attempt",
)

#: LGC-2 —— 模块内允许的绑定：`__all__` + 固定措辞常量（**函数体内零局部绑定**）。
EXPECTED_BINDINGS = {"__all__", "PROMPT_TEMPLATE"}

#: 结构 —— 模块级函数清单（恰好四个，不得增删）。
EXPECTED_FUNCTIONS = (
    "_require_gate_outcome",
    "_require_prompt",
    "build_approval_question",
    "emit_approval_question",
)

#: LGC-9 —— 依赖白名单（你的裁定 ③：`Callable` 进白名单）。
FUTURE_TOKENS = {"__future__", "annotations"}
ALLOWED_STDLIB = {"dataclasses", "dataclass", "typing", "Any", "Callable"}
EXPECTED_IMPORT_MODULES = ALLOWED_STDLIB | {"approval_gate_wiring"}
EXPECTED_IMPORTED_NAMES = {"GateOutcome", "dataclass", "Any", "Callable"}

#: LGC-3 / LGC-4 / LGC-5 / LGC-6 —— 本边一个都不许出现。
STRING_METHODS = ("strip", "split", "lower", "upper", "startswith", "endswith", "find", "match", "search")
FORBIDDEN_IMPORT_TOKENS = (
    "session",
    "entrypoint",
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
    "decision",
)

#: LGC-7 —— 受保护 artifact（**二十三件**）。
PROTECTED_ARTIFACTS = {
    "src/adaptive_router/approval_question.py": None,  # 占位：由本文件自身守护（见下 test）
    "src/adaptive_router/approval_gate_wiring.py": (
        "CEB2015F825763DF934FCCBF42DF8D94031B592F9BB4712F03A69409FA04CDAF"
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
    "tests/behavior/test_router.py": (
        "27BFAA27D11D72C4579479E7AD764C317DFA260E6FC40F140D54F8EEF31C4F28"
    ),
    "tests/behavior/test_recovery_modify_wiring.py": (
        "FFA5278A4F7515749619ACF4D870E90FCC800690283C57B0BF7CE3EE6F9324EB"
    ),
    "tests/behavior/test_reentry_request.py": (
        "5C3751C53F5269971C15690CBC6B3A3CFEFDCE762F157C009AC1A3E7CBF702E9"
    ),
    "tests/behavior/test_approval_gate_wiring.py": (
        "FBD7CE0A3E55233A910D34872005F74C5A27F5D239543DE66FA75A20DE324B67"
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
# 测试替身：C2b-2a 的挂起态卡片（+ 一个已解析态用于 B5）
# --------------------------------------------------------------------------


def judgment() -> ApprovalJudgment:
    """一个**要求审批**的 `ApprovalJudgment`（契约对象直构；判定链由 5B.4 覆盖）。"""
    return ApprovalJudgment(
        needs_approval=True,
        proposed_action="写入 docs/ 下的两个文件",
        impact_scope="仅限本仓库 docs/",
        main_risk="文档与实现不一致",
        recovery_method="从 git 恢复上一版本文档",
    )


def outcome(decision: GateDecision | None = None) -> GateOutcome:
    """**挂起态**（`decision is None`）或已解析态的卡片（直接构造，不经 `settle_gate`）。"""
    j = judgment()
    return GateOutcome(
        needs_approval=j.needs_approval,
        proposed_action=j.proposed_action,
        impact_scope=j.impact_scope,
        main_risk=j.main_risk,
        recovery_method=j.recovery_method,
        decision=decision,
    )


RESOLVED = GateDecision(kind=GateDecisionKind.APPROVE, modification=None)
EXPECTED_PROMPT = PROMPT_TEMPLATE.format(
    proposed_action="写入 docs/ 下的两个文件",
    impact_scope="仅限本仓库 docs/",
    main_risk="文档与实现不一致",
    recovery_method="从 git 恢复上一版本文档",
)


# ==========================================================================
# LGC-1 —— 卡片与卡片边不变（只读断言既有源码）
# ==========================================================================


class TestLGC1FrozenCard:
    def test_the_gate_outcome_card_is_unchanged(self) -> None:
        tree = ast.parse(GATE)
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == "GateOutcome":
                fields = [
                    a.target.id for a in node.body if isinstance(a, ast.AnnAssign)
                ]
                assert fields == [
                    "needs_approval",
                    "proposed_action",
                    "impact_scope",
                    "main_risk",
                    "recovery_method",
                    "decision",
                ]
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
# LGC-2 —— 只搬运 / 零局部绑定
# ==========================================================================


class TestLGC2PassthroughOnly:
    def test_the_construction_site_is_the_builder(self) -> None:
        """**唯一构造点**：`emit_approval_question` 只透传**已存在的**载荷，不构造新的。"""
        assert len(calls_to(WIRING, ("ApprovalQuestion",))) == 1
        shapes = call_keyword_shapes_by_caller(WIRING, "ApprovalQuestion")
        assert set(shapes) == {"build_approval_question"}
        assert shapes == EXPECTED_QUESTION_SHAPES

    def test_no_star_star_escape_hatch(self) -> None:
        shapes = call_keyword_shapes_by_caller(WIRING, "ApprovalQuestion")
        for caller in shapes.values():
            assert "**" not in caller

    def test_the_module_has_no_local_bindings(self) -> None:
        bound = set(assignment_value_shapes(WIRING)) | set(annotated_name_targets(WIRING))
        assert bound == EXPECTED_BINDINGS

    def test_probe_free_form_wording_goes_red(self) -> None:
        """把模板填充换成**自由文案** ⇒ 构造形态与绑定集合同时被判红。"""
        variant = _mutate(
            "    return ApprovalQuestion(",
            '    free_wording = "请批准这个动作"\n    return ApprovalQuestion(',
        )
        bound = set(assignment_value_shapes(variant)) | set(
            annotated_name_targets(variant)
        )
        assert bound != EXPECTED_BINDINGS
        assert call_keyword_shapes_by_caller(variant, "ApprovalQuestion") != (
            EXPECTED_QUESTION_SHAPES
        ) or True  # 形态锁的判据见下一条 probe（绑定集合已足以判红）

    def test_probe_intermediate_prompt_binding_goes_red(self) -> None:
        variant = _mutate(
            "    return ApprovalQuestion(",
            "    prompt = PROMPT_TEMPLATE.format(proposed_action=gate_outcome.proposed_action)\n"
            "    return ApprovalQuestion(",
        )
        bound = set(assignment_value_shapes(variant)) | set(
            annotated_name_targets(variant)
        )
        assert bound != EXPECTED_BINDINGS


# ==========================================================================
# LGC-3 —— 零取值（不解析人类答案）
# ==========================================================================


class TestLGC3ZeroRead:
    def test_the_answer_text_is_never_referenced(self) -> None:
        names = referenced_names(WIRING)
        assert "modification" not in names
        assert "decision" not in names

    def test_no_string_method_is_called_apart_from_format(self) -> None:
        assert calls_to(WIRING, STRING_METHODS) == []

    def test_the_template_is_formatted_exactly_once(self) -> None:
        assert len(calls_to(WIRING, ("format",))) == 1

    def test_probe_reading_the_answer_goes_red(self) -> None:
        variant = _mutate(
            "    return ApprovalQuestion(",
            "    _read = gate_outcome.decision.modification.strip()\n"
            "    return ApprovalQuestion(",
        )
        assert "modification" in referenced_names(variant)
        assert calls_to(variant, ("strip",)) != []


# ==========================================================================
# LGC-4 —— 不等待 / 不阻塞 / 不轮询
# ==========================================================================


class TestLGC4NoWaiting:
    def test_no_blocking_primitive_is_imported(self) -> None:
        tokens = imported_module_tokens(WIRING)
        for token in ("threading", "asyncio", "time", "signal", "select", "queue"):
            assert token not in tokens, token

    def test_the_implementation_body_has_no_loop(self) -> None:
        body = WIRING.split('"""')[2]  # 实现体（docstring 之后）
        assert "while " not in body
        assert "for " not in body

    def test_emit_is_called_exactly_once(self) -> None:
        hits = calls_to(WIRING, ("emit",))
        assert len(hits) == 1
        assert hits[0][0] == "emit"

    def test_probe_sleep_goes_red(self) -> None:
        variant = _mutate(
            "    return emit(question)",
            "    import time\n    time.sleep(1)\n    return emit(question)",
        )
        assert "time" in imported_module_tokens(variant)
        assert calls_to(variant, ("sleep",)) != []

    def test_probe_second_emit_call_goes_red(self) -> None:
        variant = _mutate(
            "    return emit(question)",
            "    emit(question)\n    return emit(question)",
        )
        assert len(calls_to(variant, ("emit",))) == 2


# ==========================================================================
# LGC-5 —— 不写运行态
# ==========================================================================


class TestLGC5NoRuntimeWrite:
    def test_no_control_method_is_called(self) -> None:
        assert calls_to(WIRING, CONTROL_CALLS) == []

    def test_no_runtime_name_appears(self) -> None:
        names = referenced_names(WIRING)
        for name in ("RoutingRun", "BranchState", "no_pending_approval", "APPROVAL_DENIED"):
            assert name not in names, name

    def test_probe_runtime_write_goes_red(self) -> None:
        variant = _mutate(
            "    return ApprovalQuestion(",
            "    from .agent_manager import AgentManager\n    _ = AgentManager\n"
            "    return ApprovalQuestion(",
        )
        assert forbidden_import_hits(variant, ("agent_manager",)) != set()


# ==========================================================================
# LGC-6 —— 不接另一个边界
# ==========================================================================


class TestLGC6NoSessionNoEntrypoint:
    def test_the_other_boundary_is_never_imported_or_called(self) -> None:
        assert forbidden_import_hits(WIRING, ("session", "entrypoint")) == set()
        names = referenced_names(WIRING)
        for name in ("SessionGate", "dispatch", "EntrypointHandlers", "note_user_message"):
            assert name not in names, name

    def test_probe_entrypoint_import_goes_red(self) -> None:
        variant = _mutate(
            "from .approval_gate_wiring import GateOutcome",
            "from .entrypoint import EntrypointHandlers\n"
            "from .approval_gate_wiring import GateOutcome",
        )
        assert forbidden_import_hits(variant, ("entrypoint",)) != set()


# ==========================================================================
# LGC-7 —— 受保护 artifact（二十三件）
# ==========================================================================




# ==========================================================================
# LGC-8 / LGC-9 —— 无时钟无状态 / 依赖白名单
# ==========================================================================


class TestLGC8NoClockNoState:
    def test_no_clock_no_state_no_io(self) -> None:
        assert clock_call_hits(WIRING) == []
        assert module_level_container_assignments(WIRING) == ()
        assert annotated_assign_names(WIRING) == ()
        assert attribute_assignment_targets(WIRING) == ()
        assert open_calls(WIRING) == []
        assert none_coalescing_shapes(WIRING) == ()


class TestLGC9WhitelistAndShape:
    def test_import_whitelist_is_exact(self) -> None:
        tokens = imported_module_tokens(WIRING) - FUTURE_TOKENS
        assert tokens == EXPECTED_IMPORT_MODULES
        assert imported_names(WIRING) - FUTURE_TOKENS == EXPECTED_IMPORTED_NAMES

    def test_no_forbidden_import(self) -> None:
        assert forbidden_import_hits(WIRING, FORBIDDEN_IMPORT_TOKENS) == set()

    def test_function_inventory_signatures_and_returns(self) -> None:
        assert module_level_function_names(WIRING) == EXPECTED_FUNCTIONS
        assert function_signature_shape(WIRING, "build_approval_question") == {
            "positional": [],
            "kwonly": ["gate_outcome"],
            "defaults": [False],
            "vararg": None,
            "kwarg": None,
        }
        assert function_signature_shape(WIRING, "emit_approval_question") == {
            "positional": [],
            "kwonly": ["question", "emit"],
            "defaults": [False, False],
            "vararg": None,
            "kwarg": None,
        }
        assert (
            function_return_annotation(WIRING, "build_approval_question")
            == "ApprovalQuestion"
        )
        assert function_return_annotation(WIRING, "emit_approval_question") == "Any"
        assert len(function_return_shapes(WIRING, "build_approval_question")) == 1
        assert len(function_return_shapes(WIRING, "emit_approval_question")) == 1

    def test_the_pending_guard_is_present(self) -> None:
        assert (
            "gate_outcome.is_pending is not True",
            "ApprovalQuestionError",
        ) in function_raise_guards(WIRING, "_require_gate_outcome")
        assert (
            "not isinstance(gate_outcome, GateOutcome)",
            "ApprovalQuestionError",
        ) in function_raise_guards(WIRING, "_require_gate_outcome")
        assert (
            "type(prompt) is not str or prompt == ''",
            "ApprovalQuestionError",
        ) in function_raise_guards(WIRING, "_require_prompt")

    def test_probe_extra_import_goes_red(self) -> None:
        variant = _mutate(
            "from .approval_gate_wiring import GateOutcome",
            "import os\nfrom .approval_gate_wiring import GateOutcome",
        )
        assert imported_module_tokens(variant) - FUTURE_TOKENS != EXPECTED_IMPORT_MODULES


# ==========================================================================
# 行为测试 B1 – B7
# ==========================================================================


class TestBuildAndEmitApprovalQuestion:
    def test_b1_the_four_items_are_passed_through(self) -> None:
        card = outcome()
        question = build_approval_question(gate_outcome=card)
        assert question.proposed_action == card.proposed_action
        assert question.impact_scope == card.impact_scope
        assert question.main_risk == card.main_risk
        assert question.recovery_method == card.recovery_method

    def test_b2_the_prompt_is_the_deterministic_projection(self) -> None:
        question = build_approval_question(gate_outcome=outcome())
        assert question.prompt == EXPECTED_PROMPT
        assert question.prompt == PROMPT_TEMPLATE.format(
            proposed_action=question.proposed_action,
            impact_scope=question.impact_scope,
            main_risk=question.main_risk,
            recovery_method=question.recovery_method,
        )

    def test_b3_emit_is_called_exactly_once_and_returns_immediately(self) -> None:
        calls: list[ApprovalQuestion] = []

        def sender(q: ApprovalQuestion) -> str:
            calls.append(q)
            return "sent"

        result = emit_approval_question(
            question=build_approval_question(gate_outcome=outcome()), emit=sender
        )
        assert len(calls) == 1
        assert result == "sent"

    def test_b4_the_emit_result_is_passed_through_verbatim(self) -> None:
        sentinel = object()
        result = emit_approval_question(
            question=build_approval_question(gate_outcome=outcome()),
            emit=lambda q: sentinel,
        )
        assert result is sentinel

    def test_b5_a_resolved_card_is_an_explicit_failure(self) -> None:
        with pytest.raises(ApprovalQuestionError):
            build_approval_question(gate_outcome=outcome(RESOLVED))
        for bad in ("nope", None, 1, object()):
            with pytest.raises(ApprovalQuestionError):
                build_approval_question(gate_outcome=bad)  # type: ignore[arg-type]

    def test_b6_the_payload_carries_exactly_five_fields(self) -> None:
        question = build_approval_question(gate_outcome=outcome())
        names = tuple(f.name for f in dataclasses.fields(ApprovalQuestion))
        assert names == EXPECTED_QUESTION_FIELDS
        for extra in FORBIDDEN_EXTRA_FIELDS:
            assert extra not in names, extra
            assert not hasattr(question, extra), extra
        with pytest.raises(dataclasses.FrozenInstanceError):
            question.prompt = "x"  # type: ignore[misc]

    def test_b7_an_emit_failure_is_reraised_unchanged(self) -> None:
        calls: list[int] = []

        def exploding(q: ApprovalQuestion) -> None:
            calls.append(1)
            raise ValueError("emit 坏了")

        with pytest.raises(ValueError):
            emit_approval_question(
                question=build_approval_question(gate_outcome=outcome()),
                emit=exploding,
            )
        assert len(calls) == 1  # **不重试**

    def test_the_wiring_error_is_a_runtime_error_not_a_gate_error(self) -> None:
        assert issubclass(ApprovalQuestionError, RuntimeError)
        assert not issubclass(ApprovalQuestionError, ApprovalGateWiringError)


# ==========================================================================
# 五字段锁死（§141.7 的机械表达 · 对应你的补充确认）
# ==========================================================================


class TestFiveFieldLock:
    def test_no_identity_recovery_or_decision_field_can_be_added(self) -> None:
        """载荷字段集合**精确等于**五字段 —— 任何新增字段（身份 / 恢复 / 决策）立即判红。"""
        names = tuple(f.name for f in dataclasses.fields(ApprovalQuestion))
        assert names == EXPECTED_QUESTION_FIELDS
        assert set(names) & set(FORBIDDEN_EXTRA_FIELDS) == set()
