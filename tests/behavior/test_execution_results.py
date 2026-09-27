"""``[12] Execution Results`` —— 结构锁 LR-1 – LR-12 + 行为测试 B1 – B16。

架构依据：`docs/architecture.md` 2 节 ``[12] Execution Results``（**Amendment A15**，FROZEN）。
范围、锁清单与 probe 计划：`tests/behavior/README_step5b.md` §62。

⚠️ 敏感性 probe 一律在**源码字符串**上完成，**不覆写** ``src/`` 里的真实文件。

⚠️ **禁止子串扫描**：本模块的 docstring 里**故意提及** `RoutingPlan` / `AdapterFailure` /
`Valida tion` 等词（说明边界）⇒ 相关锁一律 **AST 级**。
"""

from __future__ import annotations

import pathlib

import pytest

from adaptive_router.contracts import (
    BRANCH_RETURN_FIELDS,
    BranchReturn,
    Confidence,
    ConfirmationSource,
    ContractError,
    EffortLevel,
    ModelTier,
    new_agent_id,
    new_branch_id,
)
from adaptive_router.execution_results import build_branch_return, build_execution_value
from adaptive_router.result_handoff import (
    ActualFacts,
    PlannedFacts,
    ProviderResultPayload,
    RequestedFacts,
    ResultHandoff,
)

