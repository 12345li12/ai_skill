"""P1（``execution.py``）—— 行为测试 B1–B18 + 结构锁 L12-1 – L12-16。

架构依据：`docs/architecture.md` 的 **A11.10 / A13.5 / A13.6 / A14.6**。
范围、锁清单与 probe 计划：`tests/behavior/README_step5b.md` §53–§57。

⚠️ 敏感性 probe 一律在**源码字符串**上完成，**不覆写** ``src/`` 里的真实文件。

⚠️ ``[11]`` 用**假适配器**（`test_adapter_fake.FakeAdapter`）—— 裁定 ⑤：不接真实 provider。

⚠️ **实测约束（本轮发现）**：``Plan`` 契约要求 **非 ``MULTI_AGENT`` 不得携带分支**、
而 ``MULTI_AGENT`` **至少 2 个 child**（L25）⇒ **合法的 `Plan.branches` 不可能只有 1 个 child**。
因此本文件所有场景都用**两个** child。
"""

from __future__ import annotations

import ast
import dataclasses
import pathlib

import pytest

from adaptive_router.adapters import AdapterError, AdapterFailure
from adaptive_router.admission_facts import AdmissionFacts, RelationFact
from adaptive_router.agent_manager import AgentManager
from adaptive_router.contracts import (
    BranchSpec,
    BranchState,
    CapabilityMismatch,
    CapabilitySnapshot,
    ContextBundle,
    EffortLevel,
    ExecutionMode,
    ModelSelection,
    ModelTier,
    Plan,
    RoutingPlan,
    UnavailableReason,
    new_branch_id,
    new_run_id,
)
from adaptive_router.dispatch_decisions import DispatchAction, DispatchDecision
from adaptive_router.execution import ExecutionCompositor, ExecutionPassReport
from adaptive_router.runtime_control_facts import RuntimeControlFacts
from adaptive_router.scheduler import Scheduler
from adaptive_router.scheduling_runtime_facts import SchedulingRuntimeFacts

