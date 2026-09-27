"""Router Core composition 行为测试（Step 5B.5 / 5B.5.0）。

架构依据：
  `docs/architecture.md` 4.1 冻结主流程（`:1666-1668`）、4.2 线性解环流、
  `entrypoint.py` 的 ``RouterCore`` 协议，以及 Amendment A3 / A4 / A5 / A6 / A8。

纪律：**Router 负责 orchestration，不成为 decision-maker。**
"""

from __future__ import annotations

import ast
import dataclasses
import pathlib

import pytest

from adaptive_router import router as router_module
from adaptive_router.capabilities import (
    CapabilityProbe,
    CapabilityRegistry,
    EnvironmentReading,
)
from adaptive_router.contracts import (
    ApprovalJudgment,
    Confidence,
    CostEstimate,
    ExecutionMode,
    ImpactScope,
    Ordinal,
    Plan,
    ProjectContext,
    RoutingPlan,
    UnavailableReason,
)
from adaptive_router.cost import UnitCostEstimate
from adaptive_router.planner import EvidenceStrength, TaskFacts
from adaptive_router.policy import PolicyFacts
from adaptive_router.risk import RiskInputs
from adaptive_router.router import (
    FIRST_ATTEMPT,
    TOTAL_ATTEMPT_LIMIT,
    AdaptiveRouter,
    RouterError,
    RouterFacts,
    RouterOutcome,
)

from _import_detectors import imported_module_tokens, imported_names

MODULE_PATH = pathlib.Path(__file__).resolve().parents[2] / "src/adaptive_router/router.py"
MODULE_SOURCE = MODULE_PATH.read_text(encoding="utf-8")
_MODULE_TREE = ast.parse(MODULE_SOURCE)

FIXED_TIME = "2026-09-18T00:00:00Z"


# --------------------------------------------------------------------------
# AST 工具（与项目其余部分同一纪律：不做子串扫描）
# --------------------------------------------------------------------------


def _imported_modules() -> set[str]:
    """Router 源码的 import token（保守检测器，见 `_import_detectors`）。

    ⚠️ 检测器**必须处理 ``node.module is None`` 的形式**：``from . import session``
    的 ``ImportFrom.module`` 是 ``None``，而模块名在 ``node.names`` 里 ——
    只读 ``node.module`` 会**静默漏掉**这条依赖边（假绿）。
    """
    return imported_module_tokens(_MODULE_TREE)


def _imported_names() -> set[str]:
    return imported_names(_MODULE_TREE)


def _called_names() -> set[str]:
    """源码里被**调用**的名字（``f(...)`` 里的 ``f``，含 ``A.B(...)`` 的末段）。"""
    called: set[str] = set()
    for node in ast.walk(_MODULE_TREE):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                called.add(func.id)
            elif isinstance(func, ast.Attribute):
                called.add(func.attr)
    return called


def _class_node(name: str, source: str | None = None) -> ast.ClassDef:
    tree = ast.parse(source) if source is not None else _MODULE_TREE
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == name:
            return node
    raise AssertionError(f"源码里找不到类 {name}")


def _method_node(
    method_name: str, class_name: str = "AdaptiveRouter", source: str | None = None
) -> ast.AST:
    """取**指定类**里的方法节点。

    ⚠️ 必须限定类：本模块里 `FactSource` Protocol 也有一个 `__call__`，
    而它在 `AdaptiveRouter` **之前**定义 —— 全树搜第一个名为 `__call__`
    的函数会拿到 Protocol 的空方法体，于是所有"方法体级"检测器都变成**永远为空**。
    """
    class_node = _class_node(class_name, source)
    for node in class_node.body:
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == method_name
        ):
            return node
    raise AssertionError(f"{class_name} 里找不到方法 {method_name}")


def _capabilities_member_accesses(
    method_name: str = "__call__",
    *,
    class_name: str = "AdaptiveRouter",
    source: str | None = None,
) -> list[str]:
    """``self.capabilities.<member>`` 的成员访问（**属性级**，不是名字级）。

    ⚠️ 只查 ``ast.Name`` 抓不到 ``self.capabilities._probe`` /
    ``self.capabilities.probed_at`` / ``self.capabilities.last_trigger`` ——
    那些是 ``ast.Attribute``。本检测器直接看属性链，
    因此 Registry 内部字段怎么改名都不影响这条边界。
    """
    method = _method_node(method_name, class_name, source)
    members: list[str] = []
    for node in ast.walk(method):
        if not isinstance(node, ast.Attribute):
            continue
        owner = node.value
        if (
            isinstance(owner, ast.Attribute)
            and owner.attr == "capabilities"
            and isinstance(owner.value, ast.Name)
            and owner.value.id == "self"
        ):
            members.append(node.attr)
    return members