from _results_locks import (
    attribute_reads,
    branch_return_keywords,
    calls_to,
    class_def_names,
    clock_call_hits,
    exception_subclass_names,
    forbidden_import_hits,
    function_call_keyword_values,
    function_signature_shape,
    handled_exception_names,
    imported_module_tokens,
    mapping_get_calls,
    module_level_container_assignments,
    open_calls,
    referenced_names,
    resolved_symbols,
    self_read_count,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
SRC = REPO_ROOT / "src/adaptive_router"
RESULTS_SOURCE = (SRC / "execution_results.py").read_text(encoding="utf-8")

#: LR-1 —— `[12]` 的 import token **精确集合**（实测，见 README §63.3）。
ALLOWED_IMPORT_TOKENS = frozenset(
    {
        "__future__",
        "annotations",
        "typing",
        "TypeVar",
        "contracts",
        "result_handoff",
    }
)

#: LR-1 —— 禁用清单（决策 / 执行 / 成本 / 适配层一律不得进入 `[12]`）。
FORBIDDEN_IMPORTS = (
    "scheduler",
    "agent_manager",
    "planner",
    "model_router",
    "policy",
    "cost",
    "recovery",
    "validation",
    "approval_judgment",
    "approval_disclosure",
    "execution",
    "adapters",
    "risk",
    "capabilities",
    "project_context",
)

#: LR-3 —— `[12]` **不得产出**的类型。
FORBIDDEN_OUTPUT_TYPES = (
    "ValidationRecord",
    "RecoveryDecision",
    "RetryDecision",
    "CostSummary",
    "ApprovalJudgment",
    "RoutingPlan",
)

#: LR-6 —— 失败 / 能力事实的符号名。
FORBIDDEN_FAILURE_SYMBOLS = ("AdapterFailure", "CapabilityMismatch", "AdapterError")

#: LR-11 —— 运行态读取名（`[12]` 不反查运行上下文）。
FORBIDDEN_RUNTIME_READS = (
    "pending_admission_branch_ids",
    "routing_run",
    "branch_by_id",
    "active_subagent_count",
    "branch_terminals",
)


# --------------------------------------------------------------------------
# 测试替身
# --------------------------------------------------------------------------


def make_handoff(
    *,
    observed: bool = True,
    tier: ModelTier = ModelTier.BALANCED,
    planned_effort: EffortLevel = EffortLevel.MEDIUM,
    requested_model: str = "gpt-5-codex",
    requested_effort: EffortLevel = EffortLevel.MEDIUM,
    actual_model: str | None = "gpt-5-codex-2025-xx",
    actual_effort: EffortLevel | None = EffortLevel.HIGH,
    planned_source: ConfirmationSource = ConfirmationSource.PLANNER,
    requested_source: ConfirmationSource = ConfirmationSource.REQUEST,
    actual_source: ConfirmationSource = ConfirmationSource.PROVIDER_API,
    confidence: Confidence = Confidence.HIGH,
) -> ResultHandoff:
    if not observed:
        actual_model, actual_effort = None, None
        actual_source = ConfirmationSource.UNAVAILABLE
    return ResultHandoff(
        branch_id=new_branch_id(),
        agent_id=new_agent_id(),
        attempt=1,
        planned_fact=PlannedFacts(
            tier=tier, effort=planned_effort, source=planned_source
        ),
        requested_fact=RequestedFacts(
            model_id=requested_model, effort=requested_effort, source=requested_source
        ),
        actual_fact=ActualFacts(
            model_id=actual_model,
            effort=actual_effort,
            source=actual_source,
            confidence=confidence,
            observed=observed,
        ),
        provider_payload=ProviderResultPayload(
            provider_status="ok",
            conclusion_summary="done",
            key_evidence=("e1",),
            residual_risks=(),
            main_agent_todo=("t1",),
        ),
    )


# ==========================================================================
# 结构锁 LR-1 – LR-12
# ==========================================================================


class TestLR1ImportBoundary:
    def test_exact_token_set(self) -> None:
        tokens = set(imported_module_tokens(RESULTS_SOURCE))
        assert tokens == set(ALLOWED_IMPORT_TOKENS), (
            f"新增：{tokens - set(ALLOWED_IMPORT_TOKENS)}；"
            f"消失：{set(ALLOWED_IMPORT_TOKENS) - tokens}"
        )

    def test_forbidden_imports_empty(self) -> None:
        assert forbidden_import_hits(RESULTS_SOURCE, FORBIDDEN_IMPORTS) == set()

    @pytest.mark.parametrize(
        "needle",
        [
            "\nfrom .scheduler import Scheduler  # noqa\n",
            "\nfrom .agent_manager import AgentManager  # noqa\n",
            "\nfrom . import adapters  # noqa\n",
            "\nfrom . import cost  # noqa\n",
            "\nfrom . import execution  # noqa\n",
        ],
    )
    def test_probe_forbidden_import_goes_red(self, needle: str) -> None:
        assert forbidden_import_hits(RESULTS_SOURCE + needle, FORBIDDEN_IMPORTS) != set()


class TestLR2SingleInput:
    def test_build_branch_return_takes_only_handoff(self) -> None:
        assert function_signature_shape(RESULTS_SOURCE, "build_branch_return") == {
            "positional": [],
            "kwonly": ["handoff"],
            "defaults": [False],
            "vararg": None,
            "kwarg": None,
        }

    def test_no_context_injection_parameters_anywhere(self) -> None:
        """`[12]` **不接受** scheduler / agent_manager / routing_plan 之类的注入。"""
        for needle in ("scheduler=", "agent_manager=", "routing_plan=", "routing="):
            assert needle not in RESULTS_SOURCE, needle

    def test_probe_extra_parameter_goes_red(self) -> None:
        mutated = RESULTS_SOURCE.replace(
            "def build_branch_return(*, handoff: ResultHandoff) -> BranchReturn:",
            "def build_branch_return(*, handoff: ResultHandoff, scheduler=None) -> BranchReturn:",
        )
        assert mutated != RESULTS_SOURCE, "探针失效：替换未命中"
        assert function_signature_shape(mutated, "build_branch_return") != (
            function_signature_shape(RESULTS_SOURCE, "build_branch_return")
        )


class TestLR3OutputBoundary:
    @pytest.mark.parametrize("symbol", FORBIDDEN_OUTPUT_TYPES)
    def test_forbidden_output_type_is_never_constructed(self, symbol: str) -> None:
        assert calls_to(RESULTS_SOURCE, (symbol,)) == []

    @pytest.mark.parametrize("symbol", FORBIDDEN_OUTPUT_TYPES)
    def test_probe_goes_red(self, symbol: str) -> None:
        mutated = RESULTS_SOURCE + f"\n\n\ndef _sneak(**kw):\n    return {symbol}(**kw)\n"
        assert calls_to(mutated, (symbol,)) != []


class TestLR4ProvenanceNoWholeObjects:
    @pytest.mark.parametrize("symbol", ["RoutingPlan", "Plan", "ModelSelection"])
    def test_symbol_never_appears(self, symbol: str) -> None:
        """A15.5：**整体对象不得进入**（AST 级；docstring 提及不算）。

        ⚠️ 用 `referenced_names`（含裸 `Name`）而**不是** `resolved_symbols` ——
        后者看不见 `Symbol(**kw)` 这种未 import 的直接调用（本轮的真实教训）。
        """
        assert symbol not in referenced_names(RESULTS_SOURCE), symbol

    @pytest.mark.parametrize("symbol", ["RoutingPlan", "Plan", "ModelSelection"])
    def test_construction_sites_are_zero(self, symbol: str) -> None:
        assert calls_to(RESULTS_SOURCE, (symbol,)) == []

    @pytest.mark.parametrize("symbol", ["RoutingPlan", "Plan", "ModelSelection"])
    def test_probe_goes_red(self, symbol: str) -> None:
        mutated = RESULTS_SOURCE + f"\n\n\ndef _sneak(**kw):\n    return {symbol}(**kw)\n"
        assert symbol in referenced_names(mutated)
        assert calls_to(mutated, (symbol,)) != []

    def test_docstring_mention_would_false_positive(self) -> None:
        """⚠️ 证明 LR-4 **不能**写成子串扫描：docstring 里就有这些词。"""
        assert "RoutingPlan" in RESULTS_SOURCE  # 出现于 docstring 的边界说明
        assert "RoutingPlan" not in referenced_names(RESULTS_SOURCE)
        assert "RoutingPlan" not in resolved_symbols(RESULTS_SOURCE)


class TestLR5Stateless:
    def test_no_class_definitions(self) -> None:
        assert class_def_names(RESULTS_SOURCE) == ()

    def test_no_self_reads(self) -> None:
        assert self_read_count(RESULTS_SOURCE) == 0

    def test_no_module_level_mutable_containers(self) -> None:
        assert module_level_container_assignments(RESULTS_SOURCE) == ()

    def test_probe_class_goes_red(self) -> None:
        mutated = RESULTS_SOURCE + "\n\n\nclass ExecutionResultsBuilder:\n    pass\n"
        assert class_def_names(mutated) != ()

    def test_probe_self_read_goes_red(self) -> None:
        mutated = RESULTS_SOURCE + "\n\n\ndef _sneak(self):\n    return self.cache\n"
        assert self_read_count(mutated) == 1

    def test_probe_container_goes_red(self) -> None:
        mutated = RESULTS_SOURCE + "\n\n_CACHE = {}\n"
        assert module_level_container_assignments(mutated) == ("_CACHE",)


class TestLR6FailureIsolation:
    @pytest.mark.parametrize("symbol", FORBIDDEN_FAILURE_SYMBOLS)
    def test_symbol_never_appears(self, symbol: str) -> None:
        assert symbol not in referenced_names(RESULTS_SOURCE), symbol

    def test_branch_return_has_no_error_keyword(self) -> None:
        keywords = set(branch_return_keywords(RESULTS_SOURCE))
        for forbidden in ("error", "failure", "retry", "retry_after", "degraded"):
            assert forbidden not in keywords, forbidden

    def test_probe_error_keyword_goes_red(self) -> None:
        mutated = RESULTS_SOURCE.replace(
            "        attempt=handoff.attempt,",
            "        attempt=handoff.attempt,\n        error=None,",
        )
        assert mutated != RESULTS_SOURCE, "探针失效：替换未命中"
        assert "error" in set(branch_return_keywords(mutated))


class TestLR7TenFieldWhitelist:
    def test_keywords_equal_the_frozen_whitelist(self) -> None:
        assert set(branch_return_keywords(RESULTS_SOURCE)) == set(BRANCH_RETURN_FIELDS)

    def test_keyword_count(self) -> None:
        assert len(branch_return_keywords(RESULTS_SOURCE)) == 10

    def test_probe_extra_keyword_goes_red(self) -> None:
        mutated = RESULTS_SOURCE.replace(
            "        attempt=handoff.attempt,",
            "        attempt=handoff.attempt,\n        extra='x',",
        )
        assert mutated != RESULTS_SOURCE
        assert set(branch_return_keywords(mutated)) != set(BRANCH_RETURN_FIELDS)

    def test_probe_kwargs_splat_goes_red(self) -> None:
        """用 ``**fields`` 绕过白名单必须显形。"""
        mutated = RESULTS_SOURCE + "\n\n\ndef _sneak(fields):\n    return BranchReturn(**fields)\n"
        assert "**" in branch_return_keywords(mutated)


class TestLR8ExecutionValueChokepoint:
    def test_single_construction_site(self) -> None:
        """由裁定 ④（`build_execution_value()`）决定：构造点是**一处**。"""
        assert len(calls_to(RESULTS_SOURCE, ("ExecutionValue",))) == 1

    def test_called_exactly_twice_for_two_dimensions(self) -> None:
        assert len(calls_to(RESULTS_SOURCE, ("build_execution_value",))) == 2

    def test_probe_second_construction_goes_red(self) -> None:
        mutated = RESULTS_SOURCE + "\n\n\ndef _sneak(**kw):\n    return ExecutionValue(**kw)\n"
        assert len(calls_to(mutated, ("ExecutionValue",))) == 2

    def test_actual_is_never_taken_from_planned(self) -> None:
        """INV-04 的机械形式：`actual=` 的实参**必须**逐一来自 `handoff.actual_fact.*`。"""
        assert function_call_keyword_values(
            RESULTS_SOURCE, "build_execution_value", "actual"
        ) == ("handoff.actual_fact.model_id", "handoff.actual_fact.effort")
        assert function_call_keyword_values(
            RESULTS_SOURCE, "build_execution_value", "actual_source"
        ) == ("handoff.actual_fact.source", "handoff.actual_fact.source")
        assert function_call_keyword_values(
            RESULTS_SOURCE, "build_execution_value", "observed"
        ) == ("handoff.actual_fact.observed", "handoff.actual_fact.observed")

    def test_probe_planned_substitution_goes_red(self) -> None:
        """**变异靶子**（§62.11 ⑥）：让 actual 取自 planned ⇒ 本锁必须变红。"""
        mutated = RESULTS_SOURCE.replace(
            "        actual=handoff.actual_fact.model_id,",
            "        actual=handoff.planned_fact.tier,",
        )
        assert mutated != RESULTS_SOURCE, "探针失效：替换未命中"
        assert function_call_keyword_values(
            mutated, "build_execution_value", "actual"
        ) != ("handoff.actual_fact.model_id", "handoff.actual_fact.effort")


class TestLR9NoClockNoIONoDefaults:
    def test_absent(self) -> None:
        assert clock_call_hits(RESULTS_SOURCE) == []
        assert open_calls(RESULTS_SOURCE) == []
        assert mapping_get_calls(RESULTS_SOURCE) == []


class TestLR10NoNewExceptionType:
    def test_no_exception_subclass(self) -> None:
        assert exception_subclass_names(RESULTS_SOURCE) == ()

    def test_no_except_clause(self) -> None:
        assert handled_exception_names(RESULTS_SOURCE) == ()

    def test_probe_goes_red(self) -> None:
        mutated = RESULTS_SOURCE + "\n\n\nclass ResultsError(RuntimeError):\n    pass\n"
        assert exception_subclass_names(mutated) == ("ResultsError",)


class TestLR11NoRuntimeContextReads:
    @pytest.mark.parametrize("name", FORBIDDEN_RUNTIME_READS)
    def test_name_is_never_read(self, name: str) -> None:
        assert name not in attribute_reads(RESULTS_SOURCE), name

    @pytest.mark.parametrize("name", FORBIDDEN_RUNTIME_READS)
    def test_probe_goes_red(self, name: str) -> None:
        mutated = RESULTS_SOURCE + f"\n\n\ndef _sneak(manager):\n    return manager.{name}\n"
        assert name in attribute_reads(mutated)


class TestLR12SeamShapeIsConsumedNotRedefined:
    def test_results_module_defines_no_dto(self) -> None:
        """裁定 ④：**DTO seam 与 transformer 分离** —— `[12]` 不得再定义 DTO。"""
        assert class_def_names(RESULTS_SOURCE) == ()

    def test_it_imports_the_seam_module(self) -> None:
        assert "result_handoff" in imported_module_tokens(RESULTS_SOURCE)

    def test_build_execution_value_shape(self) -> None:
        assert function_signature_shape(RESULTS_SOURCE, "build_execution_value") == {
            "positional": [],
            "kwonly": [
                "planned",
                "planned_source",
                "requested",
                "requested_source",
                "actual",
                "actual_source",
                "observed",
                "confidence",
            ],
            "defaults": [False] * 8,
            "vararg": None,
            "kwarg": None,
        }


# ==========================================================================
# 行为测试 B1 – B16
# ==========================================================================


class TestB1B2Assembly:
    def test_B1_ten_fields_map_one_to_one(self) -> None:
        handoff = make_handoff()
        result = build_branch_return(handoff=handoff)
        assert tuple(result.__dataclass_fields__) == BRANCH_RETURN_FIELDS
        assert result.branch_id == handoff.branch_id
        assert result.agent_id == handoff.agent_id
        assert result.attempt == handoff.attempt
        assert result.conclusion_summary == handoff.provider_payload.conclusion_summary
        assert result.key_evidence == handoff.provider_payload.key_evidence
        assert result.residual_risks == handoff.provider_payload.residual_risks
        assert result.main_agent_todo == handoff.provider_payload.main_agent_todo

    def test_B2_execution_value_three_values_are_typed(self) -> None:
        result = build_branch_return(handoff=make_handoff())
        assert result.model.planned is ModelTier.BALANCED
        assert result.model.requested == "gpt-5-codex"
        assert result.model.actual == "gpt-5-codex-2025-xx"
        assert result.effort.planned is EffortLevel.MEDIUM
        assert result.effort.requested is EffortLevel.MEDIUM
        assert result.effort.actual is EffortLevel.HIGH


class TestB3B5Provenance:
    def test_B3_sources_are_carried_through(self) -> None:
        """`TEST-EVIDENCE-001`：三个来源分别来自各自的 fact，且 planned/requested 不得 UNAVAILABLE。"""
        result = build_branch_return(
            handoff=make_handoff(
                planned_source=ConfirmationSource.PLANNER,
                requested_source=ConfirmationSource.REQUEST,
                actual_source=ConfirmationSource.PROVIDER_API,
            )
        )
        for value in (result.model, result.effort):
            assert value.planned_source is ConfirmationSource.PLANNER
            assert value.requested_source is ConfirmationSource.REQUEST
            assert value.actual_source is ConfirmationSource.PROVIDER_API
            assert value.planned_source is not ConfirmationSource.UNAVAILABLE
            assert value.requested_source is not ConfirmationSource.UNAVAILABLE

    def test_B4_unobserved_becomes_unavailable(self) -> None:
        """`TEST-EVIDENCE-002`：未观测 ⇒ `actual is None` 且 `UNAVAILABLE`。"""
        result = build_branch_return(handoff=make_handoff(observed=False))
        for value in (result.model, result.effort):
            assert value.observed is False
            assert value.actual is None
            assert value.actual_source is ConfirmationSource.UNAVAILABLE

    def test_B5_planned_is_never_substituted_for_actual(self) -> None:
        """`TEST-EVIDENCE-003`：**最大风险**（事实污染）——计划值不得冒充实际值。

        即使 planned 与实际值**恰好相同**，也必须各自来自各自的 fact。
        """
        handoff = make_handoff(
            observed=False, tier=ModelTier.STRONG, requested_model="gpt-5-codex"
        )
        result = build_branch_return(handoff=handoff)
        assert result.model.planned is ModelTier.STRONG
        assert result.model.requested == "gpt-5-codex"
        assert result.model.actual is None  # **没有**被 planned 顶替
        assert result.model.observed is False

    def test_B5b_same_value_still_comes_from_each_fact(self) -> None:
        handoff = make_handoff(actual_model="gpt-5-codex")  # requested 与 actual 同值
        result = build_branch_return(handoff=handoff)
        assert result.model.requested == "gpt-5-codex"
        assert result.model.actual == "gpt-5-codex"
        assert result.model.requested_source is ConfirmationSource.REQUEST
        assert result.model.actual_source is ConfirmationSource.PROVIDER_API


class TestB6B7L37Structure:
    def test_B6_shape_is_frozen(self) -> None:
        """`TEST-RETURN-001`：只返回规定的 10 项。"""
        BranchReturn.assert_shape_is_frozen()
        result = build_branch_return(handoff=make_handoff())
        assert len(result.__dataclass_fields__) == 10
        assert BranchReturn.allowed_field_names() == BRANCH_RETURN_FIELDS

    def test_B7_no_extra_keys(self) -> None:
        """`TEST-RETURN-002`：不得返回思维过程或重复背景（无额外字段）。"""
        result = build_branch_return(handoff=make_handoff())
        extra = set(result.__dataclass_fields__) - set(BRANCH_RETURN_FIELDS)
        assert extra == set()


class TestB8B16Properties:
    def test_B8_pure_function_is_deterministic(self) -> None:
        """同一 handoff 调用两次 ⇒ 结果**相等**（无隐藏状态）。"""
        handoff = make_handoff()
        assert build_branch_return(handoff=handoff) == build_branch_return(handoff=handoff)

    def test_B9_inconsistent_actual_facts_fail_at_construction(self) -> None:
        with pytest.raises(ValueError, match="observed=False 时 model_id / effort 必须为 None"):
            ActualFacts(
                model_id="m",
                effort=None,
                source=ConfirmationSource.UNAVAILABLE,
                confidence=Confidence.LOW,
                observed=False,
            )

    def test_B10_abstract_tier_as_requested_is_rejected_by_the_contract(self) -> None:
        """"model_id 不得是抽象档位" 这条规则**只有一处实现**（`BranchReturn`）。"""
        handoff = make_handoff(requested_model="balanced")
        with pytest.raises(ContractError, match="不得是抽象档位"):
            build_branch_return(handoff=handoff)

    def test_B11_handoff_contract(self) -> None:
        assert len(make_handoff().__dataclass_fields__) == 7
        with pytest.raises(ValueError):
            ResultHandoff(
                branch_id="nope",
                agent_id=new_agent_id(),
                attempt=1,
                planned_fact=make_handoff().planned_fact,
                requested_fact=make_handoff().requested_fact,
                actual_fact=make_handoff().actual_fact,
                provider_payload=make_handoff().provider_payload,
            )

    def test_B12_provider_status_is_passed_through_not_judged(self) -> None:
        """`[12]` **不解释** provider 状态 —— 它只把它交给冻结的 `validation_state` 字段。"""
        handoff = make_handoff()
        result = build_branch_return(handoff=handoff)
        assert result.validation_state == handoff.provider_payload.provider_status
        assert result.validation_state == "ok"

    def test_B13_no_error_payload_can_enter(self) -> None:
        """错误在**结构上**不可能进入：`ResultHandoff` 没有任何 error/failure 字段。"""
        field_names = set(ResultHandoff.__dataclass_fields__)
        for forbidden in ("error", "failure", "retry", "adapter_failure"):
            assert forbidden not in field_names, forbidden

    def test_B14_empty_evidence_and_risks_are_legal(self) -> None:
        handoff = make_handoff()
        empty = ResultHandoff(
            branch_id=handoff.branch_id,
            agent_id=handoff.agent_id,
            attempt=handoff.attempt,
            planned_fact=handoff.planned_fact,
            requested_fact=handoff.requested_fact,
            actual_fact=handoff.actual_fact,
            provider_payload=ProviderResultPayload(
                provider_status="ok",
                conclusion_summary="done",
                key_evidence=(),
                residual_risks=(),
                main_agent_todo=(),
            ),
        )
        result = build_branch_return(handoff=empty)
        assert result.key_evidence == () and result.residual_risks == ()

    def test_B15_no_cross_talk_between_handoffs(self) -> None:
        first = build_branch_return(handoff=make_handoff(requested_model="model-a"))
        second = build_branch_return(handoff=make_handoff(requested_model="model-b"))
        assert first.model.requested == "model-a"
        assert second.model.requested == "model-b"
        assert first.branch_id != second.branch_id

    def test_B16_execution_pass_report_is_not_consumed(self) -> None:
        """`[12]` **不消费** P1 的 report（A15 / G4：粒度不同，不得转换）。"""
        assert "ExecutionPassReport" not in referenced_names(RESULTS_SOURCE)
        assert "execution" not in imported_module_tokens(RESULTS_SOURCE)

    def test_B17_build_execution_value_is_a_verbatim_passthrough(self) -> None:
        value = build_execution_value(
            planned=ModelTier.STRONG,
            planned_source=ConfirmationSource.PLANNER,
            requested="provider-model",
            requested_source=ConfirmationSource.REQUEST,
            actual=None,
            actual_source=ConfirmationSource.UNAVAILABLE,
            observed=False,
            confidence=Confidence.LOW,
        )
        assert value.planned is ModelTier.STRONG
        assert value.requested == "provider-model"
        assert value.actual is None
        assert value.observed is False
        assert value.confidence is Confidence.LOW
