"""``RoutingPlan`` 装配 —— 纯装配层，不做任何决策。

架构依据：`docs/architecture.md`

- 2 节 ``[06] Model Router`` 的归属表：``RoutingPlan`` 归属 **``[06]``**，
  不可变、交给 Adapter（INV-03）。
- 契约 ``contracts/decision.py::RoutingPlan`` 是**唯一**的汇合点：
  ``Plan``（[05] 的分支形状）与 ``ModelSelection``（[06] 的抽象档位）在此汇合。

**本模块的定位（Step 5B.2 冻结）：**

``RoutingPlan`` 在架构上仍属 **``[06]`` 的责任域**（契约归属文字未变）。
本模块只是该责任域**下游的纯装配实现**，它：

- ✅ **消费**已经形成的 ``Plan``、``main_thread_model`` 与显式 ``branch_models``；
- ✅ 生成 ``run_id`` / ``main_thread_branch_id``（可注入，便于测试稳定）；
- ❌ **不 import、不调用 ``ModelRouter``** —— 它**不产生任何模型决策**；
- ❌ 不重新决定分支形状（那是 ``[05]`` 的事）；
- ❌ 不改 ``contracts/``。

**⚠️ 为什么不"顺手补全"每个 child 的档位：**

``[06]`` 当前的 ``select()`` 接受**整任务**的 ``Evaluation`` / ``RiskAssessment``，
只返回**一个** ``ModelSelection``；它**没有** per-branch 接口。
因此 child 档位映射必须由调用方**显式**给出：

- 给了 → 正确装配；
- ``MULTI_AGENT`` 却没给全 → **显式失败**（**绝不**"全部同模型"地猜一个）。

per-child 档位的**生产者**目前无主，属已登记的后续 seam，不在本步解决。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from .contracts import (
    BranchId,
    ExecutionMode,
    ModelSelection,
    Plan,
    RoutingPlan,
    RunId,
    new_branch_id,
    new_run_id,
)

__all__ = ["RoutingPlanAssemblyError", "RoutingPlanAssembler"]


class RoutingPlanAssemblyError(ValueError):
    """装配输入不足以产出合法的 ``RoutingPlan``。

    宁可显式失败，也不要替调用方猜一个档位映射 ——
    猜出来的 ``branch_models`` 会让"这个分支为什么用强档"变成不可审计的假事实。
    """


@dataclass
class RoutingPlanAssembler:
    """把已形成的 ``Plan`` + 档位映射装配成冻结的 ``RoutingPlan``。

    两个 factory 都可以注入（与 ``Planner.branch_id_factory`` 同一约定），
    以便测试得到**稳定**的 ``run_id`` / ``main_thread_branch_id``；
    生产路径用契约提供的生成器。
    """

    run_id_factory: Callable[[], str] = new_run_id
    main_thread_branch_id_factory: Callable[[], str] = new_branch_id

    def assemble(
        self,
        plan: Plan,
        *,
        main_thread_model: ModelSelection,
        branch_models: tuple[tuple[BranchId, ModelSelection], ...] = (),
        run_id: RunId | None = None,
        main_thread_branch_id: BranchId | None = None,
    ) -> RoutingPlan:
        """装配 ``RoutingPlan``。

        ``branch_models`` 必须**恰好**覆盖 ``plan.branches``：每个 child
        一个显式档位，不多不少。DIRECT / STAGED 是 0 分支，传空元组即可。
        """
        if not isinstance(plan, Plan):
            raise RoutingPlanAssemblyError(f"plan 必须是 Plan，收到 {plan!r}")
        if not isinstance(main_thread_model, ModelSelection):
            raise RoutingPlanAssemblyError(
                f"main_thread_model 必须是 ModelSelection，收到 {main_thread_model!r}"
            )
        pairs = self._validated_pairs(branch_models)
        self._require_complete_coverage(plan, pairs)
        by_branch = dict(pairs)

        return RoutingPlan(
            run_id=self._resolve(run_id, self.run_id_factory, "run_id"),
            plan=plan,
            main_thread_branch_id=self._resolve(
                main_thread_branch_id,
                self.main_thread_branch_id_factory,
                "main_thread_branch_id",
            ),
            main_thread_model=main_thread_model,
            # 统一按 plan.branches 的顺序输出：同一组输入无论映射顺序如何，
            # 都得到**相同**的 RoutingPlan（可比较、可审计）。
            branch_models=tuple(
                (spec.branch_id, by_branch[spec.branch_id]) for spec in plan.branches
            ),
        )

    # ------------------------------------------------------------------

    @staticmethod
    def _validated_pairs(
        branch_models: tuple[tuple[BranchId, ModelSelection], ...],
    ) -> tuple[tuple[BranchId, ModelSelection], ...]:
        if not isinstance(branch_models, tuple):
            raise RoutingPlanAssemblyError(
                "branch_models 必须是 (branch_id, ModelSelection) 的 tuple"
            )
        pairs: list[tuple[BranchId, ModelSelection]] = []
        for pair in branch_models:
            if not isinstance(pair, tuple) or len(pair) != 2:
                raise RoutingPlanAssemblyError(
                    f"branch_models 元素必须是 (branch_id, ModelSelection)，收到 {pair!r}"
                )
            branch_id, selection = pair
            if not isinstance(selection, ModelSelection):
                raise RoutingPlanAssemblyError(
                    f"分支 {branch_id!r} 的档位必须是 ModelSelection，收到 {selection!r}"
                )
            pairs.append((branch_id, selection))
        seen = [branch_id for branch_id, _ in pairs]
        if len(set(seen)) != len(seen):
            raise RoutingPlanAssemblyError("branch_models 中 branch_id 不得重复")
        return tuple(pairs)

    @staticmethod
    def _require_complete_coverage(
        plan: Plan, pairs: tuple[tuple[BranchId, ModelSelection], ...]
    ) -> None:
        """**不猜档位**：覆盖不全就直接失败（而不是"全部同模型"）。"""
        planned = {spec.branch_id for spec in plan.branches}
        given = {branch_id for branch_id, _ in pairs}
        missing = planned - given
        extra = given - planned
        if extra:
            raise RoutingPlanAssemblyError(
                f"branch_models 出现了 Plan 未定义的分支 {sorted(extra)}"
                "（档位不得凭空发明分支）"
            )
        if missing:
            detail = (
                "（MULTI_AGENT 的每个 child 都必须有**显式**档位映射；"
                "装配器不会替调用方假设'全部同模型'）"
                if plan.mode is ExecutionMode.MULTI_AGENT
                else ""
            )
            raise RoutingPlanAssemblyError(
                f"分支 {sorted(missing)} 缺少显式档位映射{detail}"
            )

    @staticmethod
    def _resolve(
        explicit: str | None, factory: Callable[[], str], field: str
    ) -> str:
        if explicit is None:
            return factory()
        if not isinstance(explicit, str) or not explicit.strip():
            raise RoutingPlanAssemblyError(f"{field} 必须是非空 str 或 None")
        return explicit