def _capabilities_call_count(
    method_name: str = "__call__",
    *,
    class_name: str = "AdaptiveRouter",
    source: str | None = None,
) -> int:
    """``self.capabilities.<member>(...)`` 形式的**调用**次数。"""
    method = _method_node(method_name, class_name, source)
    count = 0
    for node in ast.walk(method):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute):
            continue
        owner = func.value
        if (
            isinstance(owner, ast.Attribute)
            and owner.attr == "capabilities"
            and isinstance(owner.value, ast.Name)
            and owner.value.id == "self"
        ):
            count += 1
    return count


#: Router **不得构造**的冻结产物（它们必须来自对应决策节点）。
#:
#: ⚠️ `RouterFacts` / `RouterOutcome` **不在**此列 ——
#: 它们是 Router **自己的**实现级 DTO，由 Router 构造是设计的一部分。
_FORBIDDEN_CONSTRUCTIONS = (
    "ApprovalDisclosure",
    "ApprovalJudgment",
    "ModelSelection",
    "Plan",
    "CostEstimate",
    "Evaluation",
    "RiskAssessment",
    "TaskFacts",
    "PolicyFacts",
)


# --------------------------------------------------------------------------
# 测试替身
# --------------------------------------------------------------------------


def reading(concurrency_limit: int | None = 4) -> EnvironmentReading:
    """`[03]` 的**环境读数**（loader 的产物，不是快照本身）。

    ⚠️ 契约要求：**未知**能力必须给出 ``unavailable_reason``。
    """
    return EnvironmentReading(
        subagents=True,
        async_background=True,
        concurrency_limit=concurrency_limit,
        available_models=(),
        available_effort_levels=(),
        state_wait_append_stop=True,
        hot_switch=True,
        reliable_cost_data=True,
        unavailable_reason=(
            UnavailableReason.NOT_PROBED if concurrency_limit is None else None
        ),
    )


def registry(
    concurrency_limit: int | None = 4, *, probes: list[int] | None = None
) -> CapabilityRegistry:
    """真实的 `[03]` 节点：`CapabilityRegistry` + 记录**探测**次数的 loader。

    ⚠️ `probes` 记的是 loader（探测）次数；`snapshot()` 自身缓存，
    两者不同 —— 本文件另用 spy 锁 `snapshot()` 的**调用**次数。
    """
    probe = CapabilityProbe(probed_at=FIXED_TIME)

    def loader(*args: object) -> EnvironmentReading:
        if probes is not None:
            probes.append(1)
        return reading(concurrency_limit)

    return CapabilityRegistry(probe=probe, loader=loader)


def context() -> ProjectContext:
    return ProjectContext.absent("/repo", FIXED_TIME)


def task_facts(**overrides: object) -> TaskFacts:
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


def risk_inputs(**overrides: object) -> RiskInputs:
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
    return RiskInputs(**base)  # type: ignore[arg-type]


def policy_facts(**overrides: object) -> PolicyFacts:
    base: dict[str, object] = dict(
        session_authorized=True,
        system_denied=(),
        developer_denied=(),
        safety_denied=(),
        platform_denied=(),
        user_denied=(),
        project_denied=(),
        user_granted_scopes=(),
        user_commands=(),
        user_read_only_override=False,
        project_model_preference=None,
        project_approval_preference=None,
        protocol_default_mode="observe",
    )
    base.update(overrides)
    return PolicyFacts(**base)  # type: ignore[arg-type]


def facts(**overrides: object) -> RouterFacts:
    """5B.5.1 后的精确字段集：**不再含 capability**。"""
    base: dict[str, object] = dict(
        policy=policy_facts(),
        task=task_facts(),
        risk=risk_inputs(),
        recovery_method="restore from backup",
    )
    base.update(overrides)
    return RouterFacts(**base)  # type: ignore[arg-type]


