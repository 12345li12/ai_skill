"""ModelRouter 行为测试（Step 5A ③）。

对应追溯矩阵（docs/architecture.md 第 8 节，Test ID 已冻结）：
  TEST-MODEL-001  刚好够用的最低配置（4 档）  → TestModel001MinimumSufficientTier

  **PARTIAL（不是 DEFERRED）**：
  TEST-MODEL-002  升级触发（失败/冲突/风险上升）—— 同时承载于 **[06] + [14]**
                  · 已实现（[06] 侧）：低置信度、``RiskLevel.HIGH`` 的**初始**升档
                  · 未实现：[14] Recovery 触发的重路由、**执行期**上升
                  见 ``TestUpgradeHooksAndInitialBumps``

  **DEFERRED**：
  TEST-MODEL-003  转为机械明确 → 为后续任务降级 —— 需 [15c] Cost Summary 的真实数据，
                  本批不写没有真实 Cost Summary 的"完整通过"测试。

架构依据（2 节 [06]）：
  输入：Evaluation、RiskAssessment、ProjectContext 的模型偏好
  输出：ModelSelection（**计划档位 = abstract tier**）
  约束：**只选抽象档位/强度**，不得引入平台细节（provider model ID 属 [11]）。
"""

from __future__ import annotations

import ast
import dataclasses
import pathlib

import pytest

from adaptive_router.contracts import (
    BranchSpec,
    CapabilitySnapshot,
    Evaluation,
    ExecutionMode,
    ImpactScope,
    ModelSelection,
    ModelTier,
    EffortLevel,
    Ordinal,
    Plan,
    ProjectContext,
    RiskAssessment,
    RiskCategory,
    RiskLevel,
)
from adaptive_router.model_router import (
    ModelRouter,
    PlatformDetailError,
    TierDecision,
    parse_tier_preference,
)
from adaptive_router.planner import Evaluator, EvidenceStrength, TaskFacts

from _import_detectors import imported_module_tokens, imported_names

FIXED_TIME = "2026-09-17T00:00:00Z"

MODULE_SOURCE = (
    pathlib.Path(__file__).resolve().parents[2]
    / "src/adaptive_router/model_router.py"
).read_text(encoding="utf-8")
_MODULE_TREE = ast.parse(MODULE_SOURCE)


def _docstring_nodes() -> set[int]:
    """所有作为 docstring 的 ``Constant`` 节点的 id（需从字面量断言中排除）。"""
    docstrings: set[int] = set()
    for node in ast.walk(_MODULE_TREE):
        if isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            body = getattr(node, "body", [])
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                docstrings.add(id(body[0].value))
    return docstrings


def _imported_names() -> set[str]:
    return imported_names(MODULE_SOURCE)


def _imported_modules() -> set[str]:
    """被测模块的 import token（保守检测器，见 `_import_detectors`）。"""
    return imported_module_tokens(MODULE_SOURCE)


def _function_node(name: str, source: str = MODULE_SOURCE) -> ast.AST:
    for node in ast.walk(ast.parse(source)):
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == name
        ):
            return node
    raise AssertionError(f"源码里找不到函数 {name}")


def _attribute_reads(name: str, source: str = MODULE_SOURCE) -> set[str]:
    """函数体里被**访问**的属性名（``x.y`` 里的 ``y``），含嵌套作用域。

    ⚠️ 这是**属性访问**级别的证据，不是"行为碰巧没差异"级别的证据
    （A4.7 的字面要求：*不得读取或解释* child 的 goal / read_only）。
    """
    node = _function_node(name, source)
    return {child.attr for child in ast.walk(node) if isinstance(child, ast.Attribute)}


def _attribute_writes(name: str, source: str = MODULE_SOURCE) -> set[str]:
    """函数体里被**写入**的属性名（``x.y = ...`` / ``x.y += ...``）。

    ⚠️ 只有**最外层** ``Attribute.attr`` 才真的被写。例如
    ``plan.branches[0].goal = "x"`` 写的是 ``goal``；
    ``branches`` 出现在 target 的 value 链上，那是**读**，不能算进来。
    """

    def _written_of(target: ast.AST) -> set[str]:
        node = target
        while isinstance(node, (ast.Subscript, ast.Starred)):
            node = node.value
        if isinstance(node, ast.Attribute):
            return {node.attr}
        if isinstance(node, (ast.Tuple, ast.List)):
            found: set[str] = set()
            for element in node.elts:
                found |= _written_of(element)
            return found
        return set()

    node = _function_node(name, source)
    written: set[str] = set()
    for child in ast.walk(node):
        targets: list[ast.AST] = []
        if isinstance(child, ast.Assign):
            targets = list(child.targets)
        elif isinstance(child, (ast.AugAssign, ast.AnnAssign)):
            targets = [child.target]
        for target in targets:
            written |= _written_of(target)
    return written


# --------------------------------------------------------------------------
# 测试替身
# --------------------------------------------------------------------------


def snapshot() -> CapabilitySnapshot:
    return CapabilitySnapshot(
        subagents=True,
        async_background=True,
        concurrency_limit=4,
        available_models=(),
        available_effort_levels=(),
        state_wait_append_stop=True,
        hot_switch=True,
        reliable_cost_data=False,
        probed_dimensions=("subagents",),
        unavailable_reason=None,
        probed_at=FIXED_TIME,
    )


def project(**overrides: object) -> ProjectContext:
    base: dict[str, object] = dict(
        root="/repo",
        present=True,
        boundary_dirs=("src",),
        writable=(),
        denied=(),
        readme_refs=("README.md",),
        agents_md_present=False,
        agents_md_digest=None,
        test_commands=(),
        high_risk_files=(),
        model_preference=None,
        approval_preference=None,
        source_path=None,
        read_at=FIXED_TIME,
    )
    base.update(overrides)
    return ProjectContext(**base)  # type: ignore[arg-type]


