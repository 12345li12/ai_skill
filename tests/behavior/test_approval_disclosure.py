"""Approval Disclosure provenance 行为测试（Step 5B.4.d / Amendment A8）。

架构依据：
  `docs/architecture.md` 2 节 ``Approval Judgment`` **Amendment A8**（**FROZEN**）
  —— Approval Disclosure provenance seam。

⚠️ **本文件不引入新的 Test ID** —— 冻结矩阵（架构第 8 节）里没有对应 ID，
   测试以 **A8 / seam invariant** 名义记录。
"""

from __future__ import annotations

import ast
import dataclasses
import pathlib

import pytest

from adaptive_router.approval_disclosure import (
    HARD_TO_REVERT_UNKNOWN,
    ApprovalDisclosure,
    DisclosureProvenanceError,
    build_disclosure,
    project_impact_scope,
    project_proposed_action,
    resolve_recovery_method,
)
from adaptive_router.contracts import (
    BranchSpec,
    CapabilitySnapshot,
    Confidence,
    Evaluation,
    ExecutionMode,
    ImpactScope,
    Ordinal,
    Plan,
    ProjectContext,
    RiskAssessment,
    RiskCategory,
    RiskLevel,
)
from adaptive_router.planner import Evaluator, EvidenceStrength, TaskFacts
from adaptive_router.risk import RiskEvaluator, RiskInputs

from _import_detectors import imported_module_tokens, imported_names

MODULE_PATH = (
    pathlib.Path(__file__).resolve().parents[2]
    / "src/adaptive_router/approval_disclosure.py"
)
MODULE_SOURCE = MODULE_PATH.read_text(encoding="utf-8")
_MODULE_TREE = ast.parse(MODULE_SOURCE)

FIXED_TIME = "2026-09-18T00:00:00Z"


def _imported_modules() -> set[str]:
    """被测模块的 import token（保守检测器，见 `_import_detectors`）。"""
    return imported_module_tokens(MODULE_SOURCE)


def _imported_names() -> set[str]:
    return imported_names(MODULE_SOURCE)


def _function_node(name: str) -> ast.AST:
    for node in ast.walk(_MODULE_TREE):
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == name
        ):
            return node
    raise AssertionError(f"源码里找不到函数 {name}")


def _data_attribute_reads(name: str) -> set[str]:
    """函数体里被读取的**数据属性**（``x.y`` 里的 ``y``）。

    ⚠️ **排除方法调用名**（``parts.append`` / ``"；".join``）：
    它们是 ``ast.Call`` 的 ``func``，属方法调用而非被读取的事实字段。
    不区分的话断言会被 ``append`` / ``join`` 污染。
    """
    node = _function_node(name)
    call_funcs = {id(call.func) for call in ast.walk(node) if isinstance(call, ast.Call)}
    return {
        attribute.attr
        for attribute in ast.walk(node)
        if isinstance(attribute, ast.Attribute) and id(attribute) not in call_funcs
    }


def _method_call_names(name: str) -> set[str]:
    """函数体里被**调用**的方法名（与 ``_data_attribute_reads`` 互补）。"""
    node = _function_node(name)
    return {
        call.func.attr
        for call in ast.walk(node)
        if isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
    }


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
        reliable_cost_data=True,
        probed_dimensions=(),
        unavailable_reason=None,
        probed_at=FIXED_TIME,
    )


def project() -> ProjectContext:
    return ProjectContext.absent("/repo", FIXED_TIME)


def facts(**overrides: object) -> TaskFacts:
    base: dict[str, object] = dict(
        affected_modules=3,
        mechanical=False,
        touches_cross_module_contracts=True,
        requires_iterative_debugging=False,
        is_review_task=False,
        subtasks=(),
        shared_context_size=2,
        has_sequential_dependency=False,
        branch_setup_cost=Ordinal.LOW,
        evidence=EvidenceStrength.STRONG,
        local_and_reversible=True,
    )
    base.update(overrides)
    return TaskFacts(**base)  # type: ignore[arg-type]