def run(**overrides: object) -> RouterOutcome:
    """跑一次真实主链（只替换事实，不替换任何决策节点）。"""
    return AdaptiveRouter(
        facts_source=lambda task, ctx: facts(**overrides), capabilities=registry()
    )("do the task", context())


class Recorder:
    """记录调用顺序的 spy：**委托**真实实现，不替换任何判断。"""

    def __init__(self, label: str, order: list[str], delegate):
        self._label = label
        self._order = order
        self._delegate = delegate

    def __getattr__(self, item):
        attribute = getattr(self._delegate, item)
        if not callable(attribute):
            return attribute

        def recorded(*args, **kwargs):
            self._order.append(f"{self._label}.{item}")
            return attribute(*args, **kwargs)

        return recorded


def ordered_router(order: list[str], **overrides) -> AdaptiveRouter:
    kwargs = {
        "policy": Recorder(
            "policy", order, overrides.pop("policy", None) or router_module.Policy()
        ),
        "capabilities": Recorder(
            "capabilities", order, overrides.pop("capabilities", None) or registry()
        ),
        "evaluator": Recorder("evaluator", order, router_module.Evaluator()),
        "risk_evaluator": Recorder("risk", order, router_module.RiskEvaluator()),
        "planner": Recorder("planner", order, router_module.Planner()),
        "model_router": Recorder("model", order, router_module.ModelRouter()),
        "assembler": Recorder("assembler", order, router_module.RoutingPlanAssembler()),
        "cost_wiring": Recorder("cost", order, router_module.CostEstimatorWiring()),
        "judge": Recorder("judge", order, router_module.ApprovalJudge()),
    }
    kwargs.update(overrides)
    return AdaptiveRouter(facts_source=lambda task, ctx: facts(), **kwargs)


# ==========================================================================
# 端到端：真实主链
# ==========================================================================


class TestEndToEnd:
    def test_it_returns_the_implementation_level_outcome(self) -> None:
        assert isinstance(run(), RouterOutcome)

    def test_every_artifact_is_the_frozen_type(self) -> None:
        outcome = run()
        assert isinstance(outcome.plan, Plan)
        assert isinstance(outcome.routing, RoutingPlan)
        assert isinstance(outcome.approval, ApprovalJudgment)
        assert all(isinstance(u, UnitCostEstimate) for u in outcome.costs)

    def test_the_chain_reaches_a_decision(self) -> None:
        outcome = run()
        assert outcome.plan.mode in tuple(ExecutionMode)
        assert isinstance(outcome.approval.needs_approval, bool)

    def test_the_frozen_outcome_has_exactly_four_products(self) -> None:
        """每个字段都有具名冻结消费者；不夹带上游中间产物。"""
        assert {f.name for f in dataclasses.fields(RouterOutcome)} == {
            "plan",
            "routing",
            "costs",
            "approval",
        }

    def test_it_carries_no_platform_field(self) -> None:
        names = {f.name for f in dataclasses.fields(RouterOutcome)}
        for forbidden in ("provider", "model_id", "openai", "codex", "api"):
            assert forbidden not in names, forbidden

    def test_it_is_deterministic(self) -> None:
        """`run_id` / `main_thread_branch_id` 是随机的，故注入固定 factory。

        （它们由装配层生成，不属决策；固定后可逐字段比较。）
        """
        fixed = router_module.RoutingPlanAssembler(
            run_id_factory=lambda: "run_fixed",
            main_thread_branch_id_factory=lambda: "br_main_fixed",
        )
        one = AdaptiveRouter(
            facts_source=lambda task, ctx: facts(),
            capabilities=registry(),
            assembler=fixed,
        )("do the task", context())
        two = AdaptiveRouter(
            facts_source=lambda task, ctx: facts(),
            capabilities=registry(),
            assembler=fixed,
        )("do the task", context())
        assert one.plan == two.plan
        assert one.approval == two.approval
        assert one.routing.run_id == two.routing.run_id
        assert [u.drivers.drivers() for u in one.costs] == [
            u.drivers.drivers() for u in two.costs
        ]

    def test_a_writable_branch_forces_approval_end_to_end(self) -> None:
        """真实通路：`external_write` 风险 ⇒ 审批，且理由来自**真实**风险。"""
        outcome = run(risk=risk_inputs(external_write=True))
        assert outcome.approval.needs_approval is True
        assert "external_write" in outcome.approval.main_risk

    def test_hard_to_revert_without_a_fact_yields_the_explicit_unknown(self) -> None:
        """A8.3 分支 2 走通真实主链。"""
        outcome = run(
            risk=risk_inputs(hard_to_revert_operation=True), recovery_method=None
        )
        assert "难以恢复" in outcome.approval.recovery_method


