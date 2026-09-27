"""``C2b-2c-3 恢复意图`` —— 结构锁 **LRS-1 – LRS-9** + 行为 **B1 – B7**。

架构依据：`docs/architecture.md` **Approval Gate 节点行**（五步图 · V1 三出口）·
**§4.6**（R4 本身即授权）· **6.4 / L47**（`[10]` 运行态唯一写者）· **9.1**（`total_attempt_limit = 2`）·
**A13.3**（`branches` = attempt history）· **A13.5**（显式外部事实注入先例）· **A18.7 / `P2`**（DEFERRED）。
范围、冻结、授权与实现记录见 `tests/behavior/README_step5b.md` **§149–§153**。

**本模块要守住的五条主张：**

  1. **Gate 三件套不变**：不构造 `GateOutcome` / `GateDecision`，不改 `settle_gate`；
  2. **唯一载体 / 只搬运**：五字段逐字来自形参，函数体内零局部绑定；
  3. **零取值**：不读 `decision.modification`，人类答案文本不进入本边；
  4. **只对已解析卡片 + 地址显式自洽**（`1 <= attempt <= total_attempt_limit`）；
  5. **不写运行态 / 不推进 attempt / 不建注册表 / 不接另一边界**。

⚠️ **禁止子串扫描**：本文件 docstring 里**故意提及** `modification` / `close_run` / `run_id`
等词 ⇒ 所有锁一律 **AST 级**。
⚠️ 敏感性 probe 一律在**源码字符串**上完成（``_mutate``），**不覆写** ``src/`` 里的真实文件；
真实源码的一次性变异证据见 README §153.5。
"""

from __future__ import annotations

import dataclasses
import hashlib
import pathlib

import pytest

from adaptive_router.approval_gate_wiring import GateDecision, GateDecisionKind, GateOutcome
from adaptive_router.contracts import ProjectContext
from adaptive_router.resume_intent import (
    ResumeIntent,
    ResumeIntentError,
    build_resume_intent,
)