def task_facts(**overrides: object) -> TaskFacts:
    base: dict[str, object] = dict(
        affected_modules=1,
        mechanical=True,
        touches_cross_module_contracts=False,
        requires_iterative_debugging=False,
        is_review_task=False,
        subtasks=(),
        shared_context_size=0,
        has_sequential_dependency=False,
        branch_setup_cost=Ordinal.LOW,
        evidence=EvidenceStrength.STRONG,
        local_and_reversible=True,
    )
    base.update(overrides)
    return TaskFacts(**base)  # type: ignore[arg-type]


def risk(level: RiskLevel = RiskLevel.LOW, **overrides: object):
    base: dict[str, object] = dict(
        level=level,
        categories=(),
        detail=f"level={level.value}",
    )
    base.update(overrides)
    return RiskAssessment(**base)  # type: ignore[arg-type]


def select(
    *,
    fact_overrides: dict[str, object] | None = None,
    risk_assessment: RiskAssessment | None = None,
    project_context: ProjectContext | None = None,
) -> ModelSelection:
    fact_set = task_facts(**(fact_overrides or {}))
    cap = snapshot()
    proj = project_context if project_context is not None else project()
    evaluation: Evaluation = Evaluator().evaluate(fact_set, capability=cap, project=proj)
    return ModelRouter().select(
        evaluation,
        risk_assessment if risk_assessment is not None else risk(),
        project=proj,
    )


# ==========================================================================
# TEST-MODEL-001 —— 刚好够用的最低配置
# ==========================================================================


class TestModel001MinimumSufficientTier:
    def test_returns_the_frozen_contract_type(self) -> None:
        assert isinstance(select(), ModelSelection)

    def test_four_tiers_are_defined(self) -> None:
        assert len(ModelTier) == 4
        assert {t.name for t in ModelTier} == {
            "LOW_COST",
            "BALANCED",
            "STRONG",
            "MAXIMUM",
        }

    def test_mechanical_low_risk_gets_the_cheapest_tier(self) -> None:
        """机械明确、低风险 → 低成本 / 低强度。"""
        selection = select(fact_overrides={"mechanical": True, "affected_modules": 1})
        assert selection.tier is ModelTier.LOW_COST
        assert selection.effort is EffortLevel.LOW

    def test_routine_analysis_gets_balanced(self) -> None:
        """常规编码分析 → 平衡型 / 中等。"""
        selection = select(
            fact_overrides={
                "mechanical": False,
                "touches_cross_module_contracts": False,
                "requires_iterative_debugging": False,
                "affected_modules": 1,
            }
        )
        assert selection.tier is ModelTier.BALANCED
        assert selection.effort is EffortLevel.MEDIUM

    def test_cross_module_work_gets_strong(self) -> None:
        """跨模块 → 较强 / 较高。"""
        selection = select(
            fact_overrides={
                "mechanical": False,
                "touches_cross_module_contracts": True,
            }
        )
        assert selection.tier is ModelTier.STRONG
        assert selection.effort is EffortLevel.HIGH

    def test_complex_debugging_gets_strong(self) -> None:
        selection = select(
            fact_overrides={
                "mechanical": False,
                "requires_iterative_debugging": True,
                "affected_modules": 4,
            }
        )
        assert selection.tier is ModelTier.STRONG

    def test_debugging_alone_reaches_strong_without_cross_module(self) -> None:
        """⚠️ **隔离测试**：`requires_iterative_debugging` **本身就是强信号**。

        L33 明确"**复杂调试** → 较强/较高"。因此**单模块**的复杂调试
        也必须达到 STRONG —— 不得依赖跨模块影响来"蹭"到高档位。
        """
        selection = select(
            fact_overrides={
                "mechanical": False,
                "requires_iterative_debugging": True,
                "affected_modules": 1,
                "touches_cross_module_contracts": False,
            }
        )
        assert selection.tier is ModelTier.STRONG
        assert selection.effort is EffortLevel.HIGH

    def test_debugging_alone_reaches_strong_even_when_mechanical(self) -> None:
        """⚠️ **隔离测试**：``mechanical`` **不得抹掉** debugging 信号。

        机械但需要迭代调试 → 仍是复杂调试，仍要 STRONG。
        """
        selection = select(
            fact_overrides={
                "mechanical": True,
                "requires_iterative_debugging": True,
                "affected_modules": 1,
                "touches_cross_module_contracts": False,
            }
        )
        assert selection.tier is ModelTier.STRONG

    def test_a_plain_single_module_task_stays_balanced(self) -> None:
        """反向保险：**常规但非机械**的单模块任务仍是平衡档 —— 没有过度升配。"""
        selection = select(
            fact_overrides={
                "mechanical": False,
                "requires_iterative_debugging": False,
                "affected_modules": 1,
                "touches_cross_module_contracts": False,
            }
        )
        assert selection.tier is ModelTier.BALANCED

    def test_maximum_is_not_the_default_for_hard_work(self) -> None:
        """最高配置只用于**必要的**极难高价值任务，不是"难就用"。"""
        selection = select(
            fact_overrides={
                "mechanical": False,
                "touches_cross_module_contracts": True,
                "requires_iterative_debugging": True,
                "affected_modules": 20,
            }
        )
        assert selection.tier is not ModelTier.MAXIMUM

    def test_review_task_reaches_the_top_tier(self) -> None:
        """重要审查 → 较强或更高（L33 的第三类）。

        注意要同时解除 ``mechanical``，否则"机械"会短路复杂度。
        """
        selection = select(
            fact_overrides={
                "is_review_task": True,
                "mechanical": False,
                "affected_modules": 4,
                "touches_cross_module_contracts": True,
                "requires_iterative_debugging": True,
            }
        )
        assert selection.tier in (ModelTier.STRONG, ModelTier.MAXIMUM)

    def test_even_a_simple_review_task_gets_a_strong_tier(self) -> None:
        """代码简单但属复核任务 → 仍要较强档（复核本身要求高）。"""
        selection = select(
            fact_overrides={
                "is_review_task": True,
                "mechanical": False,
                "affected_modules": 1,
                "touches_cross_module_contracts": False,
                "requires_iterative_debugging": False,
            }
        )
        assert selection.tier is ModelTier.STRONG

    def test_effort_matches_the_tier_floor(self) -> None:
        for fact_overrides, expected_tier in (
            ({"mechanical": True, "affected_modules": 1}, ModelTier.LOW_COST),
            (
                {
                    "mechanical": False,
                    "touches_cross_module_contracts": False,
                    "requires_iterative_debugging": False,
                    "affected_modules": 1,
                },
                ModelTier.BALANCED,
            ),
            (
                {"mechanical": False, "touches_cross_module_contracts": True},
                ModelTier.STRONG,
            ),
        ):
            selection = select(fact_overrides=fact_overrides)
            assert selection.tier is expected_tier
            assert selection.effort is ModelRouter.effort_floor(expected_tier)

    def test_is_deterministic(self) -> None:
        assert select(fact_overrides={"affected_modules": 4}) == select(
            fact_overrides={"affected_modules": 4}
        )

    def test_reason_is_present_and_explains_the_choice(self) -> None:
        selection = select(
            fact_overrides={
                "mechanical": False,
                "touches_cross_module_contracts": True,
            }
        )
        assert selection.reason.strip()
        assert "strong" in selection.reason
        assert "effort=" in selection.reason

    def test_selection_is_frozen(self) -> None:
        selection = select()
        with pytest.raises(dataclasses.FrozenInstanceError):
            selection.tier = ModelTier.MAXIMUM  # type: ignore[misc]