# ==========================================================================
# 顺序：与 4.1 冻结主流程逐节点一致
# ==========================================================================


class TestChainOrder:
    def test_order_matches_the_frozen_main_flow(self) -> None:
        """架构 4.1（`:1657-1668`）：

            [02] → [03] → [04] → [05] → Risk → [06] → [15a] → Judgment

        ⚠️ **[03] Capability 必须在主链上**（5B.5.1）—— 它曾经被
        `facts_source` 预置成静态事实而完全绕过。
        ⚠️ **[05] Planner 必须在 Risk 之前** —— 不得照 4.2 的反环子链重排
        （4.2 只画了解环所需的那条子链，Planner 不在该环里）。
        """
        order: list[str] = []
        ordered_router(order)("do the task", context())
        assert order == [
            "policy.resolve",
            "capabilities.snapshot",
            "evaluator.evaluate",
            "planner.plan",
            "risk.assess",
            "model.select",
            "model.assign_branch_models",
            "assembler.assemble",
            "cost.estimate_all_units",
            "judge.judge",
        ], order

    def test_disclosure_is_built_between_cost_and_judgment(self, monkeypatch) -> None:
        order: list[str] = []
        real = router_module.build_disclosure

        def spy(**kwargs):
            order.append("build_disclosure")
            return real(**kwargs)

        monkeypatch.setattr(router_module, "build_disclosure", spy)
        ordered_router(order)("do the task", context())
        assert order.index("build_disclosure") == order.index(
            "cost.estimate_all_units"
        ) + 1
        assert order.index("build_disclosure") < order.index("judge.judge")


# ==========================================================================
# A5 —— 组合层的两处派生
# ==========================================================================


class TestA5Derivations:
    @pytest.mark.parametrize(
        ("ceiling", "expected"),
        [
            (None, 1),  # 0 分支计划：unit_count = 1（仅主线程）
            (0, 1),  # zero-ceiling → 成本结构下界 1（**非**能力口径）
            (1, 1),
            (4, 1),
        ],
    )
    def test_max_concurrency_branches(self, ceiling, expected) -> None:
        outcome = run_with_registry(registry(ceiling))
        assert all(
            unit.drivers.max_concurrency == expected for unit in outcome.costs
        ), ceiling

    def test_retry_allowance_is_derived_not_configured(self) -> None:
        outcome = run()
        assert TOTAL_ATTEMPT_LIMIT == 2
        assert all(
            unit.drivers.retry_allowance
            == max(TOTAL_ATTEMPT_LIMIT - FIRST_ATTEMPT, 0)
            for unit in outcome.costs
        )

    def test_first_attempt_is_structural(self) -> None:
        outcome = run()
        assert FIRST_ATTEMPT == 1
        assert all(unit.drivers.attempt == FIRST_ATTEMPT for unit in outcome.costs)


def run_with_registry(capabilities: CapabilityRegistry, **overrides: object):
    return AdaptiveRouter(
        facts_source=lambda task, ctx: facts(**overrides), capabilities=capabilities
    )("do the task", context())


# ==========================================================================
# 5B.5.1 —— [03] 在主链上，且每次 invocation 只取一次快照
# ==========================================================================


