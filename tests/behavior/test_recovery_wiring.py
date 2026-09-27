"""``[13] → [14] Wiring`` —— 结构锁 **LRW-1 – LRW-11** + 行为测试 **B1 – B8**。

架构依据：`docs/architecture.md` 的 **A16**（判定权与配对）/ **A17**（`[14]` 的决策语义，
含 A17.6 身份链与 `attempt` 层级、A17.7 收敛归 `[10]`、`INV-20`）；
范围、缺口裁定与锁设计：`tests/behavior/README_step5b.md` **§86（preflight）/ §87（scope review）**。

**本模块要守住的三条主张：**

  1. **载体恒定且不重建**：`RecoveryOutcome` **唯一构造点**；`decision` 是 `disce_recovery` 的
     **同一对象**；`decision is None`（到上限）**仍然带回身份**；
  2. **接线不决策、不发明事实、不扩大边界**：`RecoveryDecision(` 构造点 = **0**、
     不复制映射规则、不 import `adapters`、不写运行态、不碰收敛事实；
  3. **PASS 被结构拒收**：`route_failed_branch` 收到 PASS 配对见证 ⇒ **构造期显式失败**
     （不是静默 `None`）。

⚠️ **禁止子串扫描**：本文件 docstring 里**故意提及** `control_flow_converged` /
`CapabilityMismatch` / `BranchState` 等词（说明边界）⇒ 所有锁一律 **AST 级**。
⚠️ 敏感性 probe 一律在**源码字符串**上完成，**不覆写** `src/` 里的真实文件；
真实源码的一次性变异证据见 README §88.5。
"""

from __future__ import annotations

import pathlib

import pytest

from adaptive_router.contracts import (
    BranchReturn,
    Confidence,
    ConfirmationSource,
    EffortLevel,
    ExecutionValue,
    FailureClass,
    ModelTier,
    RecoveryDecision,
    RecoveryTarget,
    ValidationRecord,
    new_agent_id,
    new_branch_id,
)
from adaptive_router.recovery import decide_recovery
from adaptive_router.recovery_context import RecoveryContext
from adaptive_router.recovery_wiring import (
    RecoveryOutcome,
    route_adapter_failure,
    route_failed_branch,
)
from adaptive_router.validation_wiring import BranchValidationOutcome

