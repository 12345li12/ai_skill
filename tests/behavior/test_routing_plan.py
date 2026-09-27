"""``RoutingPlan`` 装配层行为测试（Step 5B.2）。

架构依据：
  `docs/architecture.md` 2 节 ``[06] Model Router`` —— ``RoutingPlan`` 归属 **[06]**，
  不可变、交给 Adapter（INV-03）。
  契约 ``contracts/decision.py::RoutingPlan`` 是 ``Plan``（[05]）与
  ``ModelSelection``（[06]）的**唯一**汇合点。

⚠️ **本文件不引入新的 Test ID** —— 冻结矩阵（架构第 8 节）里没有专门的
   RoutingPlan 装配 Test ID，因此这里只断言**冻结契约的装配不变式**，
   不假装自己承载了某个 Test ID。
"""

from __future__ import annotations

import pathlib

import pytest

from adaptive_router.contracts import (
    BranchSpec,
    EffortLevel,
    ExecutionMode,
    ModelSelection,
    ModelTier,
    Plan,
    RoutingPlan,
)
from adaptive_router.routing_plan import RoutingPlanAssembler, RoutingPlanAssemblyError

from _import_detectors import imported_module_tokens

MODULE_SOURCE = (
    pathlib.Path(__file__).resolve().parents[2] / "src/adaptive_router/routing_plan.py"
).read_text(encoding="utf-8")


def _imported_modules() -> set[str]:
    """被测模块的 import token（保守检测器，见 `_import_detectors`）。"""
    return imported_module_tokens(MODULE_SOURCE)


# --------------------------------------------------------------------------
# 测试替身
# --------------------------------------------------------------------------


def selection(tier: ModelTier = ModelTier.BALANCED) -> ModelSelection:
    return ModelSelection(tier=tier, effort=EffortLevel.MEDIUM, reason="test selection")


def branch(branch_id: str) -> BranchSpec:
    return BranchSpec(
        branch_id=branch_id,
        goal=f"goal {branch_id}",
        read_only=True,
        completion_criteria=f"done {branch_id}",
    )


def direct_plan() -> Plan:
    return Plan(
        mode=ExecutionMode.DIRECT,
        main_thread_duty="execute on the main thread",
        reason="simple task",
    )


def staged_plan() -> Plan:
    return Plan(
        mode=ExecutionMode.STAGED_SINGLE_AGENT,
        main_thread_duty="execute in stages",
        reason="policy no split",
    )


def multi_plan() -> Plan:
    return Plan(
        mode=ExecutionMode.MULTI_AGENT,
        main_thread_duty="coordinate branches",
        reason="independent subtasks",
        branches=(branch("br_a"), branch("br_b"), branch("br_c")),
    )


def assembler() -> RoutingPlanAssembler:
    """固定 run_id / main_thread_branch_id，让断言可重复。"""
    return RoutingPlanAssembler(
        run_id_factory=lambda: "run_fixed",
        main_thread_branch_id_factory=lambda: "br_main",
    )


# ==========================================================================
# 0 分支：DIRECT / STAGED
# ==========================================================================


class TestZeroBranchAssembly:
    @pytest.mark.parametrize("plan_factory", [direct_plan, staged_plan])
    def test_assembles_a_zero_branch_plan(self, plan_factory) -> None:
        routing = assembler().assemble(plan_factory(), main_thread_model=selection())
        assert isinstance(routing, RoutingPlan)
        assert routing.branch_models == ()
        assert routing.branch_ids == ()

    @pytest.mark.parametrize("plan_factory", [direct_plan, staged_plan])
    def test_main_thread_is_the_only_execution_unit(self, plan_factory) -> None:
        """0 分支时执行单位**只有主线程**（Cost Ledger 的 branch_id 取值域）。"""
        routing = assembler().assemble(plan_factory(), main_thread_model=selection())
        assert routing.execution_unit_ids == ("br_main",)

    def test_plan_is_carried_through_unchanged(self) -> None:
        plan = direct_plan()
        routing = assembler().assemble(plan, main_thread_model=selection())
        assert routing.plan is plan