class TestTierDecisionIsAuditable:
    def test_decision_exposes_base_and_final_tier(self) -> None:
        decision = ModelRouter().decide(
            Evaluator().evaluate(task_facts(), capability=snapshot(), project=project()),
            risk(),
            project=project(),
        )
        assert isinstance(decision, TierDecision)
        assert decision.base_tier in tuple(ModelTier)
        assert decision.final_tier in tuple(ModelTier)

    def test_risk_bolt_on_is_recorded(self) -> None:
        decision = ModelRouter().decide(
            Evaluator().evaluate(
                task_facts(
                    mechanical=False,
                    touches_cross_module_contracts=False,
                    requires_iterative_debugging=False,
                    affected_modules=1,
                ),
                capability=snapshot(),
                project=project(),
            ),
            risk(RiskLevel.HIGH),
            project=project(),
        )
        assert decision.risk_raised is True
        assert decision.base_tier is ModelTier.BALANCED
        assert decision.final_tier is ModelTier.STRONG


# ==========================================================================
# 风险是**单向**约束（L33 / 架构 4.2）
# ==========================================================================


class TestRiskConstrainsOneWayOnly:
    def test_high_risk_raises_the_tier_by_one_step(self) -> None:
        low = select(
            fact_overrides={
                "mechanical": False,
                "touches_cross_module_contracts": False,
                "affected_modules": 1,
            }
        )
        high = select(
            fact_overrides={
                "mechanical": False,
                "touches_cross_module_contracts": False,
                "affected_modules": 1,
            },
            risk_assessment=risk(
                RiskLevel.HIGH,
                categories=(RiskCategory.HARD_TO_REVERT_OPERATION,),
            ),
        )
        assert low.tier is ModelTier.BALANCED
        assert high.tier is ModelTier.STRONG

    def test_high_risk_never_lowers_a_tier(self) -> None:
        strong = select(
            fact_overrides={"mechanical": False, "touches_cross_module_contracts": True}
        )
        strong_with_risk = select(
            fact_overrides={"mechanical": False, "touches_cross_module_contracts": True},
            risk_assessment=risk(
                RiskLevel.HIGH,
                categories=(RiskCategory.PRODUCTION_OR_REAL_USERS,),
            ),
        )
        assert strong.tier is ModelTier.STRONG
        assert strong_with_risk.tier is not ModelTier.LOW_COST
        assert strong_with_risk.tier is not ModelTier.BALANCED

    def test_low_risk_does_not_downgrade(self) -> None:
        assert select(
            fact_overrides={"mechanical": False, "touches_cross_module_contracts": True},
            risk_assessment=risk(RiskLevel.LOW),
        ).tier is ModelTier.STRONG

    def test_weak_evidence_raises_one_step(self) -> None:
        baseline = select(
            fact_overrides={
                "mechanical": False,
                "touches_cross_module_contracts": False,
                "affected_modules": 1,
                "evidence": EvidenceStrength.STRONG,
            }
        )
        weak = select(
            fact_overrides={
                "mechanical": False,
                "touches_cross_module_contracts": False,
                "affected_modules": 1,
                "evidence": EvidenceStrength.WEAK,
            }
        )
        assert baseline.tier is ModelTier.BALANCED
        assert weak.tier is ModelTier.STRONG

    def test_no_signal_ever_reaches_below_the_complexity_floor(self) -> None:
        """任何信号都不得把档位压到复杂度下限以下。"""
        floor = select(
            fact_overrides={"mechanical": False, "touches_cross_module_contracts": True}
        ).tier
        for assessment in (
            risk(RiskLevel.LOW),
            risk(RiskLevel.LOW, categories=()),
        ):
            assert (
                select(
                    fact_overrides={
                        "mechanical": False,
                        "touches_cross_module_contracts": True,
                    },
                    risk_assessment=assessment,
                ).tier
                == floor
            )