from _recovery_wiring_locks import (
    attribute_chain_reads,
    calls_to,
    class_def_names,
    clock_call_hits,
    comparison_sites,
    dataclass_field_names,
    declared_public_names,
    forbidden_import_hits,
    function_call_keyword_values,
    function_return_annotation,
    function_return_shapes,
    function_signature_shape,
    handled_exception_names,
    imported_module_tokens,
    literal_call_arguments,
    mapping_get_calls,
    module_level_container_assignments,
    module_level_function_names,
    open_calls,
    property_names,
    raise_guards,
    referenced_names,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
SRC = REPO_ROOT / "src/adaptive_router"
WIRING_SOURCE = (SRC / "recovery_wiring.py").read_text(encoding="utf-8")

#: LRW-1 —— import token **精确集合**（实测，见 README §88.3）。
ALLOWED_IMPORT_TOKENS = frozenset(
    {
        "__future__",
        "annotations",
        "dataclasses",
        "dataclass",
        "contracts",
        "recovery",
        "recovery_context",
        "validation_wiring",
    }
)

#: LRW-1 —— 禁用清单（每条都有冻结依据，见 §87.4）。
FORBIDDEN_IMPORTS = (
    "adapters",
    "agent_manager",
    "scheduler",
    "execution",
    "execution_results",
    "result_handoff",
    "validation",
    "validation_judgment",
    "reusable_results",
    "cost",
    "policy",
    "router",
    "entrypoint",
    "capabilities",
    "project_context",
    "session",
    "context_builder",
)

#: LRW-8 / LRW-9 / LRW-10 —— 不得出现的符号。
FORBIDDEN_SYMBOLS = (
    "BranchState",
    "RoutingRun",
    "AgentManager",
    "AgentStartRequest",
    "commit_started",
    "record_outcome",
    "apply_runtime_control",
    "control_flow_converged",
    "is_converged",
    "no_pending_approval",
    "no_pending_recovery",
    "CapabilityMismatch",
    "AdapterFailure",
    "AdapterFailureKind",
    "AdapterStartReceipt",
)

#: LRW-10 —— duck-typed 只准读的三个属性名（字面量实参）。
ALLOWED_DUCK_FIELDS = frozenset({"failure_kind", "message", "timestamp"})

#: LRW-11 —— 模块级函数清单（实测）。
EXPECTED_FUNCTIONS = (
    "_field",
    "_kind_of",
    "_text_field",
    "_route",
    "route_failed_branch",
    "route_adapter_failure",
)


# --------------------------------------------------------------------------
# 测试替身
# --------------------------------------------------------------------------


class FakeKind:
    """duck-typed 的 ``failure_kind``（带 ``.value``，模拟 A14.4 的枚举成员）。"""

    def __init__(self, value: str) -> None:
        self.value = value


class FakeFailure:
    """duck-typed 的失败事实：**只有**三个属性（不 import `adapters`）。"""

    def __init__(self, kind: object, message: str = "平台失败", timestamp: str = "T") -> None:
        self.failure_kind = kind
        self.message = message
        self.timestamp = timestamp


def make_branch_return(*, branch_id: str, attempt: int = 1) -> BranchReturn:
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


def failed_record(*, branch_id: str) -> ValidationRecord:
    return ValidationRecord(
        branch_id=branch_id,
        passed=False,
        evidence=("pytest 未通过",),
        failed_criteria=("2) 测试通过",),
        detail="一条未满足",
    )


def passed_record(*, branch_id: str) -> ValidationRecord:
    return ValidationRecord(
        branch_id=branch_id,
        passed=True,
        evidence=("e",),
        failed_criteria=(),
        detail="全满足",
    )


def failed_outcome(*, attempt: int = 1) -> BranchValidationOutcome:
    branch_id = new_branch_id()
    return BranchValidationOutcome(
        branch_return=make_branch_return(branch_id=branch_id, attempt=attempt),
        validation=failed_record(branch_id=branch_id),
    )


def passed_outcome() -> BranchValidationOutcome:
    branch_id = new_branch_id()
    return BranchValidationOutcome(
        branch_return=make_branch_return(branch_id=branch_id),
        validation=passed_record(branch_id=branch_id),
    )


# ==========================================================================
# 结构锁 LRW-1 – LRW-11
# ==========================================================================


class TestLRW1ImportBoundary:
    def test_exact_token_set(self) -> None:
        tokens = set(imported_module_tokens(WIRING_SOURCE))
        assert tokens == set(ALLOWED_IMPORT_TOKENS), (
            f"新增：{sorted(tokens - set(ALLOWED_IMPORT_TOKENS))}；"
            f"消失：{sorted(set(ALLOWED_IMPORT_TOKENS) - tokens)}"
        )

    def test_no_adapters_import(self) -> None:
        # Core 不得 import adapters（L11-1 / L11-2 / L12-2）
        assert "adapters" not in set(imported_module_tokens(WIRING_SOURCE))
        assert forbidden_import_hits(WIRING_SOURCE, FORBIDDEN_IMPORTS) == set()

    @pytest.mark.parametrize(
        "needle",
        [
            "\nfrom .adapters import AdapterFailure  # noqa\n",
            "\nfrom .agent_manager import AgentManager  # noqa\n",
            "\nfrom .execution_results import build_branch_return  # noqa\n",
            "\nfrom .validation import Validator  # noqa\n",
            "\nfrom . import cost  # noqa\n",
        ],
    )
    def test_probe_forbidden_import_goes_red(self, needle: str) -> None:
        assert (
            forbidden_import_hits(WIRING_SOURCE + needle, FORBIDDEN_IMPORTS) != set()
        )


class TestLRW2EntryShapes:
    def test_route_failed_branch_signature(self) -> None:
        assert function_signature_shape(WIRING_SOURCE, "route_failed_branch") == {
            "positional": [],
            "kwonly": ["outcome", "total_attempt_limit"],
            "defaults": [False, False],
            "vararg": None,
            "kwarg": None,
        }

    def test_route_adapter_failure_signature(self) -> None:
        assert function_signature_shape(WIRING_SOURCE, "route_adapter_failure") == {
            "positional": [],
            "kwonly": ["branch_id", "attempt", "total_attempt_limit", "failure"],
            "defaults": [False, False, False, False],
            "vararg": None,
            "kwarg": None,
        }

    @pytest.mark.parametrize(
        "func", ["route_failed_branch", "route_adapter_failure"]
    )
    def test_total_attempt_limit_has_no_default(self, func: str) -> None:
        shape = function_signature_shape(WIRING_SOURCE, func)
        assert all(default is False for default in shape["defaults"])  # type: ignore[union-attr]
        assert "total_attempt_limit" in shape["kwonly"]  # type: ignore[operator]

    def test_probe_default_limit_goes_red(self) -> None:
        mutated = WIRING_SOURCE.replace(
            "    total_attempt_limit: int,\n) -> RecoveryOutcome:\n    \"\"\"**FAIL 侧入口**",
            "    total_attempt_limit: int = 2,\n) -> RecoveryOutcome:\n    \"\"\"**FAIL 侧入口**",
        )
        assert mutated != WIRING_SOURCE
        shape = function_signature_shape(mutated, "route_failed_branch")
        assert shape["defaults"] != [False, False]


class TestLRW3SingleDecisionCall:
    def test_decide_recovery_called_once(self) -> None:
        # 两条入口共用 `_route` ⇒ 决策**只有一个调用点**（"第二条旁路"结构上不存在）
        assert len(calls_to(WIRING_SOURCE, ("decide_recovery",))) == 1
        assert len(calls_to(WIRING_SOURCE, ("_route",))) == 2  # 两条入口各一次

    def test_probe_second_decision_call_goes_red(self) -> None:
        mutated = WIRING_SOURCE.replace(
            "    return _route(context)",
            "    decide_recovery(context=context)\n    return _route(context)",
            1,
        )
        assert mutated != WIRING_SOURCE
        assert len(calls_to(mutated, ("decide_recovery",))) == 2


class TestLRW4NoSelfMadeDecision:
    def test_no_recovery_decision_construction(self) -> None:
        assert calls_to(WIRING_SOURCE, ("RecoveryDecision",)) == []

    def test_probe_self_made_decision_goes_red(self) -> None:
        mutated = WIRING_SOURCE.replace(
            "    return _route(context)",
            "    RecoveryDecision  # noqa\n    return _route(context)",
            1,
        )
        assert mutated != WIRING_SOURCE
        assert "RecoveryDecision" in set(referenced_names(mutated))


class TestLRW5ContextConstruction:
    def test_two_construction_sites_one_per_entry(self) -> None:
        assert len(calls_to(WIRING_SOURCE, ("RecoveryContext",))) == 2

    def test_attempt_argument_is_actually_used(self) -> None:
        """`attempt` 参数必须**真的进入上下文**（防"签名有、实现忽略"）。

        ⚠️ 这条是本轮 probe 发现的**锁缺口**补强（并入 LRW-5，**未新增编号**）：
        mutation #2（把 attempt 写死成常量 1）原先只被一条**行为测试**抓到，
        结构锁完全看不见 —— 现在两处构造的 `attempt=` 实参形态被逐字锁定。
        """
        assert function_call_keyword_values(
            WIRING_SOURCE, "RecoveryContext", "attempt"
        ) == ("outcome.branch_return.attempt", "attempt")

    def test_probe_hardcoded_attempt_goes_red(self) -> None:
        mutated = WIRING_SOURCE.replace("        attempt=attempt,", "        attempt=1,")
        assert mutated != WIRING_SOURCE
        assert function_call_keyword_values(
            mutated, "RecoveryContext", "attempt"
        ) != ("outcome.branch_return.attempt", "attempt")

    def test_probe_third_context_site_goes_red(self) -> None:
        mutated = WIRING_SOURCE + (
            "\n\ndef _extra(branch_id: str, limit: int) -> RecoveryContext:\n"
            "    return RecoveryContext(\n"
            "        branch_id=branch_id,\n"
            "        attempt=1,\n"
            "        total_attempt_limit=limit,\n"
            "        validation=None,\n"
            '        failure_kind="crash",\n'
            '        message="m",\n'
            '        timestamp="t",\n'
            "    )\n"
        )
        assert len(calls_to(mutated, ("RecoveryContext",))) == 3


class TestLRW6WitnessSingleConstructionAndConstantReturn:
    def test_single_construction_site(self) -> None:
        assert len(calls_to(WIRING_SOURCE, ("RecoveryOutcome",))) == 1

    def test_no_other_module_constructs_it(self) -> None:
        offenders = sorted(
            str(path.relative_to(SRC))
            for path in SRC.rglob("*.py")
            if path.name != "recovery_wiring.py"
            and calls_to(path.read_text(encoding="utf-8"), ("RecoveryOutcome",))
        )
        assert offenders == []

    @pytest.mark.parametrize("func", ["route_failed_branch", "route_adapter_failure"])
    def test_constant_return_in_both_entries(self, func: str) -> None:
        # 两条入口都**只**返回 `_route(context)`（不得返回裸决策 / None / 元组）
        assert function_return_shapes(WIRING_SOURCE, func) == ("_route(context)",)
        assert function_return_annotation(WIRING_SOURCE, func) == "RecoveryOutcome"

    def test_probe_bare_decision_return_goes_red(self) -> None:
        mutated = WIRING_SOURCE.replace(
            "    return _route(context)\n\n\ndef route_adapter_failure(",
            "    return _route(context).decision\n\n\ndef route_adapter_failure(",
        )
        assert mutated != WIRING_SOURCE
        assert function_return_shapes(mutated, "route_failed_branch") != (
            "_route(context)",
        )


class TestLRW7WitnessShape:
    def test_fields(self) -> None:
        assert dataclass_field_names(WIRING_SOURCE, "RecoveryOutcome") == (
            "context",
            "decision",
        )

    def test_derived_properties_are_not_fields(self) -> None:
        assert property_names(WIRING_SOURCE, "RecoveryOutcome") == (
            "branch_id",
            "attempt",
            "should_recover",
        )
        for name in ("branch_id", "attempt", "should_recover"):
            assert name not in dataclass_field_names(WIRING_SOURCE, "RecoveryOutcome")

    def test_constructor_invariants(self) -> None:
        assert raise_guards(
            WIRING_SOURCE, "RecoveryOutcome", "__post_init__"
        ) == (
            ("not isinstance(self.context, RecoveryContext)", "TypeError"),
            (
                "self.decision is not None and"
                " (not isinstance(self.decision, RecoveryDecision))",
                "TypeError",
            ),
        )

    def test_probe_extra_field_goes_red(self) -> None:
        mutated = WIRING_SOURCE.replace(
            "    context: RecoveryContext\n    decision: RecoveryDecision | None\n",
            "    context: RecoveryContext\n    decision: RecoveryDecision | None\n"
            "    note: str\n",
        )
        assert mutated != WIRING_SOURCE
        assert dataclass_field_names(mutated, "RecoveryOutcome") != (
            "context",
            "decision",
        )


class TestLRW8NoStateWrite:
    def test_no_runtime_symbols(self) -> None:
        names = set(referenced_names(WIRING_SOURCE))
        assert names & set(FORBIDDEN_SYMBOLS) == set()


class TestLRW9NoConvergenceGuess:
    @pytest.mark.parametrize(
        "name",
        ["control_flow_converged", "is_converged", "no_pending_approval", "no_pending_recovery"],
    )
    def test_convergence_names_absent(self, name: str) -> None:
        assert name not in set(referenced_names(WIRING_SOURCE))


class TestLRW10AdapterIsolation:
    def test_no_adapter_type_names(self) -> None:
        names = set(referenced_names(WIRING_SOURCE))
        assert names & {
            "CapabilityMismatch",
            "AdapterFailure",
            "AdapterFailureKind",
            "AdapterStartReceipt",
        } == set()

    def test_only_three_duck_typed_fields_are_read(self) -> None:
        literal_args = set(literal_call_arguments(WIRING_SOURCE, "_field")) | set(
            literal_call_arguments(WIRING_SOURCE, "_text_field")
        )
        assert literal_args == set(ALLOWED_DUCK_FIELDS)

    def test_no_literal_attribute_access_on_failure(self) -> None:
        # duck-typed 访问一律经 `_field(...)`/`getattr`；源码里不得出现 `failure.<字面属性>`
        forbidden = [
            chain
            for chain in attribute_chain_reads(WIRING_SOURCE)
            if chain.startswith("failure.")
        ]
        assert forbidden == []

    def test_probe_fourth_duck_field_goes_red(self) -> None:
        mutated = WIRING_SOURCE.replace(
            '        message=_text_field(failure, "message"),',
            '        message=_text_field(failure, "message"),\n'
            '        extra=_text_field(failure, "handle"),  # noqa',
        )
        assert mutated != WIRING_SOURCE
        literal_args = set(literal_call_arguments(mutated, "_field")) | set(
            literal_call_arguments(mutated, "_text_field")
        )
        assert literal_args != set(ALLOWED_DUCK_FIELDS)


class TestLRW11StatelessNoIO:
    def test_module_surface(self) -> None:
        assert declared_public_names(WIRING_SOURCE) == (
            "RecoveryOutcome",
            "route_adapter_failure",
            "route_failed_branch",
        )
        assert module_level_function_names(WIRING_SOURCE) == EXPECTED_FUNCTIONS
        assert class_def_names(WIRING_SOURCE) == ("RecoveryOutcome",)

    def test_dunder_all_matches_definitions(self) -> None:
        declared = set(declared_public_names(WIRING_SOURCE))
        defined = (
            set(module_level_function_names(WIRING_SOURCE))
            | set(class_def_names(WIRING_SOURCE))
        )
        public_defined = {name for name in defined if not name.startswith("_")}
        assert declared <= defined, f"陈旧导出：{sorted(declared - defined)}"
        assert public_defined <= declared, f"漏导出：{sorted(public_defined - declared)}"

    def test_no_state_no_io_no_clock_and_no_exception_handling(self) -> None:
        assert module_level_container_assignments(WIRING_SOURCE) == ()
        assert clock_call_hits(WIRING_SOURCE) == []
        assert open_calls(WIRING_SOURCE) == []
        assert mapping_get_calls(WIRING_SOURCE) == []
        assert handled_exception_names(WIRING_SOURCE) == ()
        assert set(referenced_names(WIRING_SOURCE)) & {
            "lru_cache",
            "cache",
            "setdefault",
            "global",
        } == set()

    def test_no_classification_rules_in_the_wiring(self) -> None:
        # TEST-RECOVERY-001 的接线侧：映射规则**只在** [14]
        assert set(referenced_names(WIRING_SOURCE)) & {
            "FAILURE_KIND_TO_CLASS",
            "FAILURE_CLASS_TO_TARGET",
            "V1_REACHABLE",
            "FAILURE_CLASS_TO_CORRECTION",
            "CARRY_FORWARD_TEXT",
        } == set()
        assert calls_to(WIRING_SOURCE, ("FailureClass",)) == []
        assert calls_to(WIRING_SOURCE, ("RecoveryTarget",)) == []


# ==========================================================================
# 行为测试 B1 – B8
# ==========================================================================


class TestB1FailedBranchPath:
    def test_fail_produces_witness_and_decision(self) -> None:
        outcome = failed_outcome(attempt=1)
        result = route_failed_branch(outcome=outcome, total_attempt_limit=2)
        assert isinstance(result, RecoveryOutcome)
        assert result.decision is not None
        assert result.decision.failure_class is FailureClass.COMPLETION_CRITERIA
        assert result.decision.route_to is RecoveryTarget.PLANNER
        # 身份来自**同一次配对事件**
        assert result.branch_id == outcome.branch_return.branch_id
        assert result.attempt == outcome.branch_return.attempt
        assert result.should_recover is True

    def test_attempt_comes_from_the_witness_not_a_parameter(self) -> None:
        result = route_failed_branch(outcome=failed_outcome(attempt=2), total_attempt_limit=2)
        assert result.attempt == 2
        assert result.decision is None  # 到上限（见 B4）


class TestB2PassIsRejected:
    def test_pass_record_is_refused_at_construction(self) -> None:
        """PASS 不进 Recovery —— 由 `RecoveryContext` 的构造期守卫**显式抛出**（不是静默 None）。"""
        with pytest.raises(ValueError) as excinfo:
            route_failed_branch(outcome=passed_outcome(), total_attempt_limit=2)
        assert "FAIL" in str(excinfo.value)

    def test_wrong_type_is_refused(self) -> None:
        with pytest.raises(TypeError):
            route_failed_branch(outcome="not-an-outcome", total_attempt_limit=2)  # type: ignore[arg-type]


class TestB3AdapterKindsAgreeWithNode:
    @pytest.mark.parametrize(
        ("kind", "expected_class", "expected_target"),
        [
            ("crash", FailureClass.EXTERNAL_SERVICE, RecoveryTarget.CAPABILITY),
            ("timeout", FailureClass.MODEL, RecoveryTarget.MODEL_ROUTER),
            ("refused", FailureClass.PERMISSION, RecoveryTarget.PLANNER),
            ("network", FailureClass.EXTERNAL_SERVICE, RecoveryTarget.CAPABILITY),
        ],
    )
    def test_four_kinds(
        self, kind: str, expected_class: FailureClass, expected_target: RecoveryTarget
    ) -> None:
        branch_id = new_branch_id()
        result = route_adapter_failure(
            branch_id=branch_id,
            attempt=1,
            total_attempt_limit=2,
            failure=FakeFailure(FakeKind(kind)),
        )
        assert result.decision is not None
        assert result.decision.failure_class is expected_class
        assert result.decision.route_to is expected_target
        assert result.branch_id == branch_id


class TestB4LimitKeepsIdentity:
    def test_at_limit_has_no_decision_but_keeps_identity(self) -> None:
        from adaptive_router.recovery_context import RecoveryContext as Ctx

        branch_id = new_branch_id()
        result = route_adapter_failure(
            branch_id=branch_id,
            attempt=2,
            total_attempt_limit=2,
            failure=FakeFailure(FakeKind("crash")),
        )
        assert result.decision is None
        assert result.should_recover is False
        # **载体恒定**：身份与上下文仍在
        assert result.branch_id == branch_id
        assert result.attempt == 2
        assert isinstance(result.context, Ctx)
        assert result.context.branch_id == branch_id


class TestB5IdentityIsPreserved:
    def test_witness_pairs_the_same_branch_and_attempt(self) -> None:
        outcome = failed_outcome(attempt=1)
        result = route_failed_branch(outcome=outcome, total_attempt_limit=2)
        assert result.context.validation is outcome.validation
        assert result.context.branch_id == outcome.branch_return.branch_id

    def test_decision_still_carries_no_branch_id(self) -> None:
        # 契约未变：身份只在载体上（这正是载体存在的理由，§86 G2）
        assert "branch_id" not in RecoveryDecision.__dataclass_fields__
        result = route_failed_branch(outcome=failed_outcome(), total_attempt_limit=2)
        assert not hasattr(result.decision, "branch_id")


class TestB6DuckTypedProjection:
    def test_three_fields_are_projected_verbatim(self) -> None:
        result = route_adapter_failure(
            branch_id=new_branch_id(),
            attempt=1,
            total_attempt_limit=2,
            failure=FakeFailure(FakeKind("timeout"), message="预算耗尽", timestamp="T1"),
        )
        assert result.context.failure_kind == "timeout"
        assert result.context.message == "预算耗尽"
        assert result.context.timestamp == "T1"

    @pytest.mark.parametrize(
        "failure",
        [
            FakeFailure("timeout"),  # 裸 str 当作 kind ⇒ 无 .value ⇒ 显式失败
            object(),  # 连 failure_kind 都没有
            FakeFailure(FakeKind("timeout"), message=""),  # 审计字段为空
        ],
    )
    def test_bad_failure_facts_fail_explicitly(self, failure: object) -> None:
        with pytest.raises(TypeError):
            route_adapter_failure(
                branch_id=new_branch_id(),
                attempt=1,
                total_attempt_limit=2,
                failure=failure,
            )

    def test_unknown_kind_fails_explicitly(self) -> None:
        with pytest.raises(ValueError):
            route_adapter_failure(
                branch_id=new_branch_id(),
                attempt=1,
                total_attempt_limit=2,
                failure=FakeFailure(FakeKind("weird")),
            )


class TestB7DeterministicAndPure:
    def test_same_input_same_outcome(self) -> None:
        outcome = failed_outcome()
        first = route_failed_branch(outcome=outcome, total_attempt_limit=2)
        second = route_failed_branch(outcome=outcome, total_attempt_limit=2)
        assert first == second

    def test_inputs_are_untouched(self) -> None:
        outcome = failed_outcome()
        failure = FakeFailure(FakeKind("crash"))
        before = (outcome.branch_return.branch_id, failure.message, failure.timestamp)
        route_failed_branch(outcome=outcome, total_attempt_limit=2)
        route_adapter_failure(
            branch_id=new_branch_id(),
            attempt=1,
            total_attempt_limit=2,
            failure=failure,
        )
        assert (outcome.branch_return.branch_id, failure.message, failure.timestamp) == before


class TestB8DecisionIsTheNodeObject:
    def test_decision_equals_the_node_output(self) -> None:
        """载体里的 decision **就是** `[14]` 的产出（不是接线自己算的）。

        ⚠️ **为什么用 `==` 而不是 `is`**：`decide_recovery` 是**纯函数**，每次调用都返回
        一个**新的相等对象** ⇒ 在测试里"再调一次拿同一个对象"用 `is` 断言**不可观测**
        （这是 §87.6 锁定 3 的措辞在测试层面的修正）。
        "**不重建**"由**结构**保证、并且是可观测的：本模块 `RecoveryDecision(` 构造点 = **0**
        （LRW-4）、`decide_recovery(` 调用点 = **1**（LRW-3，唯一决策来源）。
        """
        outcome = failed_outcome()
        result = route_failed_branch(outcome=outcome, total_attempt_limit=2)
        assert result.decision == decide_recovery(context=result.context)

    def test_no_reconstruction_sites(self) -> None:
        assert calls_to(WIRING_SOURCE, ("RecoveryDecision",)) == []
        assert len(calls_to(WIRING_SOURCE, ("decide_recovery",))) == 1

    def test_context_is_the_same_object_used_for_the_decision(self) -> None:
        result = route_failed_branch(outcome=failed_outcome(), total_attempt_limit=2)
        assert result.decision.attempt == result.context.attempt
        assert result.decision.total_attempt_limit == result.context.total_attempt_limit