# ==========================================================================
# MULTI_AGENT：映射必须**显式**覆盖每个 child
# ==========================================================================


class TestExplicitBranchModelMapping:
    def test_complete_mapping_is_assembled(self) -> None:
        routing = assembler().assemble(
            multi_plan(),
            main_thread_model=selection(ModelTier.LOW_COST),
            branch_models=(
                ("br_a", selection(ModelTier.STRONG)),
                ("br_b", selection(ModelTier.BALANCED)),
                ("br_c", selection(ModelTier.LOW_COST)),
            ),
        )
        assert routing.branch_ids == ("br_a", "br_b", "br_c")
        assert routing.model_for_branch("br_a").tier is ModelTier.STRONG
        assert routing.model_for_branch("br_c").tier is ModelTier.LOW_COST

    def test_each_branch_keeps_its_own_tier(self) -> None:
        """**不得**被压成"全部同模型"。"""
        routing = assembler().assemble(
            multi_plan(),
            main_thread_model=selection(),
            branch_models=(
                ("br_a", selection(ModelTier.LOW_COST)),
                ("br_b", selection(ModelTier.STRONG)),
                ("br_c", selection(ModelTier.MAXIMUM)),
            ),
        )
        tiers = {routing.model_for_branch(b) .tier for b in routing.branch_ids}
        assert tiers == {ModelTier.LOW_COST, ModelTier.STRONG, ModelTier.MAXIMUM}

    def test_missing_mapping_is_refused_not_guessed(self) -> None:
        """**核心不变式**：MULTI_AGENT 缺映射 → 显式失败，绝不假设"全部同模型"。"""
        with pytest.raises(RoutingPlanAssemblyError) as info:
            assembler().assemble(multi_plan(), main_thread_model=selection())
        message = str(info.value)
        assert "br_a" in message
        assert "显式" in message

    def test_partially_missing_mapping_is_refused(self) -> None:
        with pytest.raises(RoutingPlanAssemblyError):
            assembler().assemble(
                multi_plan(),
                main_thread_model=selection(),
                branch_models=(("br_a", selection()), ("br_b", selection())),
            )

    def test_unknown_branch_is_refused(self) -> None:
        """档位不得凭空发明分支。"""
        with pytest.raises(RoutingPlanAssemblyError):
            assembler().assemble(
                multi_plan(),
                main_thread_model=selection(),
                branch_models=(
                    ("br_a", selection()),
                    ("br_b", selection()),
                    ("br_zzz", selection()),
                ),
            )

    def test_duplicate_branch_entries_are_refused(self) -> None:
        with pytest.raises(RoutingPlanAssemblyError):
            assembler().assemble(
                multi_plan(),
                main_thread_model=selection(),
                branch_models=(
                    ("br_a", selection()),
                    ("br_a", selection()),
                    ("br_b", selection()),
                ),
            )

    def test_a_zero_branch_plan_rejects_child_models(self) -> None:
        """DIRECT / STAGED 不得携带 child 档位。"""
        with pytest.raises(RoutingPlanAssemblyError):
            assembler().assemble(
                direct_plan(),
                main_thread_model=selection(),
                branch_models=(("br_a", selection()),),
            )


# ==========================================================================
# 纯装配：确定性 / 不猜
# ==========================================================================