# ==========================================================================
# ProjectContext 偏好：只是 preference
# ==========================================================================


class TestProjectPreferenceIsOnlyAPreference:
    def test_preference_can_raise_the_tier(self) -> None:
        selection = select(
            fact_overrides={
                "mechanical": False,
                "touches_cross_module_contracts": False,
                "affected_modules": 1,
            },
            project_context=project(model_preference="strong"),
        )
        assert selection.tier is ModelTier.STRONG

    def test_preference_cannot_lower_below_the_complexity_floor(self) -> None:
        """项目偏好**不能**覆盖复杂度下限 —— 只能收窄，不能放宽。"""
        selection = select(
            fact_overrides={
                "mechanical": False,
                "touches_cross_module_contracts": True,
            },
            project_context=project(model_preference="low_cost"),
        )
        assert selection.tier is ModelTier.STRONG

    def test_preference_is_ignored_when_it_would_downgrade(self) -> None:
        decision = ModelRouter().decide(
            Evaluator().evaluate(
                task_facts(mechanical=False, touches_cross_module_contracts=True),
                capability=snapshot(),
                project=project(),
            ),
            risk(),
            project=project(model_preference="balanced"),
        )
        assert decision.preference_applied is False
        assert decision.final_tier is ModelTier.STRONG

    def test_preference_does_not_grant_authority(self) -> None:
        """偏好不获得授权权力 —— 它不触及 risk，只影响档位下限之上。"""
        forced_up = select(
            fact_overrides={"mechanical": True, "affected_modules": 1},
            project_context=project(model_preference="strong"),
        )
        assert forced_up.tier is ModelTier.STRONG

    def test_unparseable_preference_raises(self) -> None:
        with pytest.raises(PlatformDetailError):
            select(
                fact_overrides={"mechanical": True, "affected_modules": 1},
                project_context=project(model_preference="gpt-5-codex"),
            )

    def test_parse_tier_preference_accepts_abstract_tiers_only(self) -> None:
        assert parse_tier_preference("balanced") is ModelTier.BALANCED
        assert parse_tier_preference("STRONG") is ModelTier.STRONG
        assert parse_tier_preference("low_cost") is ModelTier.LOW_COST
        assert parse_tier_preference("maximum") is ModelTier.MAXIMUM
        assert parse_tier_preference(None) is None
        assert parse_tier_preference("   ") is None
        for bad in ("gpt-5", "openai:gpt-5", "cheap", "tier1"):
            with pytest.raises(PlatformDetailError):
                parse_tier_preference(bad)

    def test_platform_detail_error_is_a_value_error(self) -> None:
        assert issubclass(PlatformDetailError, ValueError)


# ==========================================================================
# 边界：只选抽象档位，不含平台细节
# ==========================================================================


class TestNoPlatformDetails:
    def test_selection_carries_only_tier_effort_reason(self) -> None:
        field_names = {f.name for f in dataclasses.fields(ModelSelection)}
        assert field_names == {"tier", "effort", "reason"}

    def test_no_provider_model_identifier_is_produced(self) -> None:
        selection = select()
        for forbidden in ("model_id", "provider", "platform", "actual_model_id"):
            assert not hasattr(selection, forbidden), forbidden

    def test_module_never_mentions_provider_names(self) -> None:
        """模块里的**字符串字面量**不得出现 provider / 平台名。

        ⚠️ 排除 docstring：模块 docstring 说明"不得引入平台细节"时
           会提到 provider 概念，那是文档，不是实现里的平台参数。
           但实现中若真出现 provider 名字面量，就会被这里拦下。
        """
        excluded = _docstring_nodes()
        literals = [
            node.value
            for node in ast.walk(_MODULE_TREE)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in excluded
        ]
        joined = " ".join(literals).lower()
        for forbidden in ("gpt-", "codex", "openai", "anthropic", "claude"):
            assert forbidden not in joined, forbidden

    def test_the_exclusion_does_not_swallow_real_literals(self) -> None:
        """非平凡性：排除 docstring 后，实现里的字符串字面量仍然被检查到。"""
        excluded = _docstring_nodes()
        literals = [
            node.value
            for node in ast.walk(_MODULE_TREE)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in excluded
        ]
        assert literals, "排除 docstring 后仍应剩下实现用的字符串字面量"

    def test_router_does_not_import_the_adapter_layer(self) -> None:
        assert "adapters" not in _imported_modules()

    def test_tier_and_effort_are_abstract_enums(self) -> None:
        selection = select()
        assert isinstance(selection.tier, ModelTier)
        assert isinstance(selection.effort, EffortLevel)


# ==========================================================================
# 升级接口已就位，但 Recovery / Cost 未接线（TEST-MODEL-002 / 003 deferred）
# ==========================================================================