from _resume_intent_locks import (
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
WIRING_PATH = SRC / "resume_intent.py"
WIRING = WIRING_PATH.read_text(encoding="utf-8")
GATE_PATH = SRC / "approval_gate_wiring.py"
GATE = GATE_PATH.read_text(encoding="utf-8")
QUESTION_PATH = SRC / "approval_question.py"
QUESTION = QUESTION_PATH.read_text(encoding="utf-8")

#: LRS-1 —— Gate 侧两张卡片的冻结字段（**只读**断言既有源码）。
EXPECTED_OUTCOME_FIELDS = [
    "needs_approval",
    "proposed_action",
    "impact_scope",
    "main_risk",
    "recovery_method",
    "decision",
]
EXPECTED_QUESTION_FIELDS = [
    "prompt",
    "proposed_action",
    "impact_scope",
    "main_risk",
    "recovery_method",
]

#: LRS-2 —— `ResumeIntent(` 的**唯一** caller 与它的实参形态。
_REQ = "_require_attempt_and_limit(attempt, total_attempt_limit)"
EXPECTED_INTENT_SHAPES = {
    "build_resume_intent": {
        "gate_outcome": ("_require_authorization(gate_outcome)",),
        "run_id": ("_require_run_id(run_id)",),
        "branch_id": ("_require_branch_id(branch_id)",),
        "attempt": (f"{_REQ}[0]",),
        "total_attempt_limit": (f"{_REQ}[1]",),
    }
}

#: §150.2 —— 载荷**恰好五个字段**（多一个都不许）。
EXPECTED_INTENT_FIELDS = (
    "gate_outcome",
    "run_id",
    "branch_id",
    "attempt",
    "total_attempt_limit",
)
FORBIDDEN_PAYLOAD_FIELDS = (
    "decision",
    "modification",
    "rationale",
    "target",
    "route_to",
    "terminal",
    "answer",
)

#: LRS-9 —— 模块级函数清单（**按源码定义顺序**，恰好六个）。
EXPECTED_FUNCTIONS = (
    "_require_authorization",
    "_require_run_id",
    "_require_branch_id",
    "_require_attempt_and_limit",
    "_require_forbidden_fields",
    "build_resume_intent",
)

#: LRS-2 —— 模块内允许的绑定：`__all__` + **越界字段名常量**（本边唯一的模块级常量）。
#: ⚠️ `_FORBIDDEN_FACT_FIELDS` 是"**授权事实侧不得出现的字段名**"这一**边界声明**的载体
#: ⇒ 函数体内**仍然零局部绑定**（见 §153.7 的偏差登记）。
EXPECTED_BINDINGS = {"__all__", "_FORBIDDEN_FACT_FIELDS"}

#: LRS-9 —— 依赖白名单（精确集合）。
FUTURE_TOKENS = {"__future__", "annotations"}
EXPECTED_IMPORT_MODULES = {"approval_gate_wiring", "contracts", "dataclasses", "dataclass"}
EXPECTED_IMPORTED_NAMES = {"GateOutcome", "BranchId", "RunId", "dataclass"}

#: LRS-3 / LRS-5 / LRS-6 —— 本边一个都不许出现。
STRING_METHODS = ("strip", "split", "lower", "upper", "startswith", "endswith", "find", "match", "search")
FORBIDDEN_IMPORT_TOKENS = (
    "agent_manager",
    "scheduler",
    "execution",
    "session",
    "entrypoint",
    "routing_plan",
    "reentry_request",
    "recovery",
    "terminal_wiring",
    "router",
    "policy",
    "adapters",
    "cost",
    "risk",
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
    "AgentRun",
    "BranchState",
    "no_pending_approval",
    "APPROVAL_DENIED",
    "AgentManager",
    "modification",
    "decision",
)

#: LRS-7 —— 受保护 artifact（**二十六件**；不含本文件自身）。
PROTECTED_ARTIFACTS = {
    "src/adaptive_router/approval_gate_wiring.py": (
        "CEB2015F825763DF934FCCBF42DF8D94031B592F9BB4712F03A69409FA04CDAF"
    ),
    "src/adaptive_router/approval_question.py": (
        "04B4274E79F4579DFD1A971CFDE4B0A132368042A7A604259E8ACEBBA7A433E0"
    ),
    "src/adaptive_router/approval_answer.py": (
        "9FDF7EF527976E1FACF86111EA7180E55095C6FDAA9CC87BB060FB43C0900C3C"
    ),
    "src/adaptive_router/agent_manager.py": (
        "154B171E0F2A7D568FCD61318869EEA05DA0D9A4140640A6A89860D13D0D01C5"
    ),
    "src/adaptive_router/entrypoint.py": (
        "43B635D937D228421B6902CF5B157B79A2476FA6DDB1BCDE7FDB5B65DC00361C"
    ),
    "src/adaptive_router/session.py": (
        "A8483929115C1F197DB20E286550EDC558A392BFDC8603B4D387220F4D78C695"
    ),
    "src/adaptive_router/router.py": (
        "B64F82C52C6FBFA1DC2D8EC5165F3328D0004BBEBA2424B518C02AC3FE1B07D3"
    ),
    "src/adaptive_router/policy.py": (
        "14C5BEDD562E3F99C308F3CCFDA1FA717A08CB3FDA6970FF771B1F4C62ADFA08"
    ),
    "src/adaptive_router/routing_plan.py": (
        "ABE17DA0879D8F2F6AF8464C01E60DCA7AD4FC2DE6F4590E8A3F7E4CF7161475"
    ),
    "src/adaptive_router/contracts/runtime.py": (
        "6466A4E628754002FB43515A2E2D15ECBF6398AE4204C07A256621673173F379"
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
    "src/adaptive_router/recovery_modify_wiring.py": (
        "D56E81F45858161060941A51E10C0D828D6A8E59441E0025882037E3AEEECCEB"
    ),
    "src/adaptive_router/reentry_request.py": (
        "F836A15BEAA91859CF502B08B23CD115830D5BC1FB76ED8BF7CEA5150A798F14"
    ),
    "src/adaptive_router/terminal_wiring.py": (
        "2B317FEABD72313465CDBE86659E58ED35CBDC24EE7CBD694AEAB357C92C0691"
    ),
    "src/adaptive_router/cost/ledger_wiring.py": (
        "6F0F3DBCA6970F8873115AB672434A49D6227F017A8BCD55F7B6075B431F0B7F"
    ),
    "tests/behavior/_recovery_route_locks.py": (
        "F22A5A49B0216BEE6FEDBC258C27E63A57A74425C5C70FB84B69196F77D1F023"
    ),
    "tests/behavior/_recovery_regate_locks.py": (
        "F260F8514119F14A34C31758EE34590012E27E2ABCD3E4232A3211430262897D"
    ),
    "tests/behavior/_recovery_modify_locks.py": (
        "E6A0495005CA86287435A15005044A4B0C3FDA6C9F6DF4B002105100461FBCF0"
    ),
    "tests/behavior/_approval_question_locks.py": (
        "C647659F7E831000B61356AB001C13E5EA3AA6EB8468EA85119613B42AEB5AB9"
    ),
    "tests/behavior/_approval_answer_locks.py": (
        "2B01AFD9EF4C12162E857D1B76495C259BBE1BC05213AD3990ED8831F2FC90F3"
    ),
    "tests/behavior/test_router.py": (
        "27BFAA27D11D72C4579479E7AD764C317DFA260E6FC40F140D54F8EEF31C4F28"
    ),
    "tests/behavior/test_approval_question.py": (
        "533A1595D6D35C2ABE13AAAEDB2079CD0B440A24667F967F78FB3545F2CBB016"
    ),
    "tests/behavior/test_approval_answer.py": (
        "4008E4D707CBF623D3CE6ABBA65E77941DDE9AFBE9781BE6F95D8534D5305942"
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
# 测试替身：已解析 / 挂起卡片 + 合法地址
# --------------------------------------------------------------------------


def card(*, resolved: bool = True) -> GateOutcome:
    """由 `settle` 的语义直构：`resolved=True` ⇒ `is_pending is False`。"""
    decision = (
        GateDecision(kind=GateDecisionKind.APPROVE, modification=None) if resolved else None
    )
    return GateOutcome(
        needs_approval=True,
        proposed_action="写入 docs/ 下的两个文件",
        impact_scope="仅限本仓库 docs/",
        main_risk="文档与实现不一致",
        recovery_method="从 git 恢复上一版本文档",
        decision=decision,
    )


MODIFY = GateDecision(kind=GateDecisionKind.MODIFY, modification="只允许读，不允许写")
DENY = GateDecision(kind=GateDecisionKind.DENY, modification=None)

ADDRESS = {
    "run_id": "run_42",
    "branch_id": "br_main",
    "attempt": 2,
    "total_attempt_limit": 2,
}


def build(**overrides: object):
    """便利构造：默认给出**合法**输入（已解析卡片 + 合法地址）。"""
    kwargs = {"gate_outcome": card(), **ADDRESS}
    kwargs.update(overrides)
    return build_resume_intent(**kwargs)  # type: ignore[arg-type]


# ==========================================================================
# LRS-1 —— Gate 三件套不变
# ==========================================================================


class TestLRS1FrozenGateSide:
    def test_the_gate_cards_are_unchanged(self) -> None:
        import ast

        for source, cls, expected in (
            (GATE, "GateOutcome", EXPECTED_OUTCOME_FIELDS),
            (QUESTION, "ApprovalQuestion", EXPECTED_QUESTION_FIELDS),
        ):
            tree = ast.parse(source)
            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef) and node.name == cls:
                    fields = [a.target.id for a in node.body if isinstance(a, ast.AnnAssign)]
                    assert fields == expected, cls
                    break
            else:  # pragma: no cover - 防御
                raise AssertionError(f"{cls} 未找到")

    def test_this_edge_never_constructs_the_cards(self) -> None:
        assert calls_to(WIRING, ("GateOutcome", "GateDecision", "GateDecisionKind")) == []

    def test_probe_seventh_outcome_field_goes_red(self) -> None:
        variant = GATE.replace(
            "    recovery_method: str\n    decision: GateDecision | None",
            "    recovery_method: str\n    decision: GateDecision | None\n    gate_id: str",
        )
        assert variant != GATE
        import ast

        tree = ast.parse(variant)
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == "GateOutcome":
                fields = [a.target.id for a in node.body if isinstance(a, ast.AnnAssign)]
                assert "gate_id" in fields
                return
        raise AssertionError("变异未生效")


# ==========================================================================
# LRS-2 —— 唯一载体 / 只搬运
# ==========================================================================


class TestLRS2SingleCarrier:
    def test_the_carrier_is_constructed_exactly_once(self) -> None:
        assert len(calls_to(WIRING, ("ResumeIntent",))) == 1

    def test_the_only_caller_and_argument_shapes(self) -> None:
        shapes = call_keyword_shapes_by_caller(WIRING, "ResumeIntent")
        assert set(shapes) == {"build_resume_intent"}
        assert shapes == EXPECTED_INTENT_SHAPES

    def test_no_star_star_escape_hatch(self) -> None:
        shapes = call_keyword_shapes_by_caller(WIRING, "ResumeIntent")
        assert "**" not in shapes["build_resume_intent"]

    def test_the_module_has_no_local_bindings(self) -> None:
        bound = set(assignment_value_shapes(WIRING)) | set(annotated_name_targets(WIRING))
        assert bound == EXPECTED_BINDINGS

    def test_probe_second_construction_site_goes_red(self) -> None:
        variant = _mutate(
            "    return ResumeIntent(",
            "    ResumeIntent(\n"
            "        gate_outcome=gate_outcome, run_id=run_id, branch_id=branch_id,\n"
            "        attempt=attempt, total_attempt_limit=total_attempt_limit,\n    )\n"
            "    return ResumeIntent(",
        )
        assert len(calls_to(variant, ("ResumeIntent",))) == 2

    def test_probe_fabricated_address_goes_red(self) -> None:
        variant = _mutate(
            "        run_id=_require_run_id(run_id),",
            '        run_id=_require_run_id("run_hardcoded"),',
        )
        assert call_keyword_shapes_by_caller(variant, "ResumeIntent") != EXPECTED_INTENT_SHAPES


# ==========================================================================
# LRS-3 —— 零取值
# ==========================================================================


class TestLRS3ZeroRead:
    def test_the_answer_text_is_never_referenced(self) -> None:
        names = referenced_names(WIRING)
        assert "modification" not in names
        assert "decision" not in names

    def test_no_string_method_is_called(self) -> None:
        assert calls_to(WIRING, STRING_METHODS) == []

    def test_probe_reading_the_answer_goes_red(self) -> None:
        variant = _mutate(
            "    _require_forbidden_fields(_require_authorization(gate_outcome))",
            "    _read = gate_outcome.decision.modification.strip()\n"
            "    _require_forbidden_fields(_require_authorization(gate_outcome))",
        )
        assert "modification" in referenced_names(variant)
        assert calls_to(variant, ("strip",)) != []


# ==========================================================================
# LRS-4 —— 只对已解析卡片 + 地址显式自洽
# ==========================================================================


class TestLRS4ResolvedOnlyAndAddress:
    def test_the_six_guards_are_present(self) -> None:
        assert (
            "not isinstance(gate_outcome, GateOutcome)",
            "ResumeIntentError",
        ) in function_raise_guards(WIRING, "_require_authorization")
        assert (
            "gate_outcome.is_pending is not False",
            "ResumeIntentError",
        ) in function_raise_guards(WIRING, "_require_authorization")
        assert (
            "type(run_id) is not str or run_id == ''",
            "ResumeIntentError",
        ) in function_raise_guards(WIRING, "_require_run_id")
        assert (
            "type(branch_id) is not str or branch_id == ''",
            "ResumeIntentError",
        ) in function_raise_guards(WIRING, "_require_branch_id")
        assert (
            "type(total_attempt_limit) is not int or total_attempt_limit < 1",
            "ResumeIntentError",
        ) in function_raise_guards(WIRING, "_require_attempt_and_limit")
        assert (
            "type(attempt) is not int or attempt < 1 or attempt > total_attempt_limit",
            "ResumeIntentError",
        ) in function_raise_guards(WIRING, "_require_attempt_and_limit")

    def test_probe_dropped_pending_guard_goes_red(self) -> None:
        variant = _mutate(
            "    if gate_outcome.is_pending is not False:",
            "    if False:",
        )
        assert (
            "gate_outcome.is_pending is not False",
            "ResumeIntentError",
        ) not in function_raise_guards(variant, "_require_authorization")


# ==========================================================================
# LRS-5 —— 不写运行态 / 不推进 attempt
# ==========================================================================


class TestLRS5NoRuntimeWrite:
    def test_no_control_method_is_called(self) -> None:
        assert calls_to(WIRING, CONTROL_CALLS) == []

    def test_attempt_is_never_an_assignment_target(self) -> None:
        assert "attempt" not in assignment_value_shapes(WIRING)
        assert "attempt" not in annotated_name_targets(WIRING)

    def test_no_runtime_name_appears(self) -> None:
        names = referenced_names(WIRING)
        for name in RUNTIME_NAMES:
            assert name not in names, name

    def test_no_runtime_module_is_imported(self) -> None:
        assert forbidden_import_hits(WIRING, FORBIDDEN_IMPORT_TOKENS) == set()

    def test_probe_advancing_attempt_goes_red(self) -> None:
        variant = _mutate(
            "    _require_forbidden_fields(_require_authorization(gate_outcome))",
            "    attempt = attempt + 1\n"
            "    _require_forbidden_fields(_require_authorization(gate_outcome))",
        )
        assert "attempt" in assignment_value_shapes(variant)

    def test_probe_runtime_write_goes_red(self) -> None:
        variant = _mutate(
            "    _require_forbidden_fields(_require_authorization(gate_outcome))",
            "    from .agent_manager import AgentManager\n    _ = AgentManager\n"
            "    _require_forbidden_fields(_require_authorization(gate_outcome))",
        )
        assert forbidden_import_hits(variant, ("agent_manager",)) != set()


# ==========================================================================
# LRS-6 —— 不接另一边界 / 不建注册表
# ==========================================================================


class TestLRS6NoOtherBoundary:
    def test_the_other_boundaries_are_not_imported(self) -> None:
        assert forbidden_import_hits(
            WIRING, ("session", "entrypoint", "routing_plan", "reentry_request", "recovery")
        ) == set()

    def test_no_module_level_registry(self) -> None:
        assert module_level_container_assignments(WIRING) == ()
        assert annotated_assign_names(WIRING) == ()

    def test_probe_registry_goes_red(self) -> None:
        variant = _mutate(
            "class ResumeIntentError(RuntimeError):",
            "_REGISTRY: dict = {}\n\n\nclass ResumeIntentError(RuntimeError):",
        )
        assert module_level_container_assignments(variant) != () or (
            annotated_assign_names(variant) != ()
        )


# ==========================================================================
# LRS-7 —— 受保护 artifact（二十六件）
# ==========================================================================




# ==========================================================================
# LRS-8 / LRS-9 —— 无时钟无状态 / 依赖白名单
# ==========================================================================


class TestLRS8NoClockNoState:
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


class TestLRS9WhitelistAndShape:
    def test_import_whitelist_is_exact(self) -> None:
        tokens = imported_module_tokens(WIRING) - FUTURE_TOKENS
        assert tokens == EXPECTED_IMPORT_MODULES
        assert imported_names(WIRING) - FUTURE_TOKENS == EXPECTED_IMPORTED_NAMES

    def test_function_inventory_signature_and_return(self) -> None:
        assert module_level_function_names(WIRING) == EXPECTED_FUNCTIONS
        assert function_signature_shape(WIRING, "build_resume_intent") == {
            "positional": [],
            "kwonly": ["gate_outcome", "run_id", "branch_id", "attempt", "total_attempt_limit"],
            "defaults": [False, False, False, False, False],
            "vararg": None,
            "kwarg": None,
        }
        assert function_return_annotation(WIRING, "build_resume_intent") == "ResumeIntent"
        assert len(function_return_shapes(WIRING, "build_resume_intent")) == 1

    def test_probe_extra_import_goes_red(self) -> None:
        variant = _mutate(
            "from .contracts import BranchId, RunId",
            "import os\nfrom .contracts import BranchId, RunId",
        )
        assert imported_module_tokens(variant) - FUTURE_TOKENS != EXPECTED_IMPORT_MODULES


# ==========================================================================
# 行为测试 B1 – B7
# ==========================================================================


class TestBuildResumeIntent:
    def test_b1_the_five_fields_are_passed_through(self) -> None:
        resolved = card()
        out = build(gate_outcome=resolved)
        assert out.gate_outcome is resolved
        assert out.run_id == ADDRESS["run_id"]
        assert out.branch_id == ADDRESS["branch_id"]
        assert out.attempt == ADDRESS["attempt"]
        assert out.total_attempt_limit == ADDRESS["total_attempt_limit"]

    def test_b2_a_pending_card_is_an_explicit_failure(self) -> None:
        with pytest.raises(ResumeIntentError):
            build(gate_outcome=card(resolved=False))
        for bad in ("nope", None, 1, object()):
            with pytest.raises(ResumeIntentError):
                build(gate_outcome=bad)

    def test_b3_the_address_must_be_explicitly_valid(self) -> None:
        for bad in ("", 1, None, True):
            with pytest.raises(ResumeIntentError):
                build(run_id=bad)
            with pytest.raises(ResumeIntentError):
                build(branch_id=bad)

    def test_b4_the_attempt_bounds_are_enforced(self) -> None:
        for bad in (0, -1, 3, "2", True):
            with pytest.raises(ResumeIntentError):
                build(attempt=bad)
        for bad in (0, -1, "2", True):
            with pytest.raises(ResumeIntentError):
                build(total_attempt_limit=bad, attempt=1)
        # 边界内的合法取值：1 与上限都允许
        assert build(attempt=1, total_attempt_limit=2).attempt == 1
        assert build(attempt=2, total_attempt_limit=2).attempt == 2

    def test_b5_the_carrier_does_not_explain_the_destination(self) -> None:
        first = build(gate_outcome=card())
        second = build(gate_outcome=card())
        assert dataclasses.replace(first, gate_outcome=second.gate_outcome) == second
        names = tuple(f.name for f in dataclasses.fields(ResumeIntent))
        assert names == EXPECTED_INTENT_FIELDS

    def test_b6_the_payload_carries_exactly_five_fields(self) -> None:
        out = build()
        names = tuple(f.name for f in dataclasses.fields(ResumeIntent))
        assert names == EXPECTED_INTENT_FIELDS
        for extra in FORBIDDEN_PAYLOAD_FIELDS:
            assert extra not in names, extra
            assert not hasattr(out, extra), extra
        with pytest.raises(dataclasses.FrozenInstanceError):
            out.attempt = 3  # type: ignore[misc]

    def test_b7_it_does_not_write_runtime_state(self) -> None:
        resolved = card()
        out = build(gate_outcome=resolved, attempt=2, total_attempt_limit=2)
        assert resolved.is_pending is False  # 入参未被改动
        assert out.gate_outcome is resolved
        assert not hasattr(out, "manager")
        assert not hasattr(out, "routing_run")

    def test_the_three_exits_share_the_same_carrier(self) -> None:
        """三个出口（APPROVE / DENY / MODIFY）产出**同形**意图 —— 载体**不含**去向字段。"""
        for decision in (MODIFY, DENY):
            resolved = GateOutcome(
                needs_approval=True,
                proposed_action="a",
                impact_scope="b",
                main_risk="c",
                recovery_method="d",
                decision=decision,
            )
            out = build(gate_outcome=resolved)
            assert tuple(f.name for f in dataclasses.fields(out)) == EXPECTED_INTENT_FIELDS
            assert out.gate_outcome is resolved

    def test_the_wiring_error_is_a_runtime_error(self) -> None:
        assert issubclass(ResumeIntentError, RuntimeError)
        assert not issubclass(ResumeIntentError, ValueError)

    def test_the_project_context_type_is_importable_but_unused(self) -> None:
        """本边**不读** `task` / `context`（那是主链重入载荷的事，属 C2b-2b-2）。"""
        assert "ProjectContext" not in referenced_names(WIRING)
        assert ProjectContext is not None