class TestPureAssembly:
    def test_mapping_order_does_not_change_the_result(self) -> None:
        """同一组输入，映射顺序不同 → **相同**的 ``RoutingPlan``。"""
        one = assembler().assemble(
            multi_plan(),
            main_thread_model=selection(),
            branch_models=(("br_c", selection()), ("br_a", selection()), ("br_b", selection())),
        )
        two = assembler().assemble(
            multi_plan(),
            main_thread_model=selection(),
            branch_models=(("br_a", selection()), ("br_b", selection()), ("br_c", selection())),
        )
        assert one == two
        assert [b for b, _ in one.branch_models] == ["br_a", "br_b", "br_c"]

    def test_output_order_follows_the_plan(self) -> None:
        routing = assembler().assemble(
            multi_plan(),
            main_thread_model=selection(),
            branch_models=(("br_c", selection()), ("br_b", selection()), ("br_a", selection())),
        )
        assert [b for b, _ in routing.branch_models] == [
            spec.branch_id for spec in routing.plan.branches
        ]

    def test_run_id_is_generated_and_injectable(self) -> None:
        generated = RoutingPlanAssembler().assemble(
            direct_plan(), main_thread_model=selection()
        )
        assert generated.run_id.startswith("run_")

        injected = assembler().assemble(
            direct_plan(), main_thread_model=selection(), run_id="run_explicit"
        )
        assert injected.run_id == "run_explicit"

    def test_main_thread_branch_id_is_generated_and_injectable(self) -> None:
        generated = RoutingPlanAssembler().assemble(
            direct_plan(), main_thread_model=selection()
        )
        assert generated.main_thread_branch_id.startswith("br_")

        injected = assembler().assemble(
            direct_plan(),
            main_thread_model=selection(),
            main_thread_branch_id="br_explicit",
        )
        assert injected.main_thread_branch_id == "br_explicit"

    def test_two_runs_without_injection_get_different_ids(self) -> None:
        """默认不是魔法常量 —— 每次装配是**独立**的执行单位身份。"""
        one = RoutingPlanAssembler().assemble(
            direct_plan(), main_thread_model=selection()
        )
        two = RoutingPlanAssembler().assemble(
            direct_plan(), main_thread_model=selection()
        )
        assert one.run_id != two.run_id
        assert one.main_thread_branch_id != two.main_thread_branch_id

    def test_main_thread_identity_must_not_collide_with_a_child(self) -> None:
        with pytest.raises(Exception):  # ContractError：执行单位身份必须唯一
            assembler().assemble(
                multi_plan(),
                main_thread_model=selection(),
                branch_models=(
                    ("br_a", selection()),
                    ("br_b", selection()),
                    ("br_c", selection()),
                ),
                main_thread_branch_id="br_a",
            )

    def test_input_validation(self) -> None:
        with pytest.raises(RoutingPlanAssemblyError):
            assembler().assemble("nope", main_thread_model=selection())  # type: ignore[arg-type]
        with pytest.raises(RoutingPlanAssemblyError):
            assembler().assemble(direct_plan(), main_thread_model="nope")  # type: ignore[arg-type]
        with pytest.raises(RoutingPlanAssemblyError):
            assembler().assemble(
                direct_plan(), main_thread_model=selection(), run_id=""
            )
        with pytest.raises(RoutingPlanAssemblyError):
            assembler().assemble(
                direct_plan(),
                main_thread_model=selection(),
                branch_models=[("br_a", selection())],  # type: ignore[arg-type]
            )


# ==========================================================================
# 边界：纯装配，不做决策
# ==========================================================================


class TestAssemblyBoundaries:
    def test_does_not_import_or_call_model_router(self) -> None:
        """装配层**不产生模型决策** —— 它只消费已形成的 ``ModelSelection``。

        架构上 ``RoutingPlan`` 仍属 [06] 的责任域；本模块只是其下游的纯装配实现。
        """
        modules = _imported_modules()
        assert "model_router" not in modules
        assert all("model_router" not in name for name in modules)

    def test_does_not_import_planner_or_policy(self) -> None:
        """也不重新决定分支形状或策略。"""
        modules = _imported_modules()
        for forbidden in ("planner", "policy", "risk", "cost", "session"):
            assert forbidden not in modules, forbidden

    def test_does_not_read_files_or_environment(self) -> None:
        modules = _imported_modules()
        for forbidden in ("pathlib", "os", "subprocess", "json", "socket"):
            assert forbidden not in modules, forbidden

    def test_plan_has_no_model_field_still(self) -> None:
        """``Plan`` 仍然不含档位 —— 档位只在 ``RoutingPlan`` 汇合。"""
        import dataclasses

        names = {field.name for field in dataclasses.fields(Plan)}
        for forbidden in ("tier", "model", "effort", "model_selection"):
            assert forbidden not in names, forbidden
