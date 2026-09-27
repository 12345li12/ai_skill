"""Approval Judgment 行为测试（Step 5B.4.b）。

架构依据：
  `docs/architecture.md` 2 节 ``Approval Judgment``
  —— **Amendment A6**（approval-policy seam，FROZEN）
  —— **Amendment A7**（ordinary 谓词，FROZEN）

⚠️ **本文件不引入新的 Test ID** —— 冻结矩阵（架构第 8 节）里 Approval Judgment
   相关的 `TEST-RISK-00x` / `TEST-APPROVAL-001` 承载在别处
   （契约与流程测试），因此这里只断言本节点的组合不变式，不假装承载某个 ID。
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
import pathlib

import pytest

from adaptive_router.approval_constraints import EffectiveApprovalConstraints
from adaptive_router.approval_judgment import (
    NO_CRITERION_EXPLANATION,
    POLICY_ONLY_EXPLANATION,
    ApprovalDisclosure,
    ApprovalJudge,
)
from adaptive_router.contracts import (
    ApprovalJudgment,
    AuthorizationContext,
    BranchSpec,
    Confidence,
    CostBasis,
    CostEstimate,
    CostMagnitude,
    ExecutionMode,
    ModelSelection,
    ModelTier,
    EffortLevel,
    Plan,
    RiskAssessment,
    RiskCategory,
    RiskLevel,
)
from adaptive_router.routing_plan import RoutingPlanAssembler

from _import_detectors import imported_module_tokens, imported_names

MODULE_PATH = (
    pathlib.Path(__file__).resolve().parents[2]
    / "src/adaptive_router/approval_judgment.py"
)
MODULE_SOURCE = MODULE_PATH.read_text(encoding="utf-8")
_MODULE_TREE = ast.parse(MODULE_SOURCE)


def _imported_modules() -> set[str]:
    """被测模块的 import token（保守检测器，见 `_import_detectors`）。"""
    return imported_module_tokens(MODULE_SOURCE)


def _imported_names() -> set[str]:
    return imported_names(MODULE_SOURCE)


def _identifier_names() -> set[str]:
    return {n.id for n in ast.walk(_MODULE_TREE) if isinstance(n, ast.Name)}


def _attribute_reads(function_name: str) -> set[str]:
    for node in ast.walk(_MODULE_TREE):
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == function_name
        ):
            return {
                child.attr for child in ast.walk(node) if isinstance(child, ast.Attribute)
            }
    raise AssertionError(f"源码里找不到函数 {function_name}")


# --------------------------------------------------------------------------
# 测试替身
# --------------------------------------------------------------------------


def selection() -> ModelSelection:
    return ModelSelection(tier=ModelTier.STRONG, effort=EffortLevel.HIGH, reason="t")


def routing(*, branches: tuple[BranchSpec, ...] = (), mode: ExecutionMode | None = None):
    if mode is None:
        mode = ExecutionMode.MULTI_AGENT if branches else ExecutionMode.DIRECT
    plan = Plan(
        mode=mode,
        main_thread_duty="execute on the main thread",
        reason="test",
        branches=branches,
    )
    chosen = selection()
    return RoutingPlanAssembler(
        run_id_factory=lambda: "run_fixed",
        main_thread_branch_id_factory=lambda: "br_main",
    ).assemble(
        plan,
        main_thread_model=chosen,
        branch_models=tuple((spec.branch_id, chosen) for spec in plan.branches),
    )


def writable_branch(branch_id: str = "br_w") -> BranchSpec:
    return BranchSpec(
        branch_id=branch_id,
        goal="write something",
        read_only=False,
        completion_criteria="done",
    )


def read_only_branch(branch_id: str = "br_r") -> BranchSpec:
    return BranchSpec(
        branch_id=branch_id,
        goal="read something",
        read_only=True,
        completion_criteria="done",
    )


def risk(
    level: RiskLevel = RiskLevel.LOW,
    categories: tuple[RiskCategory, ...] = (),
) -> RiskAssessment:
    return RiskAssessment(level=level, categories=categories, detail="test detail")


def cost(magnitude: CostMagnitude = CostMagnitude.LOW) -> CostEstimate:
    return CostEstimate(
        basis=CostBasis.UNKNOWN,
        magnitude=magnitude,
        confidence=Confidence.LOW,
        drivers=("tier=strong",),
    )


def constraints(
    require_approval_for_all: bool = False,
    rationale: str = "no higher-priority restriction on requiring approval",
) -> EffectiveApprovalConstraints:
    return EffectiveApprovalConstraints(
        require_approval_for_all=require_approval_for_all, rationale=rationale
    )


def disclosure() -> ApprovalDisclosure:
    return ApprovalDisclosure(
        proposed_action="apply the patch",
        impact_scope="2 files",
        recovery_method="git revert",
    )


def authorization(
    *,
    session_authorized: bool = True,
    user_read_only_override: bool = False,
    denied: tuple[str, ...] = (),
) -> AuthorizationContext:
    """构造合法的 ``AuthorizationContext``（契约要求 effective ⊇ system ∪ platform ∪ project）。"""
    return AuthorizationContext(
        session_authorized=session_authorized,
        user_granted_scopes=(),
        system_denied=denied,
        project_denied=(),
        platform_denied=(),
        effective_denied=denied,
        user_read_only_override=user_read_only_override,
        rationale="test authorization context",
    )


def judge(
    *,
    risk_assessment: RiskAssessment | None = None,
    cost_estimate: CostEstimate | None = None,
    routing_plan=None,
    authorization_context: AuthorizationContext | None = None,
    constraint_set: EffectiveApprovalConstraints | None = None,
    view: ApprovalDisclosure | None = None,
) -> ApprovalJudgment:
    return ApprovalJudge().judge(
        risk_assessment if risk_assessment is not None else risk(),
        cost_estimate if cost_estimate is not None else cost(),
        authorization_context if authorization_context is not None else authorization(),
        routing_plan if routing_plan is not None else routing(),
        constraints=constraint_set if constraint_set is not None else constraints(),
        disclosure=view if view is not None else disclosure(),
    )


# ==========================================================================
# A6.4 —— True / False 的语义
# ==========================================================================


class TestA6TrueForcesFalseDoesNotSuppress:
    def test_the_frozen_output_type_is_produced(self) -> None:
        assert isinstance(judge(), ApprovalJudgment)

    def test_clean_inputs_do_not_need_approval(self) -> None:
        assert judge().needs_approval is False

    def test_true_forces_approval_even_when_nothing_ordinary_hits(self) -> None:
        result = judge(constraint_set=constraints(True))
        assert result.needs_approval is True

    def test_true_never_turns_an_ordinary_hit_off(self) -> None:
        for overrides in (
            {"risk_assessment": risk(level=RiskLevel.HIGH, categories=(RiskCategory.EXTERNAL_WRITE,))},
            {"cost_estimate": cost(CostMagnitude.HIGH)},
            {"routing_plan": routing(branches=(writable_branch(), read_only_branch()))},
        ):
            assert judge(constraint_set=constraints(False), **overrides).needs_approval is True
            assert judge(constraint_set=constraints(True), **overrides).needs_approval is True

    def test_false_is_bit_identical_to_the_ordinary_path(self) -> None:
        """A6.4：`False` 时必须与"没有这条 seam"**逐位相同**。

        本节点没有"没有 seam"的入口（缺参是显式失败），
        因此等价性用 `needs_approval == ordinary_needs_approval(...)`
        与 `main_risk` 不出现策略文案来锁。
        """
        cases = (
            {},
            {"risk_assessment": risk(level=RiskLevel.HIGH, categories=(RiskCategory.SCOPE_EXPANSION,))},
            {"cost_estimate": cost(CostMagnitude.HIGH)},
            {"cost_estimate": cost(CostMagnitude.MEDIUM)},
            {"routing_plan": routing(branches=(writable_branch(), read_only_branch()))},
            {"routing_plan": routing(branches=(read_only_branch("br_a"), read_only_branch("br_b")))},
        )
        for overrides in cases:
            risk_assessment = overrides.get("risk_assessment", risk())
            cost_estimate = overrides.get("cost_estimate", cost())
            routing_plan = overrides.get("routing_plan", routing())
            expected = ApprovalJudge.ordinary_needs_approval(
                risk_assessment, cost_estimate, routing_plan
            )
            result = judge(
                risk_assessment=risk_assessment,
                cost_estimate=cost_estimate,
                routing_plan=routing_plan,
                constraint_set=constraints(False),
            )
            assert result.needs_approval is expected, overrides
            assert POLICY_ONLY_EXPLANATION not in result.main_risk, overrides

    def test_false_dot_not_mean_exempt_from_approval(self) -> None:
        """`False` 只表示"不强制全部审批"（A6.4）。"""
        field_names = {f.name for f in dataclasses.fields(ApprovalJudgment)}
        for forbidden in ("skip_approval", "approval_not_required"):
            assert forbidden not in field_names, forbidden
        assert judge(constraint_set=constraints(False)).needs_approval is False
        assert (
            judge(
                constraint_set=constraints(False),
                cost_estimate=cost(CostMagnitude.HIGH),
            ).needs_approval
            is True
        )


# ==========================================================================
# A6.4 —— 缺失 / None / 类型错误必须显式失败
# ==========================================================================


class TestMissingSeamFailsExplicitly:
    def test_constraints_is_a_required_keyword_only_parameter(self) -> None:
        parameter = inspect.signature(ApprovalJudge.judge).parameters["constraints"]
        assert parameter.default is inspect.Parameter.empty
        assert parameter.kind is inspect.Parameter.KEYWORD_ONLY

    def test_omitting_constraints_is_a_type_error(self) -> None:
        with pytest.raises(TypeError):
            ApprovalJudge().judge(  # type: ignore[call-arg]
                risk(), cost(), routing(), disclosure=disclosure()
            )

    def test_none_constraints_is_a_type_error(self) -> None:
        with pytest.raises(TypeError):
            ApprovalJudge().judge(
                risk(),
                cost(),
                routing(),
                constraints=None,  # type: ignore[arg-type]
                disclosure=disclosure(),
            )

    def test_wrong_type_constraints_is_a_type_error(self) -> None:
        for wrong in (True, "yes", 0):
            with pytest.raises(TypeError):
                judge(constraint_set=wrong)  # type: ignore[arg-type]

    def test_authorization_is_a_required_validated_input(self) -> None:
        """冻结输入行成员：必须接收、必须校验（A7.5 只移除它的**肯定项**作用）。"""
        parameters = inspect.signature(ApprovalJudge.judge).parameters
        assert parameters["authorization"].default is inspect.Parameter.empty
        with pytest.raises(TypeError):
            ApprovalJudge().judge(  # type: ignore[call-arg]
                risk(), cost(), routing(), constraints=constraints(), disclosure=disclosure()
            )
        for wrong in (None, "authorized", True):
            with pytest.raises(TypeError):
                ApprovalJudge().judge(
                    risk(),
                    cost(),
                    wrong,  # type: ignore[arg-type]
                    routing(),
                    constraints=constraints(),
                    disclosure=disclosure(),
                )

    def test_policy_only_predicate_also_refuses_missing_constraints(self) -> None:
        with pytest.raises(TypeError):
            ApprovalJudge.policy_only_approval(
                risk(), cost(), routing(), constraints=None  # type: ignore[arg-type]
            )

    def test_no_default_may_be_read_as_either_boolean(self) -> None:
        """两种默认都被 A6.4 排除：`→False` 是 fail-open；`→True` 会掩盖 wiring bug。"""
        module_defaults = {
            name: getattr(ApprovalJudge, name, None)
            for name in dir(ApprovalJudge)
            if "default" in name.lower() or "unrestricted" in name.lower()
        }
        assert module_defaults == {}


# ==========================================================================
# A7.2 / A7.2.1 —— 三个子判据
# ==========================================================================


class TestA7RiskHit:
    def test_an_l41_category_hit_counts(self) -> None:
        assert ApprovalJudge.risk_hit(
            risk(level=RiskLevel.MEDIUM, categories=(RiskCategory.SHARED_INFRASTRUCTURE,))
        )

    def test_a_high_level_without_categories_counts(self) -> None:
        assert ApprovalJudge.risk_hit(risk(level=RiskLevel.HIGH)) is True

    def test_a_low_level_without_categories_does_not_count(self) -> None:
        assert ApprovalJudge.risk_hit(risk()) is False

    def test_it_reuses_the_frozen_helper_instead_of_copying_the_rule(
        self, monkeypatch
    ) -> None:
        """**A7.2.1 的核心**：`HIGH` 的定义由 Step 5A 的 helper 拥有。

        把 helper 换成返回 `True` 的替身：
        一个 `categories` 为空、`level == LOW` 的评估**也**必须随之命中 ——
        这只有在"真的调用了 helper"时才成立。
        """
        from adaptive_router import approval_judgment as module

        low = risk(level=RiskLevel.LOW)
        assert ApprovalJudge.risk_hit(low) is False  # 前置

        monkeypatch.setattr(module, "requires_human_input", lambda assessment: True)
        assert ApprovalJudge.risk_hit(low) is True

    def test_the_module_imports_the_helper(self) -> None:
        assert "requires_human_input" in _imported_names()
        assert "risk" in _imported_modules()

    def test_it_does_not_reimplement_the_high_rule(self) -> None:
        """不得出现 `level is HIGH` 之类的**复制**（那是 helper 的职责）。

        ⚠️ 用 **AST**，不做子串扫描：源码 docstring 会**说明**
        "不复制 `level == HIGH`"，那是文档，不是规则。
        （同类假红此前已记录在 README_step5b §6.1 / §6.3 / §6.5 / §14.4。）
        """
        assert "RiskLevel" not in _imported_names()
        assert "level" not in _attribute_reads("risk_hit")
        assert "RiskLevel" not in _identifier_names()


class TestA7CostHit:
    @pytest.mark.parametrize(
        ("magnitude", "expected"),
        [
            (CostMagnitude.LOW, False),
            (CostMagnitude.MEDIUM, False),
            (CostMagnitude.HIGH, True),
        ],
    )
    def test_only_high_counts(self, magnitude, expected) -> None:
        assert ApprovalJudge.cost_hit(cost(magnitude)) is expected

    def test_confidence_is_not_part_of_the_rule(self) -> None:
        """不得叠加 confidence —— `_derive` 恒返回 LOW，叠加会让规则永不触发。

        ⚠️ 用 **AST** 断言，不做子串扫描：源码 docstring 会**解释**
        "为什么不叠 confidence"，那是文档，不是规则。
        """
        assert "Confidence" not in _imported_names()
        assert "confidence" not in _attribute_reads("cost_hit")
        assert "confidence" not in _identifier_names()


class TestA7SideEffectHit:
    def test_a_writable_child_counts(self) -> None:
        assert ApprovalJudge.side_effect_hit(
            routing(branches=(writable_branch(), read_only_branch()))
        )

    def test_all_read_only_children_do_not_count(self) -> None:
        assert (
            ApprovalJudge.side_effect_hit(
                routing(branches=(read_only_branch("br_a"), read_only_branch("br_b")))
            )
            is False
        )

    @pytest.mark.parametrize(
        "mode", [ExecutionMode.DIRECT, ExecutionMode.STAGED_SINGLE_AGENT]
    )
    def test_zero_branch_plans_do_not_count(self, mode) -> None:
        assert ApprovalJudge.side_effect_hit(routing(mode=mode)) is False

    def test_zero_branch_must_not_be_explained_as_no_side_effects(self) -> None:
        """**A7.4 的关键**：0 child 只表示"本维度没有额外证据"。

        契约要求 `main_risk` 非空，因此解释文本必然存在 ——
        但它**不得**声称"无副作用"。
        """
        result = judge(routing_plan=routing(mode=ExecutionMode.DIRECT))
        assert result.needs_approval is False
        for forbidden in ("无副作用", "没有副作用", "no side effect"):
            assert forbidden not in result.main_risk, forbidden


class TestA7OrdinaryComposition:
    def test_ordinary_is_the_disjunction_of_three_hits(self) -> None:
        cases = (
            (risk(level=RiskLevel.HIGH), cost(), routing()),
            (risk(), cost(CostMagnitude.HIGH), routing()),
            (risk(), cost(), routing(branches=(writable_branch(), read_only_branch()))),
        )
        for risk_assessment, cost_estimate, routing_plan in cases:
            assert ApprovalJudge.ordinary_needs_approval(
                risk_assessment, cost_estimate, routing_plan
            )
            assert judge(
                risk_assessment=risk_assessment,
                cost_estimate=cost_estimate,
                routing_plan=routing_plan,
            ).needs_approval

    def test_ordinary_excludes_the_authorization_dimension(self) -> None:
        """**A7.5**：授权维度不产生肯定项 —— 但它是**冻结输入面**的一员。

        三层锁：

        1. 顶层 ``judge()`` **必须**接收 ``authorization``（冻结输入行）；
        2. 三个 ordinary 子判据**签名里不得**出现授权参数，
           且它们的函数体**不得**读取任何授权字段；
        3. 改变授权事实**不得**改变 ``needs_approval``。
        """
        # 1) 顶层必须接收
        parameters = inspect.signature(ApprovalJudge.judge).parameters
        assert "authorization" in parameters
        assert parameters["authorization"].default is inspect.Parameter.empty

        # 2) 子判据不接触授权
        for predicate in (
            ApprovalJudge.ordinary_needs_approval,
            ApprovalJudge.risk_hit,
            ApprovalJudge.cost_hit,
            ApprovalJudge.side_effect_hit,
        ):
            names = set(inspect.signature(predicate).parameters)
            assert not any("author" in n.lower() for n in names), predicate
        for function in (
            "ordinary_needs_approval",
            "risk_hit",
            "cost_hit",
            "side_effect_hit",
        ):
            for forbidden in (
                "is_read_only",
                "effective_denied",
                "session_authorized",
                "user_read_only_override",
            ):
                assert forbidden not in _attribute_reads(function), (function, forbidden)

    def test_authorization_alone_cannot_flip_false_to_true(self) -> None:
        """**A7.5 的关键**：授权事实单独**不能**让 `needs_approval` 变真。

        遍历授权状态的组合：已授权 / 未授权、只读 / 可写、有拒绝项 / 无拒绝项 ——
        三个 ordinary 判据全不命中时，结果必须**恒为 `False`**。
        """
        contexts = (
            authorization(),
            authorization(session_authorized=False),
            authorization(user_read_only_override=True),
            authorization(denied=("write", "deploy")),
            authorization(session_authorized=False, user_read_only_override=True),
        )
        for context in contexts:
            result = judge(authorization_context=context)
            assert result.needs_approval is False, context
            assert result.main_risk == NO_CRITERION_EXPLANATION, context

    def test_authorization_does_not_suppress_or_amplify_other_criteria(self) -> None:
        """A7.5：其他判据**各自独立**；"已授权 + 只读"不是全局免审批。"""
        hits = (
            {"risk_assessment": risk(level=RiskLevel.HIGH, categories=(RiskCategory.EXTERNAL_WRITE,))},
            {"cost_estimate": cost(CostMagnitude.HIGH)},
            {"routing_plan": routing(branches=(writable_branch(), read_only_branch()))},
        )
        authorized_read_only = authorization(user_read_only_override=True)
        for view in hits:
            baseline = judge(**view)
            with_context = judge(authorization_context=authorized_read_only, **view)
            assert baseline.needs_approval is True, view
            assert with_context.needs_approval is True, view
            assert with_context.main_risk == baseline.main_risk, view


# ==========================================================================
# A6.5 / A7.5 —— policy_only_approval 与 main_risk 的两套规则
# ==========================================================================


class TestA65PolicyOnly:
    def test_exact_predicate_is_policy_and_not_ordinary(self) -> None:
        assert ApprovalJudge.policy_only_approval(
            risk(), cost(), routing(), constraints=constraints(True)
        )
        assert not ApprovalJudge.policy_only_approval(
            risk(), cost(), routing(), constraints=constraints(False)
        )

    def test_must_not_be_replaced_by_risk_not_hit(self) -> None:
        """**反例**：`Risk LOW` + 显著 Cost ⇒ 存在真实普通理由，不是 policy-only。"""
        assert ApprovalJudge.risk_hit(risk()) is False  # Risk 未命中
        assert ApprovalJudge.cost_hit(cost(CostMagnitude.HIGH)) is True
        assert not ApprovalJudge.policy_only_approval(
            risk(), cost(CostMagnitude.HIGH), routing(), constraints=constraints(True)
        )

    def test_policy_only_produces_the_frozen_explanation(self) -> None:
        result = judge(constraint_set=constraints(True))
        assert result.needs_approval is True
        assert result.main_risk == POLICY_ONLY_EXPLANATION

    def test_policy_only_explanation_does_not_fabricate_risk(self) -> None:
        result = judge(constraint_set=constraints(True))
        assert result.main_risk.strip()
        for forbidden in ("风险类别命中", "显著额外费用", "可写分支"):
            assert forbidden not in result.main_risk, forbidden


class TestA65MainRisk:
    def test_an_ordinary_hit_is_reported_truthfully(self) -> None:
        assert "external_write" in judge(
            risk_assessment=risk(
                level=RiskLevel.HIGH, categories=(RiskCategory.EXTERNAL_WRITE,)
            )
        ).main_risk
        assert "high" in judge(cost_estimate=cost(CostMagnitude.HIGH)).main_risk
        assert "br_w" in judge(
            routing_plan=routing(branches=(writable_branch(), read_only_branch()))
        ).main_risk

    def test_the_real_reason_is_never_overwritten_by_policy_rationale(self) -> None:
        """**A6.5 的核心**：策略强制**不得**抹掉真实的普通审批理由。"""
        result = judge(
            cost_estimate=cost(CostMagnitude.HIGH),
            constraint_set=constraints(True, rationale="user: 用户当前指令 APPROVE_ALL（L45）"),
        )
        assert result.needs_approval is True
        assert result.main_risk != POLICY_ONLY_EXPLANATION
        assert "显著额外费用" in result.main_risk

    def test_the_policy_rationale_is_not_dumped_verbatim(self) -> None:
        """A6.6：可用 rationale 形成说明，但**不得**要求原样输出。"""
        rationale = "system: approval_mode 被该层（system_denied）限制（L11）"
        result = judge(constraint_set=constraints(True, rationale=rationale))
        assert rationale not in result.main_risk

    def test_nothing_hit_is_reported_as_nothing_hit(self) -> None:
        assert judge().main_risk == NO_CRITERION_EXPLANATION

    def test_main_risk_is_never_blank_in_any_path(self) -> None:
        for view in (
            {},
            {"risk_assessment": risk(level=RiskLevel.HIGH)},
            {"cost_estimate": cost(CostMagnitude.HIGH)},
            {"routing_plan": routing(branches=(writable_branch(), read_only_branch()))},
        ):
            for forced in (False, True):
                result = judge(constraint_set=constraints(forced), **view)
                assert result.main_risk.strip(), (view, forced)


# ==========================================================================
# 输出契约与清单
# ==========================================================================


class TestChecklistAndContract:
    def test_the_contract_still_has_exactly_five_fields(self) -> None:
        """A6.7 / A7.7：`ApprovalJudgment` 五字段未改。"""
        assert {f.name for f in dataclasses.fields(ApprovalJudgment)} == {
            "needs_approval",
            "proposed_action",
            "impact_scope",
            "main_risk",
            "recovery_method",
        }

    def test_the_checklist_has_exactly_four_items(self) -> None:
        assert len(judge().checklist) == 4

    def test_the_checklist_carries_the_disclosure(self) -> None:
        result = judge()
        assert result.checklist == (
            "apply the patch",
            "2 files",
            result.main_risk,
            "git revert",
        )

    def test_disclosure_requires_non_empty_strings(self) -> None:
        for field in ("proposed_action", "impact_scope", "recovery_method"):
            for blank in ("", "   "):
                with pytest.raises(ValueError):
                    ApprovalDisclosure(**{**dataclasses.asdict(disclosure()), field: blank})
            with pytest.raises(TypeError):
                ApprovalDisclosure(**{**dataclasses.asdict(disclosure()), field: None})

    def test_disclosure_is_frozen(self) -> None:
        view = disclosure()
        with pytest.raises(dataclasses.FrozenInstanceError):
            view.proposed_action = "x"  # type: ignore[misc]

    def test_judge_refuses_a_non_disclosure(self) -> None:
        with pytest.raises(TypeError):
            judge(view="nope")  # type: ignore[arg-type]

    def test_judgment_is_deterministic(self) -> None:
        assert judge() == judge()
        assert judge(constraint_set=constraints(True)) == judge(
            constraint_set=constraints(True)
        )

    def test_inputs_are_not_mutated(self) -> None:
        risk_assessment, cost_estimate, routing_plan = risk(), cost(), routing()
        before = (
            dataclasses.asdict(risk_assessment),
            dataclasses.asdict(cost_estimate),
            dataclasses.asdict(routing_plan.plan),
        )
        judge(
            risk_assessment=risk_assessment,
            cost_estimate=cost_estimate,
            routing_plan=routing_plan,
        )
        assert (
            dataclasses.asdict(risk_assessment),
            dataclasses.asdict(cost_estimate),
            dataclasses.asdict(routing_plan.plan),
        ) == before


# ==========================================================================
# 边界
# ==========================================================================


class TestJudgmentBoundaries:
    def test_never_imports_the_whole_policy_module(self) -> None:
        """只依赖中性 seam DTO，不依赖整个 `policy.py`（A6.3）。"""
        assert "policy" not in _imported_modules()
        assert "policy" not in _identifier_names()
        assert "approval_constraints" in _imported_modules()

    def test_does_not_touch_the_gate_or_the_contract_layer_surface(self) -> None:
        """只**产出**契约类型，不改契约；也不实现 Gate 的暂停/索取能力。"""
        modules = _imported_modules()
        assert "contracts" in modules
        public = {name for name in dir(ApprovalJudge) if not name.startswith("_")}
        for forbidden in ("pause", "wait", "ask", "prompt", "gate"):
            assert not any(forbidden in name.lower() for name in public), forbidden

    def test_does_not_read_files_or_environment(self) -> None:
        modules = _imported_modules()
        for forbidden in ("pathlib", "os", "subprocess", "json", "socket"):
            assert forbidden not in modules, forbidden

    def test_does_not_import_planner_or_model_router(self) -> None:
        modules = _imported_modules()
        for forbidden in ("planner", "model_router", "cost", "session", "entrypoint"):
            assert forbidden not in modules, forbidden

    def test_declared_input_rows_are_actually_consumed_or_documented(self) -> None:
        """架构输入行里的 `RiskAssessment` / `CostEstimate` / `RoutingPlan` 副作用
        都必须真实参与判断（不得有只声明不使用的输入）。

        ⚠️ `AuthorizationContext` 是**有意不参与**的 —— A7.5 规定它只作
        授权收窄／解释、不产生肯定项；该决定见本节
        `test_ordinary_excludes_the_authorization_dimension`。
        """
        parameters = set(inspect.signature(ApprovalJudge.judge).parameters)
        assert {"risk_assessment", "cost_estimate", "routing"} <= parameters