def evaluation(*, impact_scope: ImpactScope = ImpactScope.LOCAL) -> Evaluation:
    """用**真实** Evaluator 产出 Evaluation，再校验其 impact_scope 档位。

    ⚠️ ``Evaluation`` 没有"直接指定 impact_scope"的入口（那是 11 维判断的结果），
    因此这里通过 ``TaskFacts`` 的**真实判据**驱动它 —— 不伪造契约对象。

    `Evaluator._impact_scope` 的规则：

        affected_modules >= SYSTEM_WIDE_MODULES(8) 或 touches_cross_module_contracts
            → SYSTEM_WIDE
        affected_modules >= CROSS_MODULE_MODULES(3) → CROSS_MODULE
        否则                                        → LOCAL

    所以三档必须**同时**设置 ``affected_modules`` 与
    ``touches_cross_module_contracts``，否则那个布尔会把一切拉成 SYSTEM_WIDE。
    """
    driver = {
        ImpactScope.LOCAL: (1, False),
        ImpactScope.CROSS_MODULE: (3, False),
        ImpactScope.SYSTEM_WIDE: (9, True),
    }[impact_scope]
    produced = Evaluator().evaluate(
        facts(affected_modules=driver[0], touches_cross_module_contracts=driver[1]),
        capability=snapshot(),
        project=project(),
    )
    assert produced.impact_scope is impact_scope, produced.impact_scope
    return produced


def plan(
    *, mode: ExecutionMode = ExecutionMode.DIRECT, branches: tuple[BranchSpec, ...] = ()
) -> Plan:
    return Plan(
        mode=mode,
        main_thread_duty="coordinate branches and integrate results",
        reason="test",
        branches=branches,
    )


def reversible_branch(branch_id: str = "br_a") -> BranchSpec:
    return BranchSpec(
        branch_id=branch_id,
        goal=f"audit {branch_id}",
        read_only=True,
        completion_criteria="done",
    )