from _execution_locks import (
    call_keyword_shapes,
    calls_to,
    clock_call_hits,
    dataclass_field_names,
    exception_subclass_names,
    forbidden_import_hits,
    handled_exception_names,
    imported_module_tokens,
    mapping_get_calls,
    modules_importing,
    open_calls,
    signature_shape,
    tuple_unpacking_lines,
)
from test_adapter_fake import (
    FakeAdapter,
    PlatformCrash,
    PlatformNetworkError,
    PlatformRefused,
    PlatformTimeout,
    clock,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
SRC = REPO_ROOT / "src/adaptive_router"
EXECUTION_SOURCE = (SRC / "execution.py").read_text(encoding="utf-8")

#: L12-1 —— `execution.py` 的 import token **精确集合**。
#: ⚠️ **pre-wiring = 17 项**；`P1 ResultHandoff wiring`（§67）新增 3 项：
#: `result_handoff`（六个 seam DTO）与 `common` / `contracts.common`（`ContractError`）。
ALLOWED_IMPORT_TOKENS = frozenset(
    {
        "__future__",
        "annotations",
        "dataclasses",
        "dataclass",
        "typing",
        "Callable",
        "Mapping",
        "adapters",
        "admission_facts",
        "agent_manager",
        "agent_start_requests",
        "common",
        "contracts",
        "contracts.common",
        "dispatch_decisions",
        "result_handoff",
        "reusable_results",
        "runtime_control_facts",
        "scheduler",
        "scheduling_runtime_facts",
    }
)

#: L12-1 —— 禁用清单：决策 / 成本 / 入口 / 平台实现一律不得进入 P1。
FORBIDDEN_IMPORTS = (
    "policy",
    "cost",
    "router",
    "entrypoint",
    "session",
    "planner",
    "model_router",
    "risk",
    "capabilities",
    "project_context",
    "context_builder",
    "openai",
    "anthropic",
)

#: L12-9 —— `run_pass` 的精确 keyword-only 形状（**6** 项、全无默认值）。
EXPECTED_RUN_PASS_KWONLY = [
    "routing",
    "bundles",
    "capability",
    "runtime_facts",
    "control_facts_source",
    "first_pass",
]

#: L12-9 —— `__init__` 的精确形状（**6** 个注入；`control_facts_source` **不在**其中）。
EXPECTED_INIT_KWONLY = [
    "scheduler",
    "agent_manager",
    "adapter",
    "admission_facts_source",
    "reusable_result_source",
    # `5B.6.52.3`（P2 · E1 ingress）：Host 续跑事实来源 —— **required**（⛔ 无默认值）
    "continuation_source",
]


# --------------------------------------------------------------------------
# 测试替身
# --------------------------------------------------------------------------


def context_bundle(goal: str = "g") -> ContextBundle:
    return ContextBundle(
        goal=goal,
        background=(),
        constraints=(),
        allowed_operations=(),
        forbidden_operations=(),
        output_format="text",
        completion_criteria="done",
        confirmed_conclusions=(),
        excluded=(),
        includes_history=False,
    )


def branch_spec(branch_id: str) -> BranchSpec:
    return BranchSpec(
        branch_id=branch_id,
        goal=f"goal {branch_id}",
        read_only=True,
        completion_criteria=f"done {branch_id}",
    )


def routing_plan(first: str, second: str) -> RoutingPlan:
    """两个 child 的 ``MULTI_AGENT`` 计划（契约下限，见模块 docstring）。"""
    selection = ModelSelection(
        tier=ModelTier.BALANCED, effort=EffortLevel.MEDIUM, reason="test"
    )
    return RoutingPlan(
        run_id=new_run_id(),
        plan=Plan(
            mode=ExecutionMode.MULTI_AGENT,
            main_thread_duty="duty",
            reason="test",
            branches=(branch_spec(first), branch_spec(second)),
        ),
        main_thread_branch_id=new_branch_id(),
        main_thread_model=selection,
        branch_models=((first, selection), (second, selection)),
    )


def capability(*, concurrency_limit: int | None = 4) -> CapabilitySnapshot:
    return CapabilitySnapshot(
        subagents=True,
        async_background=True,
        concurrency_limit=concurrency_limit,
        available_models=("provider-model-x",),
        available_effort_levels=(EffortLevel.HIGH,),
        state_wait_append_stop=True,
        hot_switch=False,
        reliable_cost_data=True,
        probed_dimensions=("subagents",),
        unavailable_reason=UnavailableReason.NOT_PROBED,
        probed_at="2026-09-18T00:00:00Z",
    )


def unknown_facts() -> AdmissionFacts:
    """七槽全 ``None``（显式 UNKNOWN）—— 不构成 veto（A11.3）。"""
    blank = RelationFact(status=None, related_branch_id=None)
    return AdmissionFacts(
        duplicate=blank,
        context_duplicate=blank,
        evidence_sufficient=None,
        insufficient_independent_value=None,
        remaining_small=None,
        dependency_too_strong=None,
        cost_exceeds_benefit=None,
    )


def blocking_facts() -> AdmissionFacts:
    """``duplicate`` 明确成立 ⇒ L47 停止条件 ⇒ REJECT。"""
    return AdmissionFacts(
        duplicate=RelationFact(status=True, related_branch_id=new_branch_id()),
        context_duplicate=RelationFact(status=None, related_branch_id=None),
        evidence_sufficient=None,
        insufficient_independent_value=None,
        remaining_small=None,
        dependency_too_strong=None,
        cost_exceeds_benefit=None,
    )


class ControlRecorder:
    """记录控制 seam 的调用次数与返回值（B17 / B18）。"""

    def __init__(
        self, *, control: RuntimeControlFacts, timestamp: str = "2026-09-18T07:00:00Z"
    ) -> None:
        self._control = control
        self.timestamp = timestamp
        self.calls = 0

    def __call__(self) -> tuple[RuntimeControlFacts, str]:
        self.calls += 1
        return self._control, self.timestamp


def no_result(_branch_id: str):
    """``reusable_result_source`` 的"没有已验证可复用结果"返回值。"""
    return None


class Harness:
    """一套装配好的 P1 + `[10]` + 假适配器 + 控制 seam。"""

    def __init__(
        self,
        *,
        adapter=None,
        control: RuntimeControlFacts | None = None,
        facts_source=None,
        limit: int = 4,
    ) -> None:
        self.first = new_branch_id()
        self.second = new_branch_id()
        self.adapter = adapter if adapter is not None else FakeAdapter(clock=clock)
        self.manager = AgentManager(
            run_id=new_run_id(),
            branch_order=(self.first, self.second),
            total_attempt_limit=2,
        )
        self.recorder = ControlRecorder(
            control=control or RuntimeControlFacts(stop_branches_requested=False)
        )
        self.compositor = ExecutionCompositor(
            scheduler=Scheduler(),
            agent_manager=self.manager,
            adapter=self.adapter,
            admission_facts_source=facts_source or (lambda _b: unknown_facts()),
            reusable_result_source=no_result,
            continuation_source=lambda: None,
        )
        self.routing = routing_plan(self.first, self.second)
        self.limit = limit

    def bundles(self) -> dict[str, ContextBundle]:
        return {
            self.first: context_bundle(f"goal-{self.first}"),
            self.second: context_bundle(f"goal-{self.second}"),
        }

    def pass_(
        self, *, active: int = 0, first_pass: bool = True, control_source=None
    ) -> ExecutionPassReport:
        return self.compositor.run_pass(
            routing=self.routing,
            bundles=self.bundles(),
            capability=capability(concurrency_limit=self.limit),
            runtime_facts=SchedulingRuntimeFacts(active_subagent_count=active),
            control_facts_source=control_source or self.recorder,
            first_pass=first_pass,
        )

    def stop_all(self, *, timestamp: str = "2026-09-18T09:00:00Z") -> ExecutionPassReport:
        return self.pass_(
            control_source=ControlRecorder(
                control=RuntimeControlFacts(stop_branches_requested=True),
                timestamp=timestamp,
            )
        )


# ==========================================================================
# 结构锁 L12-1 – L12-16（静态 · 真实源码）
# ==========================================================================


class TestL12_1_import_boundary:
    def test_exact_token_set(self) -> None:
        tokens = set(imported_module_tokens(EXECUTION_SOURCE))
        assert tokens == set(ALLOWED_IMPORT_TOKENS), (
            f"新增：{tokens - set(ALLOWED_IMPORT_TOKENS)}；"
            f"消失：{set(ALLOWED_IMPORT_TOKENS) - tokens}"
        )

    def test_forbidden_imports_empty(self) -> None:
        assert forbidden_import_hits(EXECUTION_SOURCE, FORBIDDEN_IMPORTS) == set()

    @pytest.mark.parametrize(
        "needle",
        [
            "\nfrom .policy import EffectivePolicy  # noqa\n",
            "\nfrom . import cost  # noqa\n",
            "\nfrom .router import AdaptiveRouter  # noqa\n",
            "\nfrom . import entrypoint  # noqa\n",
        ],
    )
    def test_probe_forbidden_import_goes_red(self, needle: str) -> None:
        assert forbidden_import_hits(EXECUTION_SOURCE + needle, FORBIDDEN_IMPORTS) != set()


class TestL12_2_only_p1_imports_adapters:
    def test_real_repository(self) -> None:
        """P1 是 Core 内**唯一** import `adapters` 的模块（= L11-2 的实现轮形态）。"""
        sources = {
            path.relative_to(SRC).as_posix(): path.read_text(encoding="utf-8")
            for path in sorted(SRC.rglob("*.py"))
            if "__pycache__" not in str(path)
        }
        assert modules_importing(sources, "adapters") == ("execution.py",)

    def test_detector_not_vacuous(self) -> None:
        assert modules_importing({"a.py": "from . import adapters\n"}, "adapters") == ("a.py",)


class TestL12_3_no_dto_leaks_into_agent_manager:
    def test_commit_started_gets_attribute_reads_only(self) -> None:
        shapes = call_keyword_shapes(EXECUTION_SOURCE, "commit_started")
        assert shapes["handle"] == ("outcome.handle",)
        assert shapes["started_at"] == ("outcome.started_at",)
        assert shapes["branch_id"] == ("request.branch_id",)
        assert shapes["attempt"] == ("request.attempt",)

    def test_probe_passing_the_receipt_goes_red(self) -> None:
        mutated = EXECUTION_SOURCE.replace(
            "handle=outcome.handle,\n                    started_at=outcome.started_at,",
            "handle=outcome,\n                    started_at=outcome,",
        )
        assert mutated != EXECUTION_SOURCE, "探针失效：替换未命中"
        assert call_keyword_shapes(mutated, "commit_started")["handle"] != ("outcome.handle",)


class TestL12_4_no_run_state_writes:
    def test_no_agent_run_construction(self) -> None:
        assert calls_to(EXECUTION_SOURCE, ("AgentRun",)) == []

    def test_no_branch_state_reference(self) -> None:
        names = {
            node.attr
            for node in ast.walk(ast.parse(EXECUTION_SOURCE))
            if isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "BranchState"
        }
        assert names == set(), names

    def test_probe_goes_red(self) -> None:
        mutated = EXECUTION_SOURCE + "\n\n\ndef _sneak(**kw):\n    return AgentRun(**kw)\n"
        assert calls_to(mutated, ("AgentRun",)) != []


class TestL12_5_no_plan_construction:
    @pytest.mark.parametrize("symbol", ["RoutingPlan(", "Plan(", "ModelSelection("])
    def test_absent(self, symbol: str) -> None:
        assert symbol not in EXECUTION_SOURCE, symbol

    def test_probe_goes_red(self) -> None:
        mutated = EXECUTION_SOURCE + "\n\n\ndef _sneak(plan):\n    return RoutingPlan(plan=plan)\n"
        assert "RoutingPlan(" in mutated


class TestL12_6_no_policy_and_no_control_production:
    def test_no_policy_import(self) -> None:
        assert forbidden_import_hits(EXECUTION_SOURCE, ("policy",)) == set()

    def test_control_facts_is_never_constructed(self) -> None:
        """P1 只**消费** `RuntimeControlFacts`；构造点 = 0（producer 属 `[02]`，裁定 ②）。"""
        assert calls_to(EXECUTION_SOURCE, ("RuntimeControlFacts",)) == []

    def test_probe_goes_red(self) -> None:
        mutated = (
            EXECUTION_SOURCE
            + "\n\n\ndef _sneak():\n"
            "    return RuntimeControlFacts(stop_branches_requested=False)\n"
        )
        assert calls_to(mutated, ("RuntimeControlFacts",)) != []


class TestL12_7_no_cost:
    def test_no_cost_import_and_no_cost_symbols(self) -> None:
        assert forbidden_import_hits(EXECUTION_SOURCE, ("cost",)) == set()
        for symbol in ("CostEstimator", "CostEstimate", "LedgerEntry", "CostDriverSet"):
            assert symbol not in EXECUTION_SOURCE, symbol


class TestL12_8_no_clock:
    def test_no_clock_calls(self) -> None:
        assert clock_call_hits(EXECUTION_SOURCE) == []

    @pytest.mark.parametrize(
        "needle", ["x = datetime.now()\n", "x = time.time()\n", "x = clock.monotonic()\n"]
    )
    def test_probe_goes_red(self, needle: str) -> None:
        assert clock_call_hits(EXECUTION_SOURCE + "\n\n" + needle) != []


class TestL12_9_signatures:
    def test_run_pass_shape(self) -> None:
        shape = signature_shape(EXECUTION_SOURCE, "ExecutionCompositor", "run_pass")
        assert shape == {
            "positional": ["self"],
            "kwonly": EXPECTED_RUN_PASS_KWONLY,
            "defaults": [False] * 6,
            "vararg": None,
            "kwarg": None,
        }
        # `ended_at` **不是** run_pass 的参数（裁决 ⑤）—— 它只是从控制 seam 解包出的**局部名**
        assert "ended_at" not in shape["kwonly"]

    def test_init_shape(self) -> None:
        shape = signature_shape(EXECUTION_SOURCE, "ExecutionCompositor", "__init__")
        assert shape == {
            "positional": ["self"],
            "kwonly": EXPECTED_INIT_KWONLY,
            "defaults": [False] * 6,
            "vararg": None,
            "kwarg": None,
        }
        assert "control_facts_source" not in shape["kwonly"]

    def test_first_pass_is_an_explicit_switch(self) -> None:
        """裁定 ⑥：`first_pass` 只作**显式开关**，不得由 pending 反推。"""
        body = EXECUTION_SOURCE.split("def _branches_under_admission")[1]
        assert "if first_pass:" in body
        assert "pending_admission_branch_ids" in body

    @pytest.mark.parametrize(
        "needle, replacement",
        [
            (
                "        first_pass: bool,\n    ) -> ExecutionPassReport:",
                "        first_pass: bool = False,\n    ) -> ExecutionPassReport:",
            ),
            (
                "        first_pass: bool,\n    ) -> ExecutionPassReport:",
                "        first_pass: bool,\n        ended_at: str,\n    ) -> ExecutionPassReport:",
            ),
        ],
    )
    def test_probe_shape_change_goes_red(self, needle: str, replacement: str) -> None:
        mutated = EXECUTION_SOURCE.replace(needle, replacement)
        assert mutated != EXECUTION_SOURCE, "探针失效：替换未命中"
        assert signature_shape(
            mutated, "ExecutionCompositor", "run_pass"
        ) != signature_shape(EXECUTION_SOURCE, "ExecutionCompositor", "run_pass")


class TestL12_10_11_report_shape:
    def test_exactly_five_fields_in_order(self) -> None:
        assert dataclass_field_names(EXECUTION_SOURCE, "ExecutionPassReport") == (
            "decisions",
            "started",
            "mismatches",
            "failures",
            "stopped",
        )

    def test_probe_extra_field_goes_red(self) -> None:
        mutated = EXECUTION_SOURCE.replace(
            "    stopped: tuple[BranchId, ...]",
            "    stopped: tuple[BranchId, ...]\n    extra: tuple[str, ...]",
        )
        assert mutated != EXECUTION_SOURCE, "探针失效：替换未命中"
        assert len(dataclass_field_names(mutated, "ExecutionPassReport")) == 6


class TestL12_12_no_interface_layer:
    def test_no_protocol_no_abc_no_source_class(self) -> None:
        tree = ast.parse(EXECUTION_SOURCE)
        bases = {
            ast.unparse(base)
            for node in ast.walk(tree)
            if isinstance(node, ast.ClassDef)
            for base in node.bases
        }
        assert not any("Protocol" in base or "ABC" in base for base in bases), bases
        class_names = {node.name for node in ast.walk(tree) if isinstance(node, ast.ClassDef)}
        assert not any(name.endswith("Source") for name in class_names), class_names

    def test_probe_source_class_goes_red(self) -> None:
        mutated = EXECUTION_SOURCE + "\n\n\nclass AdmissionFactsSource:\n    pass\n"
        class_names = {
            node.name for node in ast.walk(ast.parse(mutated)) if isinstance(node, ast.ClassDef)
        }
        assert any(name.endswith("Source") for name in class_names)


class TestL12_13_no_silent_defaults_or_io:
    def test_absent(self) -> None:
        assert mapping_get_calls(EXECUTION_SOURCE) == []
        assert open_calls(EXECUTION_SOURCE) == []

    def test_probe_goes_red(self) -> None:
        assert mapping_get_calls(EXECUTION_SOURCE + "\n\nx = m.get('k', 'd')\n") != []
        assert open_calls(EXECUTION_SOURCE + '\n\nx = open("f")\n') != []


class TestL12_14_never_swallows_adapter_error:
    def test_no_except_clause_at_all(self) -> None:
        """P1 **完全不捕获**异常 —— `AdapterError` 必须可见（裁定 ③）。"""
        assert handled_exception_names(EXECUTION_SOURCE) == ()

    def test_substring_scan_would_false_positive(self) -> None:
        """⚠️ 证明 L12-14 **不能**写成子串扫描：docstring 里故意提到 `AdapterError`。"""
        assert EXECUTION_SOURCE.count("AdapterError") >= 1
        assert handled_exception_names(EXECUTION_SOURCE) == ()

    @pytest.mark.parametrize(
        "needle",
        [
            "\n\ndef _sneak(adapter):\n    try:\n        adapter.start()\n"
            "    except AdapterError:\n        pass\n",
            "\n\ndef _sneak(adapter):\n    try:\n        adapter.start()\n"
            "    except Exception:\n        pass\n",
            "\n\ndef _sneak(adapter):\n    try:\n        adapter.start()\n    except:\n        pass\n",
        ],
    )
    def test_probe_goes_red(self, needle: str) -> None:
        assert handled_exception_names(EXECUTION_SOURCE + needle) != ()


class TestL12_15_no_new_exception_type:
    def test_no_exception_subclass(self) -> None:
        assert exception_subclass_names(EXECUTION_SOURCE) == ()
        assert "ExecutionError" not in EXECUTION_SOURCE

    def test_probe_goes_red(self) -> None:
        mutated = EXECUTION_SOURCE + "\n\n\nclass ExecutionError(RuntimeError):\n    pass\n"
        assert exception_subclass_names(mutated) == ("ExecutionError",)


class TestL12_16_control_timestamp_comes_from_upstream:
    def test_exactly_one_two_tuple_unpacking(self) -> None:
        assert len(tuple_unpacking_lines(EXECUTION_SOURCE, "control_facts_source")) == 1

    def test_probe_discarding_the_timestamp_goes_red(self) -> None:
        mutated = EXECUTION_SOURCE.replace(
            "        facts, ended_at = control_facts_source()",
            "        facts = control_facts_source()[0]\n        ended_at = str(facts)",
        )
        assert mutated != EXECUTION_SOURCE, "探针失效：替换未命中"
        assert tuple_unpacking_lines(mutated, "control_facts_source") == ()


# ==========================================================================
# 行为测试 B1 – B18
# ==========================================================================


class TestB1B3AdmissionSource:
    def test_B1_first_pass_uses_all_plan_branches(self) -> None:
        h = Harness()
        report = h.pass_(first_pass=True)
        assert {d.branch_id for d in report.decisions} == {h.first, h.second}
        assert sorted(h.adapter.started) == sorted([(h.first, 1), (h.second, 1)])

    def test_B2_later_pass_uses_pending_ids_only(self) -> None:
        """A11.3c 的**来源义务**：后续 pass 只看 `[10]` 的 pending。"""
        h = Harness()
        h.manager.materialize(
            decisions=(
                DispatchDecision(
                    branch_id=h.first, action=DispatchAction.QUEUE, reason="no slot"
                ),
            ),
            bundles={},
        )
        assert h.manager.pending_admission_branch_ids == (h.first,)
        report = h.pass_(first_pass=False)
        assert {d.branch_id for d in report.decisions} == {h.first}

    def test_B3_decisions_are_passed_through_in_plan_order(self) -> None:
        h = Harness()
        report = h.pass_()
        assert [d.branch_id for d in report.decisions] == [h.first, h.second]
        assert all(d.action is DispatchAction.START for d in report.decisions)


class TestB4B7HappyPaths:
    def test_B4_materialized_queue_is_not_dispatched(self) -> None:
        """容量为 0 ⇒ 两个 child 都 QUEUE ⇒ **零派发**、`[10]` 各有一条 `WAITING`。"""
        h = Harness(limit=4)
        report = h.pass_(active=4)
        assert [d.action for d in report.decisions] == [
            DispatchAction.QUEUE,
            DispatchAction.QUEUE,
        ]
        assert h.adapter.started == []
        assert report.started == ()
        assert report.mismatches == ()
        assert report.failures == ()
        assert {
            record.state for record in h.manager.routing_run.branches
        } == {BranchState.WAITING}

    def test_B5_reject_writes_no_record(self) -> None:
        h = Harness(facts_source=lambda _b: blocking_facts())
        report = h.pass_()
        assert [d.action for d in report.decisions] == [
            DispatchAction.REJECT,
            DispatchAction.REJECT,
        ]
        assert h.manager.routing_run.branches == ()
        assert h.adapter.started == []

    def test_B6_start_commits_running_with_platform_handle(self) -> None:
        h = Harness()
        report = h.pass_()
        record = h.manager.routing_run.branch_by_id(h.first)
        assert record.state is BranchState.RUNNING
        assert record.handle == f"handle-{h.first}-1"
        assert record.started_at == clock()
        assert (h.first, 1, f"handle-{h.first}-1") in report.started
        assert h.manager.active_subagent_count == 2

    def test_B7_handle_reaches_agent_manager_verbatim(self) -> None:
        """L12-3 的行为面：平台句柄**原样**进入 `[10]`。"""
        h = Harness()
        h.pass_()
        assert h.manager.routing_run.branch_by_id(h.second).handle == (
            f"handle-{h.second}-1"
        )


class _CapabilityAdapter(FakeAdapter):
    """`start` 一律回能力不匹配（B8）。"""

    def __init__(self, *, clock, mismatch: CapabilityMismatch) -> None:
        super().__init__(clock=clock)
        self._mismatch = mismatch

    def start(self, *, branch_id, attempt, plan, bundle):
        return self._mismatch


class _FailOnSecondAdapter(FakeAdapter):
    """**第二次**派发时抛 `AdapterError`（B10：验证"先提交的保持已写入"）。"""

    def __init__(self, *, clock) -> None:
        super().__init__(clock=clock)
        self._dispatches = 0

    def start(self, *, branch_id, attempt, plan, bundle):
        self._dispatches += 1
        if self._dispatches == 2:
            raise AdapterError("无法归类的平台异常：???")
        return super().start(
            branch_id=branch_id, attempt=attempt, plan=plan, bundle=bundle
        )


class TestB8B10FailureFlows:
    def test_B8_capability_mismatch_reported_with_branch_id_and_not_committed(self) -> None:
        mismatch = CapabilityMismatch(
            requested="strong/high",
            supported="balanced/medium",
            alternatives=("balanced/medium",),
            detail="platform does not expose high effort",
        )
        h = Harness(adapter=_CapabilityAdapter(clock=clock, mismatch=mismatch))
        report = h.pass_()
        assert report.mismatches == ((h.first, mismatch), (h.second, mismatch))
        assert report.started == ()
        assert h.manager.routing_run.branches == ()  # 无成功事实 ⇒ 不提交 RUNNING

    def test_B9_adapter_failure_reported_with_branch_id(self) -> None:
        h = Harness(adapter=FakeAdapter(clock=clock, start_exc=PlatformNetworkError("reset")))
        report = h.pass_()
        # `P6 / D-P6C″`：witness = 三元组 `(branch_id, attempt, AdapterFailure)`
        assert [branch for branch, _attempt, _ in report.failures] == [h.first, h.second]
        assert all(
            isinstance(f, AdapterFailure) for _branch, _attempt, f in report.failures
        )
        assert [attempt for _branch, attempt, _ in report.failures] == [1, 1]
        # `P6 / D-P6C″`：失败 attempt **已由 `[10]` 落为 `FAILED`**（⛔ 不再是「无记录」）
        records = h.manager.routing_run.branches
        assert len(records) == 2
        assert [r.state for r in records] == [BranchState.FAILED, BranchState.FAILED]
        assert [r.started_at for r in records] == [None, None]
        assert [r.handle for r in records] == [None, None]
        assert [r.ended_at for r in records] == [
            f.timestamp for _b, _a, f in report.failures
        ]  # `ended_at` = `AdapterFailure.timestamp`（逐条 provenance 对齐）

    @pytest.mark.parametrize(
        "exc", [PlatformRefused("no"), PlatformTimeout("slow"), PlatformCrash("boom")]
    )
    def test_B9b_every_kind_maps_through(self, exc: BaseException) -> None:
        h = Harness(adapter=FakeAdapter(clock=clock, start_exc=exc))
        report = h.pass_()
        assert len(report.failures) == 2
        # `P6 / D-P6C″`：失败 attempt **已由 `[10]` 落为 `FAILED`**（⛔ 不再是「无记录」）
        records = h.manager.routing_run.branches
        assert len(records) == 2
        assert [r.state for r in records] == [BranchState.FAILED, BranchState.FAILED]
        assert [r.started_at for r in records] == [None, None]
        assert [r.handle for r in records] == [None, None]
        assert [r.ended_at for r in records] == [
            f.timestamp for _b, _a, f in report.failures
        ]  # `ended_at` = `AdapterFailure.timestamp`（逐条 provenance 对齐）

    def test_B10_adapter_error_propagates_and_keeps_prior_commits(self) -> None:
        """裁定 ③：`AdapterError` 向上抛出；**已提交**的分支保持已写入状态（不回滚）。"""
        h = Harness(adapter=_FailOnSecondAdapter(clock=clock))
        with pytest.raises(AdapterError, match="无法归类"):
            h.pass_()
        # 第一个 child 已经 commit 过 ⇒ 保持 RUNNING（P1 不回滚、不补偿）
        assert h.manager.routing_run.branch_by_id(h.first).state is BranchState.RUNNING
        assert h.manager.active_subagent_count == 1


class TestB11B12B16RuntimeControl:
    def test_B11_stop_all_dispatches_nothing(self) -> None:
        """裁决 ①：stop-all ⇒ **本 pass 不再派发**（"先启动再停止"被禁止）。"""
        h = Harness()
        h.pass_()
        assert h.manager.active_subagent_count == 2
        before = list(h.adapter.started)

        report = h.stop_all()
        assert h.adapter.started == before  # **没有**第二次派发
        assert report.decisions == ()
        assert report.started == ()
        assert set(report.stopped) == {h.first, h.second}
        assert h.manager.active_subagent_count == 0

    def test_B12_false_dispatches_normally(self) -> None:
        h = Harness()
        report = h.pass_()
        assert report.stopped == ()
        assert h.manager.routing_run.branch_by_id(h.first).state is BranchState.RUNNING

    def test_B16_policy_003_path_end_to_end(self) -> None:
        """`TEST-POLICY-003` 的通路**首次**可端到端断言：写 STOPPED **且**不派发。"""
        h = Harness()
        h.pass_()
        report = h.stop_all()
        states = {r.branch_id: r.state for r in h.manager.routing_run.branches}
        assert states == {h.first: BranchState.STOPPED, h.second: BranchState.STOPPED}
        assert report.decisions == ()  # 同一次断言里证明"不派发"


class TestB14B18TimestampAndReport:
    def test_B14_queue_promotion_keeps_attempt_one(self) -> None:
        h = Harness()
        h.manager.materialize(
            decisions=(
                DispatchDecision(
                    branch_id=h.first, action=DispatchAction.QUEUE, reason="no slot"
                ),
            ),
            bundles={},
        )
        report = h.pass_(first_pass=False)
        assert report.started == ((h.first, 1, f"handle-{h.first}-1"),)
        assert [r.attempt for r in h.manager.routing_run.branches] == [1]

    def test_B17_control_source_called_exactly_once_per_pass(self) -> None:
        h = Harness()
        h.pass_()
        assert h.recorder.calls == 1

    def test_B18_timestamp_is_relayed_verbatim(self) -> None:
        """P1 **不造时间**：上游时间戳恒等出现在 `[10]` 的 `STOPPED.ended_at`。"""
        h = Harness()
        h.pass_()
        stamp = "2026-09-18T11:22:33Z"
        h.stop_all(timestamp=stamp)
        assert h.manager.routing_run.branch_by_id(h.first).ended_at == stamp

    def test_B15_report_is_frozen_and_tuple_validated(self) -> None:
        report = ExecutionPassReport(
            decisions=(), started=(), mismatches=(), failures=(), stopped=()
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            report.stopped = ()  # type: ignore[misc]
        with pytest.raises(TypeError):
            ExecutionPassReport(
                decisions=(),
                started=(),
                mismatches=(),
                failures=(),
                stopped=[],  # type: ignore[arg-type]
            )


# ==========================================================================
# `P6 / D-P6C″`（`5B.6.51.4.3-impl`）—— `LP6-1…4 / LP6-9 / LP6-11`
# 范围稿与锁计划：`tests/behavior/README_step5b.md` **§254.6 / §255.4**
# ⚠️ 本块 ⛔ 不改任何既有 Test ID；`LP6-*` 为**新增**编号。
# ==========================================================================


def _repo_src_files() -> list[pathlib.Path]:
    return [p for p in sorted((SRC.parent).rglob("*.py")) if "__pycache__" not in str(p)]


def _repo_call_sites(symbol: str) -> list[tuple[str, int]]:
    """全 `src/` production 调用点（AST · ⛔ 不是子串计数）。"""
    out: list[tuple[str, int]] = []
    for path in _repo_src_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and ast.unparse(node.func).split(".")[-1] == symbol:
                out.append((path.relative_to(SRC.parent.parent).as_posix(), node.lineno))
    return out


def _func(source: str, name: str) -> ast.FunctionDef:
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(name)


def _branch_body(source: str, guard_fragment: str) -> list[ast.stmt]:
    """`_dispatch` 内指定 `isinstance(..., <guard_fragment>)` 分支的语句表。"""
    fn = _func(source, "_dispatch")
    for node in ast.walk(fn):
        if isinstance(node, ast.If) and guard_fragment in ast.unparse(node.test):
            return node.body
    raise AssertionError(guard_fragment)


class TestLP6WitnessShapeAndSettlementOrder:
    """`LP6-1`（`M6` 五件套）· `LP6-3`（branch-local dominance）· `LP6-4`（`M2-b2`）。"""

    def test_LP6_1_M6_five_fold(self) -> None:
        tree = ast.parse(EXECUTION_SOURCE)
        report_cls = next(
            n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "ExecutionPassReport"
        )
        ann = next(
            n for n in report_cls.body if isinstance(n, ast.AnnAssign)
            and ast.unparse(n.target) == "failures"
        )
        # ① annotation lock（⛔ 防"注解二元组、运行时三元组"）
        assert ast.unparse(ann.annotation) == "tuple[tuple[BranchId, int, AdapterFailure], ...]"
        body = _branch_body(EXECUTION_SOURCE, "AdapterFailure")
        append = next(
            n for n in ast.walk(ast.Module(body=body, type_ignores=[]))
            if isinstance(n, ast.Call) and ast.unparse(n.func) == "failures.append"
        )
        (element,) = append.args
        assert isinstance(element, ast.Tuple) and len(element.elts) == 3  # ② arity == 3
        assert ast.unparse(element.elts[0]) == "request.branch_id"  # ③
        assert ast.unparse(element.elts[1]) == "request.attempt"  # ④
        assert ast.unparse(element.elts[2]) == "outcome"  # ⑤ exact outcome

    def test_LP6_3_settlement_is_the_direct_predecessor(self) -> None:
        body = _branch_body(EXECUTION_SOURCE, "AdapterFailure")
        kinds = [ast.unparse(stmt) for stmt in body]
        settle = next(i for i, s in enumerate(kinds) if "commit_start_failure(" in s)
        append = next(i for i, s in enumerate(kinds) if "failures.append(" in s)
        assert settle + 1 == append, kinds  # 直接前驱（dominance · ⛔ lineno 只作辅助）
        for stmt in body[settle + 1 : append]:
            assert not isinstance(
                stmt, (ast.Return, ast.Raise, ast.Continue, ast.Break)
            ), ast.unparse(stmt)

    def test_LP6_4_report_is_built_after_dispatch(self) -> None:
        fn = _func(EXECUTION_SOURCE, "run_pass")
        assign = next(
            n for n in ast.walk(fn)
            if isinstance(n, ast.Assign)
            and "self._dispatch(" in ast.unparse(n.value)
        )
        ret = next(
            n for n in ast.walk(fn)
            if isinstance(n, ast.Return) and "ExecutionPassReport(" in ast.unparse(n)
        )
        assert assign.lineno < ret.lineno


class TestLP6OwnerAndBoundaries:
    """`LP6-2`（repo-wide owner）· `LP6-9`（γ′ 保持）· `LP6-11`（零越界 · 保留既有 authority）。"""

    def test_LP6_2_repo_wide_owner_is_exactly_one(self) -> None:
        sites = _repo_call_sites("commit_start_failure")
        assert len(sites) == 1, sites
        rel, line = sites[0]
        assert rel == "src/adaptive_router/execution.py"
        fn = _func(EXECUTION_SOURCE, "_dispatch")
        branch = _branch_body(EXECUTION_SOURCE, "AdapterFailure")
        assert branch[0].lineno <= line <= branch[-1].end_lineno

    def test_LP6_9_no_graph_reader_added(self) -> None:
        attrs = {
            n.attr for n in ast.walk(ast.parse(EXECUTION_SOURCE)) if isinstance(n, ast.Attribute)
        }
        assert "_graph" not in attrs

    def test_LP6_11_keeps_the_frozen_runtime_control_handoff(self) -> None:
        assert calls_to(EXECUTION_SOURCE, ("AgentRun",)) == []
        assert calls_to(EXECUTION_SOURCE, ("record_outcome",)) == []
        assert calls_to(EXECUTION_SOURCE, ("close_run",)) == []
        # ⚠️ `apply_runtime_control` 是 P1 **既有合法** handoff ⇒ 口径 = 保持 call shape / cardinality
        assert len(calls_to(EXECUTION_SOURCE, ("apply_runtime_control",))) == 1
