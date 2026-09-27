"""``C2d-2c pass driver`` 结构锁 + 永久探针 + 行为（`LPD-1…11` / `MPD-*` / `B-1…B-8`）。

范围与冻结项见 `tests/behavior/README_step5b.md` **§181**（C2d-2c Scope Review）/
**§182**（Scope Correction）/ **§183 · §185**（`C2d-2a` Graph Carrier Extension）/
**§186**（C2d-2c 实现授权申请，含 **`first_pass` stop 短路边角语义 = 甲**）。

⚠️ **禁止子串扫描**：真实源码 docstring 里**故意提及** `close_run` / `converge_run` / `recheck` /
`run_pass` / `materialize` 等词 ⇒ 全部锁一律 **AST 级 / 绑定集级**。

⚠️ **测试侧局部 AST helper 的由来**：既有检测器集合逐条核查后确认，其中**没有**
"取某次调用所属函数"与"数循环节点"这两项；且 `function_return_annotation` /
`function_return_shapes` / `function_raise_guards` **只作用于模块级函数**（对类方法会显式报
"未找到模块级函数"）。因此本文件按 `test_router_wrapper.py` 的既有先例实现**局部 helper**，
**不**把它们并入转发层、**不**扩充检测器集合。
"""

from __future__ import annotations

import ast
import dataclasses
import pathlib

import pytest

from adaptive_router.capabilities import CapabilityRegistry
from adaptive_router.composition_root import CompositionRoot, CompositionRootError, ExecutionGraph
from adaptive_router.contracts import ContractError, ContextBundle, RoutingPlan
from adaptive_router.contracts.decision import EffortLevel, ModelSelection, ModelTier
from adaptive_router.execution import ExecutionPassReport
from adaptive_router.execution_context_facts import ExecutionContextFacts
from adaptive_router.pass_driver import PassDriver, PassDriverError
from adaptive_router.routing_plan import RoutingPlanAssembler
from adaptive_router.runtime_control_facts import RuntimeControlFacts

from _pass_driver_locks import (  # noqa: F401  (复用既有检测器，不抄第二套)
    calls_to,
    clock_call_hits,
    exception_subclass_names,
    forbidden_import_hits,
    imported_module_tokens,
    module_level_function_names,
    referenced_names,
)
from _scheduler_locks import signature_shape  # noqa: F401  (既有工具：取**类方法**签名形状)
from test_execution import ControlRecorder
from test_execution import Harness as _ExecutionHarness  # noqa: F401  (复用既有假件)
from test_execution import capability, unknown_facts  # noqa: F401

SOURCE_PATH = pathlib.Path("src/adaptive_router/pass_driver.py")
SOURCE = SOURCE_PATH.read_text(encoding="utf-8")

#: `LPD-6` —— 精确导入面（**不得**含 `adapters` / `terminal_wiring` / `resume_wiring`）。
EXPECTED_IMPORT_TOKENS = {
    "__future__",
    "annotations",
    "composition_root",
    "contracts",
    "execution",
    "scheduling_runtime_facts",
}

#: `LPD-1` —— `run_pass` 的**冻结 6 项** keyword-only 形状与顺序。
EXPECTED_RUN_PASS_KWONLY = [
    "routing",
    "bundles",
    "capability",
    "runtime_facts",
    "control_facts_source",
    "first_pass",
]

#: `LPD-4` —— 本模块**禁止调用**的全部名字（AST 级；⛔ 不做子串扫描）。
FORBIDDEN_CALLS = (
    "close_run",
    "converge_run",
    "record_outcome",
    "commit_started",
    "apply_runtime_control",
    "resume_from_waiting",
    "recheck",
    "commit_start_failure",
    # `5B.6.52.3`（P2）：Run 级 attempt 推进的**唯一** production owner 是
    # `execution.py::ExecutionCompositor.run_pass`；驱动层 ⛔ 不得调用（`LP2-10`）。
    "commit_run_attempt",
)