class TestUpgradeHooksAndInitialBumps:
    """为未来升级/降级留**干净接口**，并**如实记录**本批已实现的部分。

    ⚠️ **TEST-MODEL-002 = PARTIAL / DEFERRED**。正确描述是：

        已实现（[06] 侧，基于**当前** Evaluation / RiskAssessment）：
            低置信度（``rework_probability == HIGH``）→ 初始档位升一档
            ``RiskLevel.HIGH``                        → 初始档位升一档

        未实现（需其他节点）：
            失败 / 冲突 / **执行中**复杂度或风险上升
            → [14] Recovery 触发的重新路由

    ⚠️ 因此**不得**断言"``select()`` 从不调用 ``raise_tier``" ——
       初始决策的升级**已经**通过它实现。
    """

    def test_a_pure_tier_step_function_exists(self) -> None:
        """[14] Recovery 将来可以据此升级一档。"""
        assert ModelRouter.raise_tier(ModelTier.BALANCED) is ModelTier.STRONG
        assert ModelRouter.raise_tier(ModelTier.STRONG) is ModelTier.MAXIMUM
        assert ModelRouter.raise_tier(ModelTier.MAXIMUM) is ModelTier.MAXIMUM

    def test_a_pure_tier_lower_function_exists(self) -> None:
        """[15c] Cost Summary 将来可以据此为**后续任务**降级。"""
        assert ModelRouter.lower_tier(ModelTier.STRONG) is ModelTier.BALANCED
        assert ModelRouter.lower_tier(ModelTier.BALANCED) is ModelTier.LOW_COST
        assert ModelRouter.lower_tier(ModelTier.LOW_COST) is ModelTier.LOW_COST

    # --- 已实现：初始决策的两类升级 ---

    def test_initial_low_confidence_bump_is_already_implemented(self) -> None:
        baseline = select(
            fact_overrides={
                "mechanical": False,
                "touches_cross_module_contracts": False,
                "affected_modules": 1,
                "evidence": EvidenceStrength.STRONG,
            }
        )
        weak = select(
            fact_overrides={
                "mechanical": False,
                "touches_cross_module_contracts": False,
                "affected_modules": 1,
                "evidence": EvidenceStrength.WEAK,
            }
        )
        assert ModelRouter.ORDER.index(weak.tier) == (
            ModelRouter.ORDER.index(baseline.tier) + 1
        )

    def test_initial_high_risk_bump_is_already_implemented(self) -> None:
        overrides = {
            "mechanical": False,
            "touches_cross_module_contracts": False,
            "affected_modules": 1,
        }
        baseline = select(fact_overrides=overrides)
        risky = select(
            fact_overrides=overrides,
            risk_assessment=risk(
                RiskLevel.HIGH,
                categories=(RiskCategory.HARD_TO_REVERT_OPERATION,),
            ),
        )
        assert ModelRouter.ORDER.index(risky.tier) == (
            ModelRouter.ORDER.index(baseline.tier) + 1
        )

    def test_decide_records_whether_risk_raised_the_tier(self) -> None:
        decision = ModelRouter().decide(
            Evaluator().evaluate(
                task_facts(
                    mechanical=False,
                    touches_cross_module_contracts=False,
                    affected_modules=1,
                ),
                capability=snapshot(),
                project=project(),
            ),
            risk(RiskLevel.HIGH),
            project=project(),
        )
        assert decision.risk_raised is True

    # --- 未实现：Recovery / 执行中升级 ---

    def test_recovery_triggered_rerouting_is_not_implemented(self) -> None:
        """**未实现**：Recovery 触发的重新路由 —— 不得假装完成。"""
        modules = _imported_modules()
        assert "recovery" not in modules
        for name in ("RecoveryDecision", "FailureClass", "RecoveryTarget"):
            assert name not in _imported_names(), name

    def test_mid_execution_escalation_has_no_input_channel(self) -> None:
        """**未实现**：执行中的复杂度 / 风险上升 —— 没有执行期输入。"""
        import inspect

        parameters = set(inspect.signature(ModelRouter.decide).parameters)
        assert parameters == {"self", "evaluation", "risk_assessment", "project"}

    def test_module_does_not_import_recovery_or_cost(self) -> None:
        """本批**不接线** [14] / [15a] / [15b] / [15c]。"""
        modules = _imported_modules()
        for forbidden in ("recovery", "cost", "risk"):
            assert forbidden not in modules, forbidden
        for name in ("CostEstimator", "CostSummary", "RecoveryDecision"):
            assert name not in _imported_names(), name

    def test_module_does_not_touch_approval(self) -> None:
        for name in ("ApprovalJudgment", "ApprovalGate", "needs_approval"):
            assert name not in _imported_names(), name

    def test_only_the_level_is_consumed_from_the_risk_assessment(self) -> None:
        """[06] 只消费 ``RiskAssessment.level`` —— 不解释具体风险类别。"""
        baseline = select(risk_assessment=risk(RiskLevel.MEDIUM))
        with_categories = select(
            risk_assessment=risk(
                RiskLevel.MEDIUM,
                categories=(RiskCategory.EXTERNAL_WRITE,),
            )
        )
        assert baseline.tier is with_categories.tier


# ==========================================================================
# mechanical 不得短路更强的任务信号
# ==========================================================================


