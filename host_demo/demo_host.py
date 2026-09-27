"""`HostDemo` 的 **Host 实现** —— 装配 + 驱动 + 结算（⛔ 不修改 Core）。

**职责边界（`§279.3` · 实测确认）**

```text
`host_wiring`（Core 内）  只**装配**：不得调用 PassDriver / drive_next_pass / converge_run /
                          record_outcome / InMemoryAdapter（其 `FORBIDDEN_CALLS` 明令）
本模块（Host 侧）         **驱动**：构造 PassDriver、走 custody guard、结算、收口
```

⚠️ **一个实测出来的边界缺口**：`converge_run(manager=…)` 需要一个 `AgentManager`，而
`PassDriver` 的公开面只有 `__init__` / `drive_next_pass`（⛔ 无 manager accessor，
`§235` / `O8` 已否决新增）。⇒ Host 要收口**只能**经 `driver._graph`（γ′ 既定的那个字段）。
本 demo 因此**恰好一处**触碰私有字段，与 `completion_wiring` 同源；已在 `§281` 登记为
`HD-G9`（Host 边界未完全闭合）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from adaptive_router.adapters.in_memory import InMemoryAdapter
from adaptive_router.completion_wiring import accept_completion
from adaptive_router.contracts import BranchSpec, Plan, TerminalState
from adaptive_router.entrypoint import dispatch
from adaptive_router.host_wiring import build_entrypoint_handlers
from adaptive_router.lifecycle_binding_guard import LifecycleBindingGuard
from adaptive_router.pass_driver import PassDriver
from adaptive_router.planner import Planner
from adaptive_router.project_context import ProjectContextLoader, ProjectFacts
from adaptive_router.session import SessionGate, SessionState
from adaptive_router.terminal_wiring import converge_run

from fixtures.conclusions import (
    ControlSource,
    admission_facts_source,
    confirmed_conclusions_by_branch,
    context_facts_by_branch,
    continuation_source,
    reusable_result_source,
)
from fixtures.facts import BRANCH_IDS, PROJECT_ROOT, RUN_ID, T0, registry, router_facts
from fixtures.judgment import judgment_source, payload


class StableIdPlanner:
    """**委派**真 `Planner`，只把 child 的 `branch_id` 换成固定序列。

    ⚠️ `planner.py` 逐字："**允许注入 `branch_id_factory`** 以便测试稳定地生成 child `branch_id`"
    ⇒ 本包装只是把该注入点**外置**（经 `CompositionRoot` 的 **公开** `router_overrides` 缝）。

    ⛔ 它**不改任何判断**：`mode` / `main_thread_duty` / `reason` / 分支数 / 每分支的
      `goal` / `read_only` / `completion_criteria` **全部**来自真 planner 的输出。
    """

    def __init__(self, ids: tuple[str, ...] = BRANCH_IDS) -> None:
        self._real = Planner()
        self._ids = list(ids)

    def plan(self, evaluation: object, task_facts: object, **kwargs: object) -> Plan:
        produced = self._real.plan(evaluation, task_facts, **kwargs)  # type: ignore[arg-type]
        branches = tuple(
            BranchSpec(
                branch_id=self._ids[index],
                goal=spec.goal,
                read_only=spec.read_only,
                completion_criteria=spec.completion_criteria,
            )
            for index, spec in enumerate(produced.branches)
        )
        return Plan(
            mode=produced.mode,
            main_thread_duty=produced.main_thread_duty,
            reason=produced.reason,
            branches=branches,
        )


def project_facts() -> ProjectFacts:
    """自撰的项目事实（⛔ 不读依赖真实仓库布局）。"""
    return ProjectFacts(
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
    )


@dataclass
class HostObservation:
    """一次 demo 运行的**可复核观测**（供 `checks.py` / 报告使用）。"""

    task: str
    outcome: Any = None
    report: Any = None
    driver: PassDriver | None = None
    execution_calls: int = 0
    settlements: list[dict[str, Any]] = field(default_factory=list)
    convergence: dict[str, Any] | None = None

    @property
    def manager(self) -> Any:
        """γ′ 字段：`PassDriver._graph`（Host 侧**唯一**可得的 manager 句柄 · 见模块 docstring）。"""
        assert self.driver is not None, "尚未驱动"
        return self.driver._graph.agent_manager  # noqa: SLF001

    @property
    def routing_run(self) -> Any:
        return self.manager.routing_run


class DemoHost:
    """外部 Host：装配 → dispatch → 驱动一个 pass →（可选）结算 → 收口。"""

    def __init__(
        self,
        *,
        task: str,
        branch_ids: tuple[str, ...] = BRANCH_IDS,
        adapter: InMemoryAdapter | None = None,
    ) -> None:
        self.task = task
        self.adapter = adapter if adapter is not None else InMemoryAdapter(timestamp=T0)
        self.control = ControlSource()
        self.loader = ProjectContextLoader(clock=lambda: T0, facts_source=lambda root: project_facts())
        self.observation = HostObservation(task=task)

        # ---- 装配：10 个 required injections **全部由本 Host 提供**（HD-A1）----
        self.handlers = build_entrypoint_handlers(
            facts_source=lambda task, context: router_facts(),
            capabilities=registry(),
            adapter=self.adapter,
            execution_context_facts_by_branch=context_facts_by_branch(),
            confirmed_conclusions_by_branch=confirmed_conclusions_by_branch(),
            session_gate=SessionGate(state=SessionState.ENABLED),
            control_facts_source=self.control,
            admission_facts_source=admission_facts_source,
            reusable_result_source=reusable_result_source,
            continuation_source=continuation_source,
            project_loader=self.loader,
            on_execution=self._on_execution,
            run_id_factory=lambda: RUN_ID,
            # 公开缝：固定 child id（⛔ 不 poke 私有字段）
            router_overrides={"planner": StableIdPlanner(branch_ids)},
        )

    # -- Host 责任：驱动 --------------------------------------------------

    def _on_execution(self, composition_root: Any, routing: Any) -> None:
        """**调用方责任**（`§200.5` 方案 (i)：装配层不驱动）：恰好驱动**一个** pass。"""
        self.observation.execution_calls += 1
        guard = LifecycleBindingGuard()
        driver = PassDriver(composition_root=composition_root, routing=routing)
        guard.bind(driver)          # custody：恰一次绑定
        self.observation.driver = driver
        self.observation.report = guard.drive_once()   # 恰一次转发
        guard.release()             # 永久放弃 custody

    def dispatch_task(self) -> Any:
        """入口：`dispatch` → loader → router_core → on_execution。"""
        outcome = dispatch(self.task, project_root=PROJECT_ROOT, handlers=self.handlers)
        self.observation.outcome = outcome
        return outcome

    # -- Host 责任：结算与收口（Demo B）----------------------------------

    def settle_branch(
        self,
        branch_id: str,
        attempt: int,
        *,
        satisfied: bool = True,
        backfill_criterion: bool = True,
    ) -> Any:
        """经 **`accept_completion`**（Host 结算唯一入口）落一个分支的终态。

        ⛔ 不绕过：`completion_wiring` → `terminal_wiring::_settle` → `record_outcome`
        （全仓唯一 production 写点）由 Core 内部完成。
        """
        assert self.observation.driver is not None, "尚未驱动"
        result = accept_completion(
            driver=self.observation.driver,
            branch_id=branch_id,
            attempt=attempt,
            payload=payload(conclusion_summary=f"{branch_id} 完成"),
            ended_at=T0,
            judgment_source=judgment_source(
                satisfied=satisfied, backfill_criterion=backfill_criterion
            ),
        )
        self.observation.settlements.append(
            {"branch_id": branch_id, "attempt": attempt, "passed": result.validation.passed}
        )
        return result

    def converge(self, terminal_state: TerminalState = TerminalState.SUCCESS) -> Any:
        """经 **`converge_run`** 收口（⇒ Core 内 `[10].close_run`，全仓唯一收口点）。

        ⚠️ `no_pending_approval` / `no_pending_recovery` 是 `close_run` 的**第二条件**
        （4.5 条件②）—— 本 demo 中二者**显式为真**（无待批 / 无待恢复），并把实参记进观测，
        供 `HD-B4` 审计（⛔ 不靠"我们大概是传了 True"）。
        """
        assert self.observation.driver is not None, "尚未驱动"
        conditions = {"no_pending_approval": True, "no_pending_recovery": True}
        self.observation.convergence = {"terminal_state": terminal_state.value, **conditions}
        return converge_run(
            manager=self.observation.manager,
            terminal_state=terminal_state,
            ended_at=T0,
            **conditions,
        )

    # -- 只读投影 ---------------------------------------------------------

    def branch_states(self) -> dict[str, str]:
        run = self.observation.routing_run
        return {record.branch_id: record.state.value for record in run.branches}