class TestCapabilityIsOnTheChain:
    def test_the_registry_is_required(self) -> None:
        """Router **不构造** Registry —— 它需要 loader / probe / probed_at 等宿主事实。"""
        import inspect

        parameters = inspect.signature(AdaptiveRouter).parameters
        assert "capabilities" in parameters
        assert parameters["capabilities"].default is inspect.Parameter.empty

    def test_omitting_the_registry_is_a_type_error(self) -> None:
        with pytest.raises(TypeError):
            AdaptiveRouter(facts_source=lambda task, ctx: facts())  # type: ignore[call-arg]
        AdaptiveRouter(facts_source=lambda task, ctx: facts(), capabilities=registry())

    def test_snapshot_is_taken_exactly_once_per_invocation(self) -> None:
        """**5B.5.1 的关键锁**：单次决策只能有一个能力视图。

        若 Evaluator / Planner / A5 各自再取一次快照，
        同一次决策就可能混用不同的能力事实。
        """
        calls: list[int] = []
        real = registry()

        class CountingRegistry:
            def __init__(self, delegate):
                self._delegate = delegate

            def snapshot(self):
                calls.append(1)
                return self._delegate.snapshot()

        AdaptiveRouter(
            facts_source=lambda task, ctx: facts(),
            capabilities=CountingRegistry(real),  # type: ignore[arg-type]
        )("do the task", context())
        assert len(calls) == 1, calls

    def test_the_probe_runs_once_even_across_invocations(self) -> None:
        """缓存语义：同一 Registry 复用 ⇒ 探测只发生一次（`[03]` 自己负责）。"""
        probes: list[int] = []
        shared = registry(probes=probes)
        router = AdaptiveRouter(
            facts_source=lambda task, ctx: facts(), capabilities=shared
        )
        router("do the task", context())
        router("do the task", context())
        assert len(probes) == 1, probes

    def test_evaluator_planner_and_a5_share_the_same_snapshot_instance(
        self, monkeypatch
    ) -> None:
        """**验收点 ④ 的身份级证明**：三者必须拿到**同一个**快照对象。

        ⚠️ 只比较 `max_concurrency` 的**数值**是不够的 ——
        V1 的默认路径只有一个执行单位，`min(1, 1)` 与 `min(1, 8)` 都得 `1`，
        于是"碰巧相等"会掩盖 `copy.copy(capability)` 这类错误实现。
        因此这里直接 **spy `_max_concurrency`**，断言它收到的就是那个冻结快照。
        """
        seen: dict[str, object] = {}
        cached = registry().snapshot()

        class SpyEvaluator:
            def evaluate(self, facts_, *, capability, project):
                seen["evaluator"] = capability
                return router_module.Evaluator().evaluate(
                    facts_, capability=capability, project=project
                )

        class SpyPlanner:
            def plan(self, evaluation, facts_, *, capability, project, constraints):
                seen["planner"] = capability
                return router_module.Planner().plan(
                    evaluation,
                    facts_,
                    capability=capability,
                    project=project,
                    constraints=constraints,
                )

        real_max_concurrency = router_module._max_concurrency

        def spy_max_concurrency(routing, capability):
            seen["a5"] = capability
            return real_max_concurrency(routing, capability)

        monkeypatch.setattr(router_module, "_max_concurrency", spy_max_concurrency)

        class FixedRegistry:
            def snapshot(self):
                return cached

        outcome = AdaptiveRouter(
            facts_source=lambda task, ctx: facts(),
            capabilities=FixedRegistry(),  # type: ignore[arg-type]
            evaluator=SpyEvaluator(),  # type: ignore[arg-type]
            planner=SpyPlanner(),  # type: ignore[arg-type]
        )("do the task", context())

        assert seen["evaluator"] is cached
        assert seen["planner"] is cached
        assert seen["a5"] is cached, "A5 派生必须消费同一个快照实例"
        assert outcome.plan is not None  # 主链跑通

    def test_the_identity_proof_has_discriminating_power(self) -> None:
        """为什么上一条必须用 `is` 而不是 `==`（**证明的区分能力**）。

        `CapabilitySnapshot` 是 frozen dataclass ⇒ **值相等**很廉价：
        `copy.copy(cached) == cached` 为真，但 `is` 为假。
        因此"比较 `max_concurrency` 数值"只能证明**碰巧相等**，
        只有身份断言才能证明三者消费的是**同一个冻结快照**。
        """
        import copy

        cached = registry().snapshot()
        duplicate = copy.copy(cached)
        assert duplicate == cached  # 值相等 —— `==` 区分不出来
        assert duplicate is not cached  # `is` 能区分

    def test_a_copy_would_break_the_identity_assertion(self, monkeypatch) -> None:
        """把 Router 的 A5 派生换成"传一份副本"，身份断言必须变红。"""
        import copy

        cached = registry().snapshot()
        seen: dict[str, object] = {}
        real = router_module._max_concurrency

        def copying(routing, capability):
            # 模拟错误实现：A5 没有消费那个冻结快照，而是用了一份副本
            duplicate = copy.copy(capability)
            seen["a5"] = duplicate
            return real(routing, duplicate)

        monkeypatch.setattr(router_module, "_max_concurrency", copying)

        class FixedRegistry:
            def snapshot(self):
                return cached

        AdaptiveRouter(
            facts_source=lambda task, ctx: facts(),
            capabilities=FixedRegistry(),  # type: ignore[arg-type]
        )("do the task", context())
        assert seen["a5"] is not cached
        assert seen["a5"] == cached  # 数值相同 —— 数值比较会漏掉它

    def test_the_snapshot_actually_reaches_the_evaluator(self) -> None:
        """真实传导：`concurrency_limit <= 1` ⇒ `Evaluation.concurrency_capacity = LOW`
        ⇒ Planner 命中 `INSUFFICIENT_CAPACITY` ⇒ 收回到单代理**阶段化**。

        这证明快照不是"被接收但没用"，而是真的进入了 `[04]` 的判断。
        """
        starved = run_with_registry(registry(1))
        assert starved.plan.mode is ExecutionMode.STAGED_SINGLE_AGENT
        assert "insufficient_capacity" in starved.plan.reason

    def test_the_a5_derivation_uses_the_same_snapshot_ceiling(self) -> None:
        assert run_with_registry(registry(1)).costs[0].drivers.max_concurrency == 1
        # 0 分支计划只有主线程 ⇒ min(1, 8) = 1；ceiling 仍参与派生
        assert run_with_registry(registry(8)).costs[0].drivers.max_concurrency == 1