class TestMechanicalDoesNotEraseStrongerSignals:
    """⚠️ ``mechanical`` 可以降低**内在**难度，但**不得**抹掉更强的
    cross-module / debugging / scope 信号。

    否则 ``mechanical=True + 跨模块契约`` 会被无脑压到最低档，
    与 L33 的"跨模块 / 复杂调试 / 重要审查 → 较强档"直接冲突。
    """

    def test_mechanical_cross_module_is_not_cheapest(self) -> None:
        selection = select(
            fact_overrides={
                "mechanical": True,
                "touches_cross_module_contracts": True,
            }
        )
        assert selection.tier is ModelTier.STRONG

    def test_mechanical_iterative_debugging_reaches_strong(self) -> None:
        selection = select(
            fact_overrides={
                "mechanical": True,
                "requires_iterative_debugging": True,
                "affected_modules": 1,
                "touches_cross_module_contracts": False,
            }
        )
        assert selection.tier is ModelTier.STRONG

    def test_mechanical_large_scope_is_not_cheapest(self) -> None:
        selection = select(
            fact_overrides={
                "mechanical": True,
                "affected_modules": 20,
            }
        )
        assert selection.tier is not ModelTier.LOW_COST

    def test_mechanical_alone_still_gets_the_cheapest_tier(self) -> None:
        """反向：纯粹的机械低风险任务**仍然**是最低档 —— 没有过度升配。"""
        selection = select(
            fact_overrides={
                "mechanical": True,
                "affected_modules": 1,
                "touches_cross_module_contracts": False,
                "requires_iterative_debugging": False,
            }
        )
        assert selection.tier is ModelTier.LOW_COST
        assert selection.effort is EffortLevel.LOW

    def test_mechanical_does_not_short_circuit_evaluation_complexity(self) -> None:
        """Evaluator 侧：更强的任务信号不得被 ``mechanical`` 抹掉。

        ⚠️ **跨模块由 ``impact_scope`` 承载，不在 ``complexity`` 里重复计数**，
           所以这里用 **debugging**（本维度内的强信号）做断言，
           跨模块的档位效果由 ``test_mechanical_cross_module_is_not_cheapest`` 覆盖。
        """
        plain = Evaluator().evaluate(
            task_facts(mechanical=True, affected_modules=1),
            capability=snapshot(),
            project=project(),
        )
        debugging = Evaluator().evaluate(
            task_facts(
                mechanical=True,
                affected_modules=1,
                requires_iterative_debugging=True,
            ),
            capability=snapshot(),
            project=project(),
        )
        assert plain.complexity is Ordinal.LOW
        assert debugging.complexity is Ordinal.HIGH

    def test_cross_module_is_carried_by_scope_not_by_complexity(self) -> None:
        """跨模块**只沿一条路径**影响档位：``impact_scope`` → scope floor。

        ⚠️ 上一版这条测试只断言了 ``impact_scope``，
           **没有断言 ``complexity``** —— 名字声称的比它证明的强。
           现在补齐：跨模块**不得**抬高 ``complexity``（那是双重承载）。
        """
        cross = Evaluator().evaluate(
            task_facts(
                mechanical=True,
                affected_modules=1,
                touches_cross_module_contracts=True,
                requires_iterative_debugging=False,
            ),
            capability=snapshot(),
            project=project(),
        )
        # ① complexity **未被**跨模块抬升
        assert cross.complexity is Ordinal.LOW
        # ② 跨模块体现在 scope
        assert cross.impact_scope is not ImpactScope.LOCAL
        assert cross.impact_scope is ImpactScope.SYSTEM_WIDE
        # ③ 最终档位由 scope floor 保证不低于 STRONG
        selection = select(
            fact_overrides={
                "mechanical": True,
                "affected_modules": 1,
                "touches_cross_module_contracts": True,
                "requires_iterative_debugging": False,
            }
        )
        assert selection.tier is ModelTier.STRONG

    def test_cross_module_does_not_change_complexity_for_any_mechanical_state(
        self,
    ) -> None:
        """两种 mechanical 取值下，跨模块都不改变 ``complexity``。"""
        for mechanical in (True, False):
            without = Evaluator().evaluate(
                task_facts(
                    mechanical=mechanical,
                    affected_modules=1,
                    touches_cross_module_contracts=False,
                ),
                capability=snapshot(),
                project=project(),
            )
            with_cross = Evaluator().evaluate(
                task_facts(
                    mechanical=mechanical,
                    affected_modules=1,
                    touches_cross_module_contracts=True,
                ),
                capability=snapshot(),
                project=project(),
            )
            assert without.complexity is with_cross.complexity, mechanical

    def test_complexity_calculation_never_reads_cross_module_flag(self) -> None:
        """机械核对：``_complexity`` 的**可执行代码**里没有这个属性访问。

        防止将来有人把 ``touches_cross_module_contracts`` 重新塞回复杂度评分，
        从而恢复"同一事实计两次"的旧问题。

        ⚠️ 用 **AST** 检查真实代码引用，不看 docstring ——
           docstring 里说明"跨模块不计入本维度"时会**提到**这个名字，
           那是文档，不是访问。
        """
        import ast
        import inspect

        module_tree = ast.parse(inspect.getsource(Evaluator))
        function = next(
            node
            for node in ast.walk(module_tree)
            if isinstance(node, ast.FunctionDef) and node.name == "_complexity"
        )

        referenced = {
            node.attr for node in ast.walk(function) if isinstance(node, ast.Attribute)
        } | {node.id for node in ast.walk(function) if isinstance(node, ast.Name)}

        assert "touches_cross_module_contracts" not in referenced

        # 非平凡性：同一类里确实有地方**真正访问**它（scope / risk），
        # 排除"属性改名导致两边都查不到"的误判。
        whole_class = {
            node.attr for node in ast.walk(module_tree) if isinstance(node, ast.Attribute)
        }
        assert "touches_cross_module_contracts" in whole_class


# ==========================================================================
# 输入校验
# ==========================================================================


class TestInputValidation:
    def test_rejects_a_non_evaluation(self) -> None:
        with pytest.raises(TypeError):
            ModelRouter().select("nope", risk(), project=project())  # type: ignore[arg-type]

    def test_rejects_a_non_risk_assessment(self) -> None:
        evaluation = Evaluator().evaluate(
            task_facts(), capability=snapshot(), project=project()
        )
        with pytest.raises(TypeError):
            ModelRouter().select(evaluation, "low", project=project())  # type: ignore[arg-type]

    def test_rejects_a_non_project_context(self) -> None:
        evaluation = Evaluator().evaluate(
            task_facts(), capability=snapshot(), project=project()
        )
        with pytest.raises(TypeError):
            ModelRouter().select(evaluation, risk(), project="/repo")  # type: ignore[arg-type]

    def test_effort_floor_is_total_over_tiers(self) -> None:
        for tier in ModelTier:
            assert ModelRouter.effort_floor(tier) in tuple(EffortLevel)