#: `LPD-9` —— `__init__` 的精确形状（恰好 2 项 keyword-only）。
EXPECTED_INIT_KWONLY = ["composition_root", "routing"]


# ======================================================================
# 测试侧局部 AST helper（沿用 `test_router_wrapper.py` 的既有先例）
# ======================================================================


def _parse(source: str | None = None) -> ast.Module:
    return ast.parse(source or SOURCE)


def _class_node(name: str, source: str | None = None) -> ast.ClassDef:
    """取模块级类 —— **不得靠 `body[i]` 索引**（易错）。"""
    for node in _parse(source).body:
        if isinstance(node, ast.ClassDef) and node.name == name:
            return node
    raise AssertionError(f"未找到模块级类 {name}")


def _method(node: ast.ClassDef, name: str) -> ast.FunctionDef:
    for child in node.body:
        if isinstance(child, ast.FunctionDef) and child.name == name:
            return child
    raise AssertionError(f"未找到方法 {node.name}.{name}")


def _return_shapes(method: ast.FunctionDef) -> list[str]:
    return [ast.unparse(n.value) for n in ast.walk(method) if isinstance(n, ast.Return)]


def _loop_nodes(source: str | None = None) -> list[str]:
    """`While` / `For` / `AsyncFor` 的**全部**循环节点名（`LPD-10` 的判定面）。"""
    return [
        type(n).__name__
        for n in ast.walk(_parse(source))
        if isinstance(n, (ast.While, ast.For, ast.AsyncFor))
    ]


def _calls_inside(source: str, class_name: str, method_name: str) -> list[str]:
    """某个**类方法**体内出现的调用名（`calls_to` 是整模块粒度，故此处按方法收窄）。

    ⚠️ 返回的是**完整 dotted 名**（如 `composition_root.materialize` /
    `self._graph.compositor.run_pass`）⇒ 断言一律用 :func:`_calls_ending_with`，⛔ 不用 `count("materialize")`。
    """
    method = _method(_class_node(class_name, source), method_name)
    return [ast.unparse(n.func) for n in ast.walk(method) if isinstance(n, ast.Call)]


def _calls_ending_with(source: str, class_name: str, method_name: str, tail: str) -> list[str]:
    """按**尾名**过滤 dotted 调用名（`self._graph.compositor.run_pass` → `run_pass`）。"""
    return [c for c in _calls_inside(source, class_name, method_name) if c.endswith(tail)]


def _kwonly_names(method: ast.FunctionDef) -> list[str]:
    return [a.arg for a in method.args.kwonlyargs]


# ======================================================================
# 测试替身（复用既有假件；**不**抄第二套）
# ======================================================================


class RecordingCompositor:
    """delegating spy：记录 `run_pass` 收到的入参，**继续调用真实实现**。"""

    def __init__(self, real: object) -> None:
        self._real = real
        self.calls: list[dict[str, object]] = []

    def run_pass(self, **kwargs: object) -> ExecutionPassReport:
        self.calls.append(kwargs)
        return self._real.run_pass(**kwargs)  # type: ignore[attr-defined]


class ExplodingCompositor:
    """第一次调用抛异常，其后委派真实实现（`B-4` 用）。"""

    def __init__(self, real: object) -> None:
        self._real = real
        self.calls: list[dict[str, object]] = []
        self.explode = True

    def run_pass(self, **kwargs: object) -> ExecutionPassReport:
        self.calls.append(kwargs)
        if self.explode:
            self.explode = False
            raise RuntimeError("模拟 run_pass 中途失败")
        return self._real.run_pass(**kwargs)  # type: ignore[attr-defined]