class TestRouterDoesNotProbe:
    def test_it_never_calls_recheck(self) -> None:
        """重查是**事件驱动**的（6 类运行期事件）；决策链没有合法 trigger 输入。"""
        assert "recheck" not in _called_names()
        assert "recheck" not in {
            n.attr for n in ast.walk(_MODULE_TREE) if isinstance(n, ast.Attribute)
        }

    def test_the_only_legal_registry_member_is_snapshot(self) -> None:
        """**allowlist**：`self.capabilities` 的**唯一**合法成员访问就是 `snapshot`。

        ```text
        ✅ self.capabilities.snapshot()
        ❌ self.capabilities.recheck
        ❌ self.capabilities._loader / ._probe / .probed_at / .last_trigger
        ❌ 任何其他成员
        ```

        ⚠️ 按字段名枚举（`ast.Name`）抓不到 `self.capabilities._probe` 这类
        属性访问；本检测器看的是**属性链**，因此 Registry 内部改名也不会削弱边界。
        """
        members = _capabilities_member_accesses()
        assert set(members) == {"snapshot"}, members

    def test_snapshot_is_called_exactly_once_in_the_source(self) -> None:
        assert _capabilities_call_count() == 1

    def test_the_allowlist_detector_sees_forbidden_members(self) -> None:
        """**检测器敏感性**：注入违规成员访问必须被看见。"""
        for injected in (
            "        self.capabilities.recheck\n",
            "        self.capabilities._probe\n",
            "        self.capabilities._loader\n",
            "        self.capabilities.probed_at\n",
            "        self.capabilities.last_trigger\n",
        ):
            patched = MODULE_SOURCE.replace(
                "        capability = self.capabilities.snapshot()\n",
                "        capability = self.capabilities.snapshot()\n" + injected,
            )
            assert injected in patched, "注入点没找到"
            assert set(_capabilities_member_accesses(source=patched)) != {"snapshot"}, (
                injected
            )

    def test_the_call_counter_sees_a_second_call(self) -> None:
        """敏感性：再调一次 `snapshot()` 必须被计数发现。"""
        patched = MODULE_SOURCE.replace(
            "        capability = self.capabilities.snapshot()\n",
            "        capability = self.capabilities.snapshot()\n"
            "        self.capabilities.snapshot()\n",
        )
        assert _capabilities_call_count(source=patched) == 2

    def test_it_never_touches_the_probe_or_its_loader(self) -> None:
        """Router 只消费 Registry，**不**碰 probe 类型本身。

        ⚠️ 不能把 `evaluate` 列进禁止名单 —— 那是 **Evaluator 的方法名**；
        要禁止的是 `CapabilityProbe` 这个类型（下一条断言）。
        """
        assert "CapabilityProbe" not in _called_names()
        assert "CapabilityProbe" not in _imported_names()
        identifiers = {n.id for n in ast.walk(_MODULE_TREE) if isinstance(n, ast.Name)}
        for forbidden in ("loader", "probed_at", "CapabilityDimension", "RecheckTrigger"):
            assert forbidden not in identifiers, forbidden

    def test_it_only_consumes_the_registry(self) -> None:
        assert "CapabilityRegistry" in _imported_names()
        assert "snapshot" in _called_names()