# ==========================================================================
# Amendment A4 —— per-child ModelSelection 的**显式投影**
#
# ⚠️ 冻结矩阵（架构第 8 节）**没有**专门的 Test ID：
#    A4 的 per-child assignment 是"[06] 已形成 ModelSelection 的显式投影 seam"，
#    **不扩大** `TEST-MODEL-001` 的"4 档最低配置"承载语义，也**不新造** Test ID。
#    故本节以 **A4 / seam invariant** 名义记录（见 README_step5b.md §9）。
# ==========================================================================


def branch(branch_id: str, *, read_only: bool = True) -> BranchSpec:
    return BranchSpec(
        branch_id=branch_id,
        goal=f"goal {branch_id}",
        read_only=read_only,
        completion_criteria=f"done {branch_id}",
    )


def plan_with(*branches: BranchSpec) -> Plan:
    if not branches:
        return Plan(
            mode=ExecutionMode.DIRECT,
            main_thread_duty="execute on the main thread",
            reason="simple task",
        )
    return Plan(
        mode=ExecutionMode.MULTI_AGENT,
        main_thread_duty="coordinate branches",
        reason="independent subtasks",
        branches=branches,
    )


def task_selection() -> ModelSelection:
    return ModelSelection(
        tier=ModelTier.STRONG, effort=EffortLevel.HIGH, reason="task-level reason"
    )


class TestA4PerChildAssignment:
    """A4.3 / A4.4 / A4.6 / A4.7 的落点。"""

    # -- A4.3 覆盖形状 --------------------------------------------------

    def test_zero_branch_plan_gets_an_empty_mapping(self) -> None:
        assert ModelRouter().assign_branch_models(
            plan_with(), selection=task_selection()
        ) == ()

    def test_multi_agent_mapping_covers_exactly_the_children(self) -> None:
        plan = plan_with(branch("br_a"), branch("br_b"), branch("br_c"))
        mapping = ModelRouter().assign_branch_models(plan, selection=task_selection())
        assert [b for b, _ in mapping] == ["br_a", "br_b", "br_c"]
        assert {b for b, _ in mapping} == {
            spec.branch_id for spec in plan.branches
        }

    def test_mapping_follows_the_plan_order(self) -> None:
        plan = plan_with(branch("br_z"), branch("br_a"))
        mapping = ModelRouter().assign_branch_models(plan, selection=task_selection())
        assert [b for b, _ in mapping] == ["br_z", "br_a"]

    # -- A4.4 V1：与任务级同档位（是决定，不是默认） ---------------------

    def test_every_child_inherits_the_task_level_tier_and_effort(self) -> None:
        selection = task_selection()
        mapping = ModelRouter().assign_branch_models(
            plan_with(branch("br_a"), branch("br_b")), selection=selection
        )
        for _, child_selection in mapping:
            assert child_selection.tier is selection.tier
            assert child_selection.effort is selection.effort

    def test_assignment_is_identical_for_all_children(self) -> None:
        """V1 不做差异化 —— 不得因 ``read_only`` 等 child 属性而分叉。"""
        mapping = ModelRouter().assign_branch_models(
            plan_with(branch("br_a", read_only=True), branch("br_b", read_only=False)),
            selection=task_selection(),
        )
        tiers = {s.tier for _, s in mapping}
        efforts = {s.effort for _, s in mapping}
        assert len(tiers) == 1
        assert len(efforts) == 1

    # -- A4.6 每条 assignment 自带理由 ----------------------------------

    def test_each_assignment_carries_an_inheritance_reason(self) -> None:
        """光看产物就能回答"为什么这个分支用这个档"。"""
        mapping = ModelRouter().assign_branch_models(
            plan_with(branch("br_a"), branch("br_b")), selection=task_selection()
        )
        for branch_id, child_selection in mapping:
            assert branch_id in child_selection.reason
            assert "inherit" in child_selection.reason
            assert "no per-branch evidence" in child_selection.reason
            assert ModelTier.STRONG.value in child_selection.reason

    # -- A4.7 投影纯度 --------------------------------------------------

    def test_projection_does_not_call_select_or_decide(self, monkeypatch) -> None:
        """**核心不变式**：投影不得产生新决策。

        把 ``select`` / ``decide`` 换成会爆炸的替身；若投影路径碰到它们，
        这个测试就会红。
        """

        def boom(*args, **kwargs):  # pragma: no cover - 只为在违规时爆炸
            raise AssertionError("投影**不得**调用 select()/decide()（A4.7）")

        monkeypatch.setattr(ModelRouter, "select", boom)
        monkeypatch.setattr(ModelRouter, "decide", boom)
        mapping = ModelRouter().assign_branch_models(
            plan_with(branch("br_a"), branch("br_b")), selection=task_selection()
        )
        assert len(mapping) == 2

    def test_projection_does_not_read_child_goal_or_read_only(self) -> None:
        """不得读取/解释 child 的 ``goal`` / ``read_only``。

        两个只有 ``goal`` / ``read_only`` 不同的计划（branch_id 相同），
        必须拿到**完全相同**的映射。
        """

        def plan_with_text(goal: str, *, read_only: bool) -> Plan:
            return plan_with(
                BranchSpec(
                    branch_id="br_x",
                    goal=goal,
                    read_only=read_only,
                    completion_criteria="c",
                ),
                BranchSpec(
                    branch_id="br_y",
                    goal=goal,
                    read_only=read_only,
                    completion_criteria="c",
                ),
            )

        mechanical = ModelRouter().assign_branch_models(
            plan_with_text("completely mechanical rename", read_only=True),
            selection=task_selection(),
        )
        hard = ModelRouter().assign_branch_models(
            plan_with_text("hard cross-module refactor with migration", read_only=False),
            selection=task_selection(),
        )
        assert mechanical == hard
        assert [(b, s.tier, s.effort) for b, s in mechanical] == [
            (b, s.tier, s.effort) for b, s in hard
        ]

    def test_the_projection_test_is_not_vacuous(self, monkeypatch) -> None:
        """上一条的对照：``select()`` 确实会炸 —— 证明替身真的接上了。"""
        monkeypatch.setattr(
            ModelRouter, "select", lambda *a, **k: (_ for _ in ()).throw(AssertionError())
        )
        with pytest.raises(AssertionError):
            ModelRouter().select(
                Evaluator().evaluate(task_facts(), capability=snapshot(), project=project()),
                risk(),
                project=project(),
            )

    # -- 与装配器合起来：产物合法 ----------------------------------------

    def test_projection_satisfies_the_frozen_routing_plan_invariant(self) -> None:
        """投影 + 装配器 → 冻结 ``RoutingPlan`` 的"恰好覆盖"不变式成立。"""
        from adaptive_router.routing_plan import RoutingPlanAssembler

        plan = plan_with(branch("br_a"), branch("br_b"))
        selection = task_selection()
        mapping = ModelRouter().assign_branch_models(plan, selection=selection)
        routing = RoutingPlanAssembler(
            run_id_factory=lambda: "run_x",
            main_thread_branch_id_factory=lambda: "br_main",
        ).assemble(plan, main_thread_model=selection, branch_models=mapping)
        assert routing.branch_ids == ("br_a", "br_b")
        assert routing.main_thread_model.tier is ModelTier.STRONG

    def test_input_validation(self) -> None:
        pair = plan_with(branch("br_a"), branch("br_b"))
        with pytest.raises(TypeError):
            ModelRouter().assign_branch_models("nope", selection=task_selection())  # type: ignore[arg-type]
        with pytest.raises(TypeError):
            ModelRouter().assign_branch_models(pair, selection=ModelTier.STRONG)  # type: ignore[arg-type]
        with pytest.raises(TypeError):
            ModelRouter().assign_branch_models(
                pair, selection=None  # type: ignore[arg-type]
            )