def risk(**overrides: object) -> RiskAssessment:
    base: dict[str, object] = dict(
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
    base.update(overrides)
    return RiskEvaluator().assess(RiskInputs(**base))  # type: ignore[arg-type]


# ==========================================================================
# A8.3 ① —— proposed_action 是 Plan 的确定性投影
# ==========================================================================


class TestProposedActionProjection:
    def test_zero_branch_plan_projects_the_main_thread_duty_verbatim(self) -> None:
        source = plan()
        assert source.main_thread_duty in project_proposed_action(source)

    def test_children_are_appended_in_plan_order(self) -> None:
        source = plan(
            mode=ExecutionMode.MULTI_AGENT,
            branches=(
                BranchSpec("br_first", "first goal", True, "c"),
                BranchSpec("br_second", "second goal", True, "c"),
                BranchSpec("br_third", "third goal", True, "c"),
            ),
        )
        projected = project_proposed_action(source)
        positions = [projected.index(spec.goal) for spec in source.branches]
        assert positions == sorted(positions)
        assert "br_first" in projected and "br_third" in projected

    def test_every_goal_appears_verbatim(self) -> None:
        source = plan(
            mode=ExecutionMode.MULTI_AGENT,
            branches=(reversible_branch("br_a"), reversible_branch("br_b")),
        )
        projected = project_proposed_action(source)
        for spec in source.branches:
            assert spec.goal in projected, spec.branch_id

    def test_it_does_not_re_summarize_into_another_plan(self) -> None:
        """A8.3 ①：只搬运 Plan 自己的文本；不得出现 Plan 里没有的措辞。

        做法：把 ``main_thread_duty`` 换成一个独有哨兵串，
        投影结果必须**包含**它 —— "重新总结"会把它改写成别的说法。
        """
        sentinel = "SENTINEL-DUTY-7f3a"
        source = Plan(
            mode=ExecutionMode.DIRECT,
            main_thread_duty=sentinel,
            reason="r",
        )
        assert sentinel in project_proposed_action(source)

    def test_exact_shape_is_only_the_plan_facts(self) -> None:
        """**实现级冻结**：输出**恰好**由固定格式 + Plan 既有事实组成。

        这是"不得重新总结 / 不得额外发明动作"的**最强行为证明** ——
        "保留原文但额外塞一个虚构动作"（例如
        ``主线程：<duty>；然后自动部署到生产环境``）会让精确相等断言变红，
        而只断言"sentinel 必须出现"是抓不到它的。

        用哨兵值把 Plan 的**其他**字段也标出来，确认它们没有被混入：
        ``reason`` / ``completion_criteria`` / ``read_only`` / ``mode``。
        """
        duty = "SENTINEL-DUTY-9c1"
        goals = ("SENTINEL-GOAL-A", "SENTINEL-GOAL-B")
        source = Plan(
            mode=ExecutionMode.MULTI_AGENT,
            main_thread_duty=duty,
            reason="SENTINEL-REASON",
            branches=(
                BranchSpec("br_one", goals[0], True, "SENTINEL-CRITERIA"),
                BranchSpec("br_two", goals[1], False, "SENTINEL-CRITERIA"),
            ),
        )
        assert project_proposed_action(source) == (
            f"主线程：{duty}；分支 br_one：{goals[0]}；分支 br_two：{goals[1]}"
        )

        projected = project_proposed_action(source)
        for forbidden in (
            "SENTINEL-REASON",
            "SENTINEL-CRITERIA",
            "multi_agent",
            "read_only",
        ):
            assert forbidden not in projected, forbidden

    def test_source_fields_are_locked_by_structure(self) -> None:
        """**结构锁**：只读 Plan 的既有陈述字段，不碰其他字段。

        | 类别 | 字段 |
        |---|---|
        | **必须读** | `main_thread_duty` / `branches` / `goal` / `branch_id` |
        | **不得读** | `reason` / `mode` / `read_only` / `completion_criteria` |

        ⚠️ 检测范围**锁在这个函数**，并排除方法调用名
        （`append` / `join`），否则断言会被无关属性污染。
        """
        reads = _data_attribute_reads("project_proposed_action")
        assert {"main_thread_duty", "branches", "goal", "branch_id"} <= reads, reads
        for forbidden in ("reason", "mode", "read_only", "completion_criteria"):
            assert forbidden not in reads, (forbidden, reads)

    def test_the_structural_detector_separates_methods_from_data(self) -> None:
        """检测器本身的**非平凡性**：方法名不得混进数据属性集合。"""
        assert _method_call_names("project_proposed_action") == {"append", "join"}
        assert not (
            _method_call_names("project_proposed_action")
            & _data_attribute_reads("project_proposed_action")
        )

    def test_it_is_deterministic(self) -> None:
        source = plan(
            mode=ExecutionMode.MULTI_AGENT,
            branches=(reversible_branch("br_a"), reversible_branch("br_b")),
        )
        assert project_proposed_action(source) == project_proposed_action(source)

    def test_it_rejects_a_non_plan(self) -> None:
        with pytest.raises(TypeError):
            project_proposed_action("nope")  # type: ignore[arg-type]


# ==========================================================================
# A8.3 ② —— impact_scope 是 Evaluation 的单字段投影
# ==========================================================================


class TestImpactScopeProjection:
    @pytest.mark.parametrize(
        "scope",
        [ImpactScope.LOCAL, ImpactScope.CROSS_MODULE, ImpactScope.SYSTEM_WIDE],
    )
    def test_it_projects_the_evaluation_field(self, scope) -> None:
        source = evaluation(impact_scope=scope)
        assert project_impact_scope(source) == source.impact_scope.value

    def test_it_is_not_derived_from_branch_counts(self) -> None:
        """A8.3 ②：影响范围**不得**从 branch 数量 / 可写分支数二次推导。

        结构锁：本函数**只**读 ``impact_scope`` 一个字段。
        """
        reads: set[str] = set()
        for node in ast.walk(_MODULE_TREE):
            if (
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == "project_impact_scope"
            ):
                reads = {
                    child.attr
                    for child in ast.walk(node)
                    if isinstance(child, ast.Attribute)
                }
        assert reads == {"impact_scope", "value"}, reads

    def test_it_does_not_read_the_plan_at_all(self) -> None:
        """本函数签名里没有 Plan —— 影响范围与分支形状无关。"""
        import inspect

        parameters = set(inspect.signature(project_impact_scope).parameters)
        assert parameters == {"evaluation"}

    def test_it_rejects_a_non_evaluation(self) -> None:
        with pytest.raises(TypeError):
            project_impact_scope(plan())  # type: ignore[arg-type]


# ==========================================================================
# A8.3 ③ —— recovery_method 的三分支
# ==========================================================================


class TestRecoveryMethodBranches:
    def test_branch_1_an_explicit_fact_is_projected_verbatim(self) -> None:
        explicit = "restore the snapshot taken before the migration"
        assert (
            resolve_recovery_method(risk(), recovery_method=explicit) == explicit
        )

    def test_branch_1_wins_even_when_hard_to_revert_is_hit(self) -> None:
        """有真实事实就**原样**搬运，不被 HARD_TO_REVERT 覆盖。"""
        explicit = "rotate credentials and redeploy"
        resolved = resolve_recovery_method(
            risk(hard_to_revert_operation=True), recovery_method=explicit
        )
        assert resolved == explicit

    def test_branch_2_hard_to_revert_without_a_fact_is_explicit_unknown(self) -> None:
        assert (
            resolve_recovery_method(risk(hard_to_revert_operation=True))
            == HARD_TO_REVERT_UNKNOWN
        )

    def test_branch_2_text_is_the_frozen_wording(self) -> None:
        assert HARD_TO_REVERT_UNKNOWN == "未提供具体恢复方式；该操作被标记为难以恢复。"

    def test_branch_2_must_not_claim_no_reliable_recovery_exists(self) -> None:
        """A8.3：`HARD_TO_REVERT` 只证明"**难以**恢复"，**不能**升级为更强的断言。"""
        resolved = resolve_recovery_method(risk(hard_to_revert_operation=True))
        for forbidden in ("无可靠恢复方式", "无法恢复", "不可恢复", "不存在恢复"):
            assert forbidden not in resolved, forbidden

    def test_branch_3_no_fact_and_no_hard_to_revert_is_an_explicit_failure(
        self,
    ) -> None:
        with pytest.raises(DisclosureProvenanceError):
            resolve_recovery_method(risk())

    def test_branch_3_never_fabricates_a_default_method(self) -> None:
        """**A8 的核心**：不得默认 `"git revert"` 之类。"""
        with pytest.raises(DisclosureProvenanceError) as info:
            resolve_recovery_method(risk())
        assert not info.value.args or "git revert" not in str(info.value)

    def test_a_blank_explicit_fact_is_refused_not_treated_as_unknown(self) -> None:
        for blank in ("", "   ", "\n"):
            with pytest.raises(DisclosureProvenanceError):
                resolve_recovery_method(risk(), recovery_method=blank)

    def test_a_non_str_explicit_fact_is_a_type_error(self) -> None:
        with pytest.raises(TypeError):
            resolve_recovery_method(risk(), recovery_method=42)  # type: ignore[arg-type]

    def test_it_rejects_a_non_risk_assessment(self) -> None:
        with pytest.raises(TypeError):
            resolve_recovery_method("nope")  # type: ignore[arg-type]


# ==========================================================================
# 组合入口
# ==========================================================================


class TestBuildDisclosure:
    def test_it_builds_the_frozen_dto(self) -> None:
        built = build_disclosure(
            plan=plan(),
            evaluation=evaluation(),
            risk_assessment=risk(),
            recovery_method="restore from backup",
        )
        assert isinstance(built, ApprovalDisclosure)

    def test_it_applies_all_three_rules(self) -> None:
        built = build_disclosure(
            plan=plan(
                mode=ExecutionMode.MULTI_AGENT,
                branches=(reversible_branch("br_a"), reversible_branch("br_b")),
            ),
            evaluation=evaluation(impact_scope=ImpactScope.SYSTEM_WIDE),
            risk_assessment=risk(hard_to_revert_operation=True),
        )
        assert "br_a" in built.proposed_action
        assert built.impact_scope == "system_wide"
        assert built.recovery_method == HARD_TO_REVERT_UNKNOWN

    def test_it_propagates_the_explicit_recovery_fact(self) -> None:
        built = build_disclosure(
            plan=plan(),
            evaluation=evaluation(),
            risk_assessment=risk(),
            recovery_method="restore from backup",
        )
        assert built.recovery_method == "restore from backup"

    def test_it_fails_explicitly_when_recovery_has_no_source(self) -> None:
        with pytest.raises(DisclosureProvenanceError):
            build_disclosure(
                plan=plan(), evaluation=evaluation(), risk_assessment=risk()
            )

    def test_the_fourth_item_is_not_built_here(self) -> None:
        """`main_risk` 由 Judgment 推导（A6.5 / A7.5），**不**属本模块。"""
        field_names = {f.name for f in dataclasses.fields(ApprovalDisclosure)}
        assert field_names == {"proposed_action", "impact_scope", "recovery_method"}

    def test_disclosure_is_frozen_and_non_empty(self) -> None:
        built = build_disclosure(
            plan=plan(),
            evaluation=evaluation(),
            risk_assessment=risk(),
            recovery_method="restore from backup",
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            built.impact_scope = "x"  # type: ignore[misc]
        for field in dataclasses.fields(ApprovalDisclosure):
            assert getattr(built, field.name).strip(), field.name

    def test_the_builder_refuses_to_fill_the_checklist_by_guessing(self) -> None:
        """**A8 的核心**：来源缺失时宁可失败，也不让清单"看起来填满了"。"""
        with pytest.raises(DisclosureProvenanceError):
            build_disclosure(
                plan=plan(), evaluation=evaluation(), risk_assessment=risk()
            )


# ==========================================================================
# 边界
# ==========================================================================


class TestDisclosureBoundaries:
    def test_it_does_not_import_the_judgment_module(self) -> None:
        """方向：`approval_judgment` 依赖本模块的 DTO，**反向边不得存在**。"""
        assert "approval_judgment" not in _imported_modules()
        assert "ApprovalJudge" not in _imported_names()

    def test_it_does_not_make_approval_decisions(self) -> None:
        """本模块只搬事实，不判审批。"""
        for forbidden in ("needs_approval", "ApprovalJudgment", "require_approval"):
            assert forbidden not in _imported_names(), forbidden
            assert forbidden not in {
                n.id for n in ast.walk(_MODULE_TREE) if isinstance(n, ast.Name)
            }, forbidden

    def test_it_does_not_import_planner_model_router_or_cost(self) -> None:
        modules = _imported_modules()
        for forbidden in ("planner", "model_router", "cost", "session", "risk"):
            assert forbidden not in modules, forbidden

    def test_it_does_not_read_files_or_environment(self) -> None:
        modules = _imported_modules()
        for forbidden in ("pathlib", "os", "subprocess", "json", "socket"):
            assert forbidden not in modules, forbidden

    def test_the_dto_identity_is_shared_with_the_judgment_module(self) -> None:
        from adaptive_router import approval_judgment as judge_module

        assert judge_module.ApprovalDisclosure is ApprovalDisclosure
        assert "ApprovalDisclosure" in judge_module.__all__