# ==========================================================================
# 5B.5.0 ②（C1）—— Judgment 读主线程单位的估算
# ==========================================================================


class TestC1MainThreadEstimate:
    def test_the_judge_receives_the_main_thread_estimate(self) -> None:
        captured: dict[str, CostEstimate] = {}
        real_judge = router_module.ApprovalJudge()

        class SpyJudge:
            def judge(self, risk, cost_estimate, authorization, routing, **kwargs):
                captured["cost"] = cost_estimate
                return real_judge.judge(
                    risk, cost_estimate, authorization, routing, **kwargs
                )

        outcome = run()
        AdaptiveRouter(
            facts_source=lambda task, ctx: facts(),
            capabilities=registry(),
            judge=SpyJudge(),  # type: ignore[arg-type]
        )("do the task", context())

        main_units = [
            unit
            for unit in outcome.costs
            if unit.branch_id == outcome.routing.main_thread_branch_id
        ]
        assert len(main_units) == 1
        assert captured["cost"] == main_units[0].estimate

    def test_a_missing_main_thread_unit_fails_explicitly(self) -> None:
        """**不得**回退到"随便挑一条"或"跨单位聚合"。"""
        outcome = run()
        other = tuple(
            unit
            for unit in outcome.costs
            if unit.branch_id != outcome.routing.main_thread_branch_id
        )
        with pytest.raises(RouterError):
            router_module._main_thread_estimate(outcome.routing, other)

    def test_the_router_does_not_aggregate_across_units(self) -> None:
        """结构锁：Router 里不得出现跨单位的成本聚合（`max` / `sum`）。"""
        assert "sum" not in _called_names()
        function = next(
            node
            for node in ast.walk(_MODULE_TREE)
            if isinstance(node, ast.FunctionDef) and node.name == "_main_thread_estimate"
        )
        identifiers = {n.id for n in ast.walk(function) if isinstance(n, ast.Name)}
        assert "max" not in identifiers
        assert "sum" not in identifiers


# ==========================================================================
# 唯一事实入口：缺事实即显式失败，不补默认值
# ==========================================================================


class TestFactSourceIsTheOnlyFactEntry:
    def test_a_non_router_facts_result_fails_explicitly(self) -> None:
        router = AdaptiveRouter(
            facts_source=lambda task, ctx: "not facts",  # type: ignore[arg-type,return-value]
            capabilities=registry(),
        )
        with pytest.raises(RouterError):
            router("do the task", context())

    def test_none_facts_fail_explicitly(self) -> None:
        router = AdaptiveRouter(
            facts_source=lambda task, ctx: None,  # type: ignore[arg-type,return-value]
            capabilities=registry(),
        )
        with pytest.raises(RouterError):
            router("do the task", context())

    def test_a_non_project_context_fails_explicitly(self) -> None:
        with pytest.raises(RouterError):
            AdaptiveRouter(
                facts_source=lambda task, ctx: facts(), capabilities=registry()
            )(
                "do the task", "not a context"  # type: ignore[arg-type]
            )

    def test_the_facts_bundle_carries_only_existing_fact_types(self) -> None:
        """5B.5.1：**不含 `capability`** —— 它由 `[03]` 在主链上产出。"""
        expected = {
            "policy",
            "task",
            "risk",
            "recovery_method",
        }
        assert {f.name for f in dataclasses.fields(RouterFacts)} == expected

    def test_the_facts_bundle_is_frozen(self) -> None:
        bundle = facts()
        with pytest.raises(dataclasses.FrozenInstanceError):
            bundle.task = bundle.task  # type: ignore[misc]

    def test_the_facts_source_is_required(self) -> None:
        parameter = __import__("inspect").signature(AdaptiveRouter).parameters
        assert parameter["facts_source"].default is __import__(
            "inspect"
        ).Parameter.empty


# ==========================================================================
# A8 —— Router **只能**经 build_disclosure()
# ==========================================================================