#: A4.7 的投影面：这两个函数共同构成 per-child 投影。
PROJECTION_FUNCTIONS = ("assign_branch_models", "_inherited")

#: A4.7 明令不得读取/解释的 child 内容。
FORBIDDEN_READS = ("goal", "read_only", "completion_criteria")


class TestA4ProjectionPurityByStructure:
    """**结构锁**：A4.7 的"纯投影"必须是**永久证据**，而不是"当前行为碰巧没差异化"。

    `test_projection_does_not_read_child_goal_or_read_only` 证明的是
    *"改变 goal/read_only 后输出不变"* —— 它能证明**没有据此差异化**，
    却证明不了**代码根本没读它们**：将来有人写下

        _ = spec.read_only

    那条行为测试**仍然是绿的**，但字面冻结要求已经被违反。

    本类直接对 **AST 属性访问**断言，补上这半步。
    """

    def test_projection_never_reads_child_content(self) -> None:
        for function in PROJECTION_FUNCTIONS:
            reads = _attribute_reads(function)
            for forbidden in FORBIDDEN_READS:
                assert forbidden not in reads, (function, forbidden)

    def test_the_check_is_not_vacuous_because_branch_id_is_read(self) -> None:
        """**非平凡性对照**：如果投影什么都不读，上面的断言就是空地的。"""
        reads = set()
        for function in PROJECTION_FUNCTIONS:
            reads |= _attribute_reads(function)
        assert "branch_id" in reads

    def test_projection_writes_no_attributes(self) -> None:
        """A4.7 的"不得改 ``tier`` / ``effort``"：投影面里没有任何属性写入。"""
        for function in PROJECTION_FUNCTIONS:
            assert _attribute_writes(function) == set(), function

    def test_the_structural_detector_catches_a_violation(self) -> None:
        """**检测器非空地**：合成源码证明这条锁真的能抓到违规。

        ⚠️ 这条测试第一次跑就抓到了**检测器自己**的 bug：
        原实现遍历整个 target 子树，于是 ``plan.branches[0].goal = "x"``
        会把**被读**的内层 ``branches`` 也算成"被写"。
        只有最外层 ``Attribute.attr`` 才是写入目标。
        """
        violating = (
            "def assign_branch_models(self, plan):\n"
            "    _ = plan.branches[0].read_only\n"
            "    return plan.branches[0].branch_id\n"
        )
        reads = _attribute_reads("assign_branch_models", violating)
        assert "read_only" in reads  # 违规被抓到
        assert "branch_id" in reads  # 非平凡性仍成立

        writes = _attribute_writes(
            "assign_branch_models",
            "def assign_branch_models(self, plan):\n"
            "    plan.branches[0].goal = 'x'\n",
        )
        assert writes == {"goal"}, "内层 branches 是**读**，不得算作写入"

        subscript_writes = _attribute_writes(
            "assign_branch_models",
            "def assign_branch_models(self, plan):\n"
            "    plan.branches[0] = 'x'\n",
        )
        assert subscript_writes == {"branches"}

    def test_the_structural_detector_sees_goal_and_completion_criteria_too(
        self,
    ) -> None:
        """三类被禁内容都要能被检测到，不能只覆盖 ``read_only``。"""
        for forbidden in FORBIDDEN_READS:
            synthetic = (
                "def assign_branch_models(self, plan):\n"
                f"    return plan.branches[0].{forbidden}\n"
            )
            assert forbidden in _attribute_reads(
                "assign_branch_models", synthetic
            ), forbidden