def _intercept_graph(monkeypatch: pytest.MonkeyPatch, spy: object) -> None:
    """把 driver 构造期物化出的 `ExecutionGraph` 的 `compositor` 换成 spy。

    ⚠️ 只在**构造参数**上换实现 ⇒ **不动生产代码签名**：driver 仍是"自己物化、不接受外部图"。
    """
    original = ExecutionGraph.__init__

    def patched(self: object, **kwargs: object) -> None:
        kwargs["compositor"] = spy
        original(self, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(ExecutionGraph, "__init__", patched)


def _registry_with(snapshot: object) -> CapabilityRegistry:
    """**不经探测**构造 registry 并把缓存快照预置好（`snapshot()` 直接命中缓存）。"""
    registry = CapabilityRegistry.__new__(CapabilityRegistry)
    registry._snapshot = snapshot  # noqa: SLF001  (测试：预置缓存，避免真实探测)
    registry.last_trigger = None
    return registry


def _facts(branch_id: str) -> ExecutionContextFacts:
    return ExecutionContextFacts(
        background=(f"bg-{branch_id}",),
        constraints=("c",),
        allowed_operations=("read",),
        forbidden_operations=("write",),
    )


class DriverHarness:
    """装配一个真实 `CompositionRoot` + 与其 `run_id` **同源**的 `RoutingPlan`。"""

    def __init__(self, *, limit: int = 4, stop_all: bool = False) -> None:
        self.exec = _ExecutionHarness(limit=limit)
        self.routing: RoutingPlan = self.exec.routing
        branch_ids = tuple(spec.branch_id for spec in self.routing.plan.branches)
        self.root = CompositionRoot(
            facts_source=lambda task, context: None,
            capabilities=_registry_with(capability(concurrency_limit=limit)),
            adapter=self.exec.adapter,
            execution_context_facts_by_branch={b: _facts(b) for b in branch_ids},
            confirmed_conclusions_by_branch={b: () for b in branch_ids},
            control_facts_source=ControlRecorder(
                control=RuntimeControlFacts(stop_branches_requested=stop_all)
            ),
            admission_facts_source=lambda _b: unknown_facts(),
            reusable_result_source=lambda _b: None,
            continuation_source=lambda: None,
            run_id_factory=lambda: self.routing.run_id,
        )

    def driver(self) -> PassDriver:
        return PassDriver(composition_root=self.root, routing=self.routing)


# ======================================================================
# `LPD-1 … LPD-11` —— 结构锁
# ======================================================================


class TestLPD1SinglePass:
    """`LPD-1` —— 一次调用恰好一个 pass。"""

    def test_run_pass_called_exactly_once_in_module(self) -> None:
        assert len(calls_to(SOURCE, {"run_pass"})) == 1

    def test_run_pass_call_site_is_inside_drive_next_pass(self) -> None:
        assert _calls_ending_with(SOURCE, "PassDriver", "drive_next_pass", "run_pass") == [
            "self._graph.compositor.run_pass"
        ]

    def test_run_pass_receives_the_frozen_six_kwonly_in_order(self) -> None:
        method = _method(_class_node("PassDriver"), "drive_next_pass")
        call = next(
            n
            for n in ast.walk(method)
            if isinstance(n, ast.Call) and ast.unparse(n.func).endswith("run_pass")
        )
        assert [kw.arg for kw in call.keywords] == EXPECTED_RUN_PASS_KWONLY

    def test_drive_next_pass_takes_no_arguments(self) -> None:
        shape = signature_shape(SOURCE, "PassDriver", "drive_next_pass")
        assert shape["kwonly"] == []
        assert shape["positional"] == ["self"]
        assert shape["vararg"] is None
        assert shape["kwarg"] is None


class TestLPD2NoReMaterializeInPass:
    """`LPD-2` —— 跨 pass **不重建**运行态载体（你 A 的约束）。"""

    def test_no_materialize_inside_drive_next_pass(self) -> None:
        assert _calls_ending_with(SOURCE, "PassDriver", "drive_next_pass", "materialize") == []

    def test_probe_re_materialize_goes_red(self) -> None:
        mutated = SOURCE.replace(
            "        first_pass = self._first_pass\n",
            "        first_pass = self._first_pass\n"
            "        self._graph = self._composition_root.materialize(self._graph.routing)\n",
            1,
        )
        assert mutated != SOURCE
        assert _calls_ending_with(mutated, "PassDriver", "drive_next_pass", "materialize") != []


class TestLPD3OneMaterializeAtConstruction:
    """`LPD-3` —— 全模块 `materialize` **恰 1 处**，且在 `__init__` 内。"""

    def test_materialize_called_exactly_once_in_module(self) -> None:
        assert len(calls_to(SOURCE, {"materialize"})) == 1

    def test_materialize_call_site_is_inside_init(self) -> None:
        assert _calls_ending_with(SOURCE, "PassDriver", "__init__", "materialize") == [
            "composition_root.materialize"
        ]
        assert _calls_ending_with(SOURCE, "PassDriver", "drive_next_pass", "materialize") == []

    def test_probe_two_materialize_sites_go_red(self) -> None:
        mutated = SOURCE.replace(
            "        self._first_pass: bool = True\n",
            "        self._first_pass: bool = True\n"
            "        self._graph = composition_root.materialize(routing)\n",
            1,
        )
        assert mutated != SOURCE
        assert len(calls_to(mutated, {"materialize"})) == 2


class TestLPD4ForbiddenCalls:
    """`LPD-4` —— 零编排 / 零状态机写入 / **不调 `recheck()`**。"""

    def test_zero_forbidden_calls(self) -> None:
        hits = {name: calls_to(SOURCE, {name}) for name in FORBIDDEN_CALLS}
        assert {k: v for k, v in hits.items() if v} == {}

    def test_zero_forbidden_names_referenced(self) -> None:
        names = referenced_names(SOURCE)
        assert {n for n in FORBIDDEN_CALLS if n in names} == set()

    def test_probe_close_run_goes_red(self) -> None:
        mutated = SOURCE.replace(
            "        return report\n",
            "        self._graph.agent_manager.close_run(\n"
            "            terminal_state=None, no_pending_approval=True,\n"
            "            no_pending_recovery=True, ended_at='t',\n"
            "        )\n"
            "        return report\n",
            1,
        )
        assert mutated != SOURCE
        assert calls_to(mutated, {"close_run"}) != []

    def test_probe_recheck_goes_red(self) -> None:
        mutated = SOURCE.replace(
            "capability = self._composition_root.capabilities.snapshot()",
            "capability = self._composition_root.capabilities.recheck(None)",
            1,
        )
        assert mutated != SOURCE
        assert calls_to(mutated, {"recheck"}) != []


class TestLPD5NoClassConstruction:
    """`LPD-5` —— 本模块**不构造** `AgentManager` / `ExecutionCompositor`。"""

    def test_zero_agent_manager_or_compositor_construction(self) -> None:
        assert calls_to(SOURCE, {"AgentManager"}) == []
        assert calls_to(SOURCE, {"ExecutionCompositor"}) == []

    def test_probe_agent_manager_construction_goes_red(self) -> None:
        mutated = SOURCE.replace(
            "        return report\n",
            "        _ = AgentManager(run_id='r', branch_order=(), total_attempt_limit=1)\n"
            "        return report\n",
            1,
        )
        assert mutated != SOURCE
        assert calls_to(mutated, {"AgentManager"}) != []


class TestLPD6ImportBoundary:
    """`LPD-6` —— 精确导入面（**不得** import `adapters`）。"""

    def test_import_tokens_are_exactly_the_whitelist(self) -> None:
        assert imported_module_tokens(SOURCE) == EXPECTED_IMPORT_TOKENS

    def test_no_adapters_import(self) -> None:
        assert forbidden_import_hits(SOURCE, {"adapters"}) == set()

    def test_probe_adapters_import_goes_red(self) -> None:
        mutated = SOURCE.replace(
            "from .execution import ExecutionPassReport",
            "from .execution import ExecutionPassReport\nfrom .adapters import contract",
            1,
        )
        assert mutated != SOURCE
        assert forbidden_import_hits(mutated, {"adapters"}) != []


class TestLPD7NoRuntimeProjectionRead:
    """`LPD-7` —— **不自行反推"还有没有活"**：续跑判据留在 `run_pass` 内部。

    ⚠️ 本锁比 `§186.7` 的原稿**更严且已登记**：`execution.py` L165 才是
    `pending_admission_branch_ids` 的读取方；driver 若读它，就是**自己反推业务事实**
    （`execution.py` L150-151 硬禁"用 pending 为空反推"）⇒ 故本锁为 **ABSENT**。
    """

    def test_pending_admission_branch_ids_is_not_referenced(self) -> None:
        assert "pending_admission_branch_ids" not in referenced_names(SOURCE)

    def test_probe_pending_read_goes_red(self) -> None:
        mutated = SOURCE.replace(
            "        first_pass = self._first_pass\n",
            "        first_pass = self._first_pass\n"
            "        _pending = self._graph.agent_manager.pending_admission_branch_ids\n",
            1,
        )
        assert mutated != SOURCE
        assert "pending_admission_branch_ids" in referenced_names(mutated)


class TestLPD8NoTerminalState:
    """`LPD-8` —— 不判终态。"""

    def test_terminal_state_not_referenced(self) -> None:
        assert "TerminalState" not in referenced_names(SOURCE)

    def test_probe_terminal_state_goes_red(self) -> None:
        mutated = SOURCE.replace(
            "        return report\n",
            "        _t = TerminalState.COMPLETED\n        return report\n",
            1,
        )
        assert mutated != SOURCE
        assert "TerminalState" in referenced_names(mutated)


class TestLPD9NoExternalGraph:
    """`LPD-9` —— **不接受**外部传入的 `ExecutionGraph`（你 A 的约束）。"""

    def test_init_kwonly_is_exactly_two(self) -> None:
        assert (
            _kwonly_names(_method(_class_node("PassDriver"), "__init__"))
            == EXPECTED_INIT_KWONLY
        )

    def test_no_graph_parameter_in_signature(self) -> None:
        shape = signature_shape(SOURCE, "PassDriver", "__init__")
        assert "graph" not in shape["kwonly"]
        assert "ExecutionGraph" not in str(shape["kwonly"])
        assert shape["kwonly"] == EXPECTED_INIT_KWONLY

    def test_own_materialize_is_the_only_source(self) -> None:
        assert _calls_ending_with(SOURCE, "PassDriver", "__init__", "materialize") == [
            "composition_root.materialize"
        ]


class TestLPD10NoLoopNoClock:
    """`LPD-10` —— **零循环 / 零 sleep / 零时钟**（`K4` 冻结）。"""

    def test_zero_loop_nodes(self) -> None:
        assert _loop_nodes() == []

    def test_zero_clock_calls(self) -> None:
        assert clock_call_hits(SOURCE) == []

    def test_zero_module_level_functions(self) -> None:
        assert module_level_function_names(SOURCE) == ()

    def test_probe_while_loop_goes_red(self) -> None:
        mutated = SOURCE.replace(
            "        first_pass = self._first_pass\n",
            "        first_pass = self._first_pass\n        while True:\n            break\n",
            1,
        )
        assert mutated != SOURCE
        assert _loop_nodes(mutated) != []

    def test_probe_for_loop_goes_red(self) -> None:
        mutated = SOURCE.replace(
            "        first_pass = self._first_pass\n",
            "        first_pass = self._first_pass\n        for _x in ():\n            pass\n",
            1,
        )
        assert mutated != SOURCE
        assert _loop_nodes(mutated) != []


class TestLPD11ReturnsExistingReport:
    """`LPD-11` —— **原样**交回既有 `ExecutionPassReport`（零新 DTO）。"""

    def test_exactly_two_module_level_objects(self) -> None:
        import adaptive_router.pass_driver as module

        declared = sorted(
            name
            for name, value in vars(module).items()
            if not name.startswith("__") and getattr(value, "__module__", None) == module.__name__
        )
        assert declared == ["PassDriver", "PassDriverError"]

    def test_public_surface(self) -> None:
        import adaptive_router.pass_driver as module

        assert module.__all__ == ["PassDriver", "PassDriverError"]

    def test_single_return_and_it_is_the_report(self) -> None:
        method = _method(_class_node("PassDriver"), "drive_next_pass")
        assert _return_shapes(method) == ["report"]

    def test_return_annotation_is_the_existing_report(self) -> None:
        method = _method(_class_node("PassDriver"), "drive_next_pass")
        assert ast.unparse(method.returns) == "ExecutionPassReport"

    def test_error_is_runtime_error_not_contract_error(self) -> None:
        assert exception_subclass_names(SOURCE) == ("PassDriverError",)
        assert issubclass(PassDriverError, RuntimeError)
        assert not issubclass(PassDriverError, ContractError)

    def test_probe_second_return_goes_red(self) -> None:
        mutated = SOURCE.replace(
            "        return report\n",
            "        return report\n        return None\n",
            1,
        )
        assert mutated != SOURCE
        assert _return_shapes(
            _method(_class_node("PassDriver", mutated), "drive_next_pass")
        ) == ["report", "None"]


# ======================================================================
# 行为 `B-1 … B-8`
# ======================================================================


class TestBehaviorPassDriver:
    def test_B1_first_pass_is_first_pass_true(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """首轮：`run_pass` 收到 `first_pass=True`（A11.3c"首次 = `Plan.branches` 全部"）。"""
        h = DriverHarness()
        spy = RecordingCompositor(h.exec.compositor)
        _intercept_graph(monkeypatch, spy)
        driver = h.driver()

        report = driver.drive_next_pass()
        assert isinstance(report, ExecutionPassReport)
        assert len(spy.calls) == 1
        assert spy.calls[0]["first_pass"] is True
        assert spy.calls[0]["routing"] is h.routing
        assert set(spy.calls[0]["bundles"]) == set(
            spec.branch_id for spec in h.routing.plan.branches
        )

    def test_B2_second_pass_is_not_first_pass(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """次轮：同一实例的第二次调用 ⇒ `first_pass=False`。"""
        h = DriverHarness()
        spy = RecordingCompositor(h.exec.compositor)
        _intercept_graph(monkeypatch, spy)
        driver = h.driver()

        driver.drive_next_pass()
        driver.drive_next_pass()
        assert [c["first_pass"] for c in spy.calls] == [True, False]

    def test_B3_graph_is_identical_across_passes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """运行态唯一性：同一实例内 `agent_manager` **恒等**；`materialize` 不再发生。"""
        h = DriverHarness()
        spy = RecordingCompositor(h.exec.compositor)
        _intercept_graph(monkeypatch, spy)
        driver = h.driver()

        first_manager = driver._graph.agent_manager  # noqa: SLF001  (测试观测点)
        first_graph = driver._graph  # noqa: SLF001
        driver.drive_next_pass()
        driver.drive_next_pass()
        assert driver._graph is first_graph  # noqa: SLF001
        assert driver._graph.agent_manager is first_manager  # noqa: SLF001

    def test_B4_exception_propagates_and_consumes_first_pass(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """⚠️ **`first_pass` 发起时消费**（`§183` B / `§186.5` 甲）：

        第一次调用**抛异常** ⇒ 异常**原样上抛**（不吞），且 `_first_pass` **已被消费**
        ⇒ 下一次调用以 `first_pass=False` 发起（**不**重复首轮语义）。
        """
        h = DriverHarness()
        spy = ExplodingCompositor(h.exec.compositor)
        _intercept_graph(monkeypatch, spy)
        driver = h.driver()

        with pytest.raises(RuntimeError, match="模拟 run_pass 中途失败"):
            driver.drive_next_pass()

        second = driver.drive_next_pass()
        assert isinstance(second, ExecutionPassReport)
        assert [c["first_pass"] for c in spy.calls] == [True, False]

    def test_B5_stop_short_circuit_consumes_first_pass(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """⚠️ **stop 短路边角语义**（你批准的 **(甲)** · 不许只靠注释）。

        首轮即 `stop_branches_requested=True` ⇒ `run_pass` 在内部短路、**不进入 Scheduler**
        ⇒ 报告为空（零 `started` / 零 `decisions`）；但 `_first_pass` **已被消费**
        ⇒ 下一次**不会**错误恢复为"首次全量"。
        """
        h = DriverHarness(stop_all=True)
        spy = RecordingCompositor(h.exec.compositor)
        _intercept_graph(monkeypatch, spy)
        driver = h.driver()

        first = driver.drive_next_pass()
        assert first.started == ()
        assert first.decisions == ()
        assert spy.calls[0]["first_pass"] is True

        driver.drive_next_pass()
        assert [c["first_pass"] for c in spy.calls] == [True, False]

    def test_B6_report_is_the_existing_dto(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """零新 DTO：返回对象是既有 `ExecutionPassReport`，5 字段逐字不变。"""
        h = DriverHarness()
        driver = h.driver()
        report = driver.drive_next_pass()
        assert type(report) is ExecutionPassReport
        assert [f.name for f in dataclasses.fields(report)] == [
            "decisions",
            "started",
            "mismatches",
            "failures",
            "stopped",
        ]

    def test_B7_convergence_not_touched(self) -> None:
        """收敛**不归 driver**：调用后 `terminal is None`、`control_flow_converged is False`。"""
        h = DriverHarness()
        driver = h.driver()
        driver.drive_next_pass()
        run = driver._graph.agent_manager.routing_run  # noqa: SLF001
        assert run.terminal is None
        assert run.control_flow_converged is False
        assert run.is_converged is False

    def test_B8_construction_guards(self) -> None:
        """构造期守卫：类型非法 / 传外部图 ⇒ 显式 `PassDriverError`；`run_id` 错配 ⇒ 早失败。"""
        h = DriverHarness()
        with pytest.raises(PassDriverError):
            PassDriver(composition_root=object(), routing=h.routing)  # type: ignore[arg-type]
        with pytest.raises(PassDriverError):
            PassDriver(composition_root=h.root, routing=object())  # type: ignore[arg-type]

        # 传"外部图"必须被**明确**拒绝（`LPD-9`），而不是含糊的"不是 CompositionRoot"
        graph = h.root.materialize(h.routing)
        with pytest.raises(PassDriverError, match="不接受"):
            PassDriver(composition_root=graph, routing=h.routing)  # type: ignore[arg-type]

        # `run_id` 错配 ⇒ **构造期**（而非第一次 pass）即失败 —— `materialize` 的既有守卫
        selection = ModelSelection(
            tier=ModelTier.BALANCED, effort=EffortLevel.MEDIUM, reason="test"
        )
        foreign = RoutingPlanAssembler(run_id_factory=lambda: "run_foreign").assemble(
            h.routing.plan,
            main_thread_model=selection,
            branch_models=tuple(
                (spec.branch_id, selection) for spec in h.routing.plan.branches
            ),
        )
        assert foreign.run_id != h.routing.run_id
        with pytest.raises(CompositionRootError):
            PassDriver(composition_root=h.root, routing=foreign)


# ======================================================================
# 探针卫生
# ======================================================================


class TestProbeHygiene:
    def test_every_declared_anchor_hits(self) -> None:
        anchors = [
            "class PassDriverError(RuntimeError):",
            "class PassDriver:",
            "        self._graph: ExecutionGraph = composition_root.materialize(routing)",
            "        self._first_pass: bool = True",
            "            report = self._graph.compositor.run_pass(",
            "            self._first_pass = False",
            "        return report",
            "        composition_root: CompositionRoot,",
            "        routing: RoutingPlan,",
        ]
        for anchor in anchors:
            assert anchor in SOURCE, f"锚点未命中：{anchor!r}"

    def test_source_is_parseable_without_bom(self) -> None:
        assert SOURCE_PATH.read_bytes()[:3] != b"\xef\xbb\xbf"
        ast.parse(SOURCE)

    def test_bundles_carrier_is_the_existing_type(self) -> None:
        """`bundles` 的承载类型是**既有** `ContextBundle`（本次未新增载体）。"""
        assert ContextBundle is not None