class TestA8DisclosureBoundary:
    def test_the_router_calls_build_disclosure(self) -> None:
        assert "build_disclosure" in _called_names()

    def test_the_router_never_constructs_the_disclosure_dto(self) -> None:
        assert "ApprovalDisclosure" not in _called_names()
        assert "ApprovalDisclosure" not in _imported_names()

    def test_the_router_does_not_build_the_three_fields_itself(self) -> None:
        """不得自拼 `proposed_action` / 自算 `impact_scope` / 自生成 `recovery_method`。"""
        for forbidden in ("proposed_action", "impact_scope"):
            assert forbidden not in _called_names(), forbidden
        # `recovery_method` 只作为**字段读取**传给 build_disclosure，不得被构造
        function = next(
            node
            for node in ast.walk(_MODULE_TREE)
            if isinstance(node, ast.FunctionDef) and node.name == "__call__"
        )
        sources = {
            node.func.attr
            for node in ast.walk(function)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        assert "resolve_recovery_method" not in sources


# ==========================================================================
# Router 不成为 decision-maker（结构锁）
# ==========================================================================


class TestRouterIsNotADecisionMaker:
    @pytest.mark.parametrize("forbidden", _FORBIDDEN_CONSTRUCTIONS)
    def test_it_never_constructs_frozen_artifacts(self, forbidden) -> None:
        assert forbidden not in _called_names(), forbidden

    def test_it_never_names_a_verdict_constant(self) -> None:
        """不得内联任何"判断结论"（档位 / 执行方式 / 审批）。"""
        identifiers = {n.id for n in ast.walk(_MODULE_TREE) if isinstance(n, ast.Name)}
        for forbidden in ("MAXIMUM", "STRONG", "BALANCED", "MULTI_AGENT", "STAGED_SINGLE_AGENT"):
            assert forbidden not in identifiers, forbidden

    def test_it_does_not_re_decide_needs_approval(self) -> None:
        identifiers = {n.id for n in ast.walk(_MODULE_TREE) if isinstance(n, ast.Name)}
        assert "needs_approval" not in identifiers

    def test_it_does_not_import_the_session_layer_or_adapters(self) -> None:
        """BYPASS 在 Router 之外；平台细节不进 Core。

        ⚠️ 检测器已处理 ``from . import session`` 形式 ——
        只读 ``ImportFrom.module`` 会漏掉这种依赖边（见 `_imported_modules`）。
        """
        modules = _imported_modules()
        for forbidden in ("session", "entrypoint", "adapters"):
            assert forbidden not in modules, forbidden
        assert not any(m.startswith("adaptive_router.session") for m in modules)
        assert not any("adapters" in m for m in modules)

    def test_the_import_detector_catches_relative_submodule_imports(self) -> None:
        """检测器**非平凡性**：把 `from . import session` 注入源码副本必须被看见。

        （只读 `node.module` 的朴素实现会漏 —— 这正是本条存在的理由；
        朴素实现的对照证明见 `tests/behavior/test_import_detectors.py`。）
        """
        for form, expected in (
            ("from . import session", "session"),
            ("from . import adapters", "adapters"),
            ("from .session import SessionGate", "session"),
            ("from adaptive_router import session", "session"),
        ):
            found = imported_module_tokens(MODULE_SOURCE + "\n" + form + "\n")
            assert expected in found, form

    def test_it_does_not_read_files_or_environment(self) -> None:
        modules = _imported_modules()
        for forbidden in ("pathlib", "os", "subprocess", "json", "socket"):
            assert forbidden not in modules, forbidden

    def test_it_does_not_touch_the_contract_layer(self) -> None:
        """`RouterFacts` / `RouterOutcome` 都是实现级 DTO，不进 contracts。"""
        from adaptive_router import contracts

        for name in ("RouterFacts", "RouterOutcome", "FactSource", "RouterError"):
            assert name not in contracts.__all__, name
            assert not hasattr(contracts, name), name

    def test_the_router_satisfies_the_frozen_router_core_protocol(self) -> None:
        """`entrypoint.py` 的 `RouterCore.__call__(task, context)`。"""
        import inspect

        parameters = list(inspect.signature(AdaptiveRouter.__call__).parameters)
        assert parameters == ["self", "task", "context"]

    def test_the_outcome_is_not_wrapped_for_the_host(self) -> None:
        """entrypoint 的纪律：不包装结果、不引入平台形状。"""
        assert not hasattr(RouterOutcome, "as_text")
        assert not hasattr(RouterOutcome, "to_platform")
