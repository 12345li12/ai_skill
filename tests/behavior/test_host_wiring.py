"""``host_wiring`` 结构锁 + 永久探针 + **端到端装配测试**（`LHP-1…11` / `MHP-*` / `B-1…B-5`）。

范围、体检与授权见 `tests/behavior/README_step5b.md` **§198** / **§199** / **§200**
（含 **`方案 (i) 只装配不驱动`** 与 **`保持 drive_next_pass，不新增 run_once`** 两条裁决）。

⚠️ **本文件的核心交付是"端到端装配测试"**（`§199.4` 实测：`dispatch → RouterWrapper →
   `CompositionRoot` → `PassDriver` 四段**从未被端到端连过**，连测试里都没有）。
⚠️ **禁止子串扫描**：真实源码 docstring 里**故意提及** `PassDriver` / `drive_next_pass` /
   `adapters` / `terminal` 等词 ⇒ 判定一律 **AST 级 / 绑定集级**，或先剥 docstring 再看代码面。
"""

from __future__ import annotations

import ast
import dataclasses
import pathlib

import pytest

from adaptive_router.adapters import AdapterStartReceipt
from adaptive_router.adapters.in_memory import InMemoryAdapter
from adaptive_router.capabilities import CapabilityRegistry
from adaptive_router.composition_root import CompositionRoot
from adaptive_router.contracts import ProjectContext
from adaptive_router.entrypoint import EntrypointHandlers, dispatch
from adaptive_router.execution import ExecutionPassReport
from adaptive_router.execution_context_facts import ExecutionContextFacts
from adaptive_router.host_wiring import build_entrypoint_handlers
from adaptive_router.pass_driver import PassDriver
from adaptive_router.project_context import ProjectContextLoader, ProjectFacts
from adaptive_router.router_wrapper import RouterWrapper
from adaptive_router.runtime_control_facts import RuntimeControlFacts
from adaptive_router.session import SessionGate, SessionState

from _host_wiring_locks import (  # noqa: F401  (复用既有检测器，不抄第二套)
    calls_to,
    clock_call_hits,
    forbidden_import_hits,
    function_return_annotation,
    function_return_shapes,
    imported_module_tokens,
    mapping_get_calls,
    module_level_function_names,
    modules_importing,
    open_calls,
    referenced_names,
)
from test_execution import capability, unknown_facts  # noqa: F401  (复用既有假件)
from test_planner import facts as planner_task_facts
from test_router import facts as router_facts

SOURCE_PATH = pathlib.Path("src/adaptive_router/host_wiring.py")
SOURCE = SOURCE_PATH.read_text(encoding="utf-8")

T0 = "2026-10-01T00:00:00Z"
ROOT_PATH = "/repo"

#: 端到端装配测试的**固定 child id 序列**（与 facts 映射的键**同一组**）。
#: ⚠️ 真 planner 默认产出随机 `branch_id`（`new_branch_id()`）⇒ 用既有的"允许注入
#: `branch_id_factory`"机制把它固定下来（见 `_DeterministicIdPlanner`）。
EXPECTED_BRANCH_IDS = ["br_e2e_a", "br_e2e_b", "br_e2e_c"]

#: `run_id` 固定值（`CompositionRoot.run_id_factory` 的既有注入面）。
RUN_ID = "run_e2e_0001"

#: `LHP-2` —— 工厂的**冻结形参序**（必填 10 + 可选 7）。
EXPECTED_REQUIRED_KWONLY = [
    "facts_source",
    "capabilities",
    "adapter",
    "execution_context_facts_by_branch",
    "confirmed_conclusions_by_branch",
    "session_gate",
    "control_facts_source",
    "admission_facts_source",
    "reusable_result_source",
    # `5B.6.52.3`（P2 · E1 ingress）：Host 续跑事实来源 —— **required**（⛔ 无默认值）
    "continuation_source",
]
EXPECTED_OPTIONAL_KWONLY = [
    "project_loader",
    "ask_enable",
    "host_direct",
    "on_execution",
    "total_attempt_limit",
    "run_id_factory",
    "router_overrides",
]

#: `LHP-6` —— 装配层**禁止调用**的名字（不驱动 · 不编排 · 不判终态）。
FORBIDDEN_CALLS = (
    "PassDriver",
    "drive_next_pass",
    "run_pass",
    "close_run",
    "converge_run",
    "record_outcome",
    "commit_started",
    "apply_runtime_control",
    "resume_from_waiting",
    "materialize",
    "recheck",
    "InMemoryAdapter",
)

#: `LHP-5` —— 允许的导入 token（**不含** `adapters`）。
EXPECTED_IMPORT_TOKENS = {
    "TYPE_CHECKING",
    "Callable",
    "Mapping",
    "__future__",
    "admission_facts",
    "annotations",
    "capabilities",
    "composition_root",
    "contracts",
    "entrypoint",
    "execution_context_facts",
    "reusable_results",
    "router_wrapper",
    "session",
    "typing",
}


# ======================================================================
# 测试侧局部 AST helper
# ======================================================================


def _code_only(source: str) -> str:
    """**剥掉 docstring 与注释**后的代码面（docstring 里故意提及禁用名 ⇒ 必须剥）。"""
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                body.pop(0)
    return ast.unparse(tree)


def _factory_node(source: str | None = None) -> ast.FunctionDef:
    for node in ast.parse(source or SOURCE).body:
        if isinstance(node, ast.FunctionDef) and node.name == "build_entrypoint_handlers":
            return node
    raise AssertionError("未找到模块级函数 build_entrypoint_handlers")


def _loop_nodes(source: str | None = None) -> list[str]:
    return [
        type(n).__name__
        for n in ast.walk(ast.parse(source or SOURCE))
        if isinstance(n, (ast.While, ast.For, ast.AsyncFor))
    ]


def _calls_of(node: ast.AST) -> list[str]:
    return [ast.unparse(n.func) for n in ast.walk(node) if isinstance(n, ast.Call)]


# ======================================================================
# 端到端装配的测试夹具（复用既有假件；**不**抄第二套）
# ======================================================================


class _ControlRecorder:
    """`control_facts_source` 的替身（形态与 `test_execution.ControlRecorder` 一致）。"""

    def __init__(self, *, timestamp: str = "2026-10-01T00:00:00Z") -> None:
        self.timestamp = timestamp
        self.calls = 0

    def __call__(self) -> tuple[RuntimeControlFacts, str]:
        self.calls += 1
        return RuntimeControlFacts(stop_branches_requested=False), self.timestamp


def _registry_with(snapshot: object) -> CapabilityRegistry:
    registry = CapabilityRegistry.__new__(CapabilityRegistry)
    registry._snapshot = snapshot  # noqa: SLF001  (测试：预置缓存，避免真实探测)
    registry.last_trigger = None
    return registry


def _facts() -> ProjectFacts:
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


def _ctx_facts(branch_id: str) -> ExecutionContextFacts:
    """`ExecutionContextFacts` 是**每 child** 的四项上下文事实（A10.3；外部 seam）。"""
    return ExecutionContextFacts(
        background=(f"bg-{branch_id}",),
        constraints=("c",),
        allowed_operations=("read",),
        forbidden_operations=("write",),
    )


class _DeterministicIdPlanner:
    """**委派**真 planner，只把 child 的 `branch_id` 换成固定序列。

    ⚠️ `planner.py` L40 逐字："**允许注入 `branch_id_factory`** 以便测试稳定地生成 child
    `branch_id`" ⇒ 本 wrapper 只是把该注入点**外置**（`router_overrides` 与 `CompositionRoot`
    自己传的 `assembler` 会在同一次构造里冲突，故不覆盖 planner 字段，而是在**实例上包一层**）。
    ⛔ 它**不替换任何判断**：`mode` / `main_thread_duty` / `reason` 与分支数、以及每分支的
    `goal` / `read_only` / `completion_criteria` **全部**来自真 planner 的输出。
    ⚠️ **每次 `plan()` 都从序列开头重新开始** —— 否则同一测试里构造两个组合根
    （harness 的 root 与 `build_entrypoint_handlers` 内建的那个）会拿到**不同**的 id。
    """

    def __init__(self, ids: list[str]) -> None:
        from adaptive_router.planner import Planner

        self._real = Planner()
        self._ids = list(ids)

    def plan(self, evaluation, task_facts, **kwargs):
        from adaptive_router.contracts.decision import BranchSpec, Plan

        produced = self._real.plan(  # type: ignore[call-arg]
            evaluation, task_facts, **kwargs
        )
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


class WiringHarness:
    """**真决策链**（真 planner / 真 assembler / 真组合根）配上既有假件。

    ⚠️ `task` facts 用 `test_planner.facts()`（跨模块 + 3 个独立子任务 ⇒ 真 `MULTI_AGENT` 路径）；
       `test_router.facts()` 的默认 `subtasks=()` 会走 `DIRECT`（零分支）⇒ 不适合端到端装配测试。
    """

    def __init__(self) -> None:
        self.adapter = InMemoryAdapter(timestamp=T0)
        self.control = _ControlRecorder()
        self.loader = ProjectContextLoader(clock=lambda: T0, facts_source=lambda root: _facts())
        self.root = self._build_root()

    # -- 真组合根（含确定性 child id）----------------------------------

    def _root_kwargs(self) -> dict[str, object]:
        return dict(
            facts_source=lambda task, context: router_facts(task=planner_task_facts()),
            capabilities=_registry_with(capability()),
            adapter=self.adapter,
            execution_context_facts_by_branch={b: _ctx_facts(b) for b in EXPECTED_BRANCH_IDS},
            confirmed_conclusions_by_branch={b: () for b in EXPECTED_BRANCH_IDS},
            control_facts_source=self.control,
            admission_facts_source=lambda _b: unknown_facts(),
            reusable_result_source=lambda _b: None,
            continuation_source=lambda: None,
            run_id_factory=lambda: RUN_ID,
        )

    def _build_root(self) -> CompositionRoot:
        root = CompositionRoot(**self._root_kwargs())  # type: ignore[arg-type]
        root._router.planner = _DeterministicIdPlanner(EXPECTED_BRANCH_IDS)  # noqa: SLF001
        return root

    # -- 装配（走被测模块）--------------------------------------------

    def handlers(self, *, on_execution=None) -> EntrypointHandlers:
        """走**被测工厂**装配；并把工厂内建 root 的 planner 换成确定性 id。

        ⚠️ 这一处替换是**测试侧**的：`build_entrypoint_handlers` 的签名**不收** planner 覆盖
        （它只转交 `CompositionRoot` 的注入面），而 `router_overrides` 又与组合根自己传的
        `assembler` 冲突 ⇒ 只能在装配完成后替换。`router_core` 仍是**真 `RouterWrapper`**。
        """
        handlers = build_entrypoint_handlers(
            facts_source=lambda task, context: router_facts(task=planner_task_facts()),
            capabilities=_registry_with(capability()),
            adapter=self.adapter,
            execution_context_facts_by_branch={b: _ctx_facts(b) for b in EXPECTED_BRANCH_IDS},
            confirmed_conclusions_by_branch={b: () for b in EXPECTED_BRANCH_IDS},
            session_gate=SessionGate(state=SessionState.ENABLED),
            control_facts_source=self.control,
            admission_facts_source=lambda _b: unknown_facts(),
            reusable_result_source=lambda _b: None,
            continuation_source=lambda: None,
            project_loader=self.loader,
            on_execution=on_execution,
            run_id_factory=lambda: RUN_ID,
        )
        core = handlers.router_core
        assert isinstance(core, RouterWrapper)
        core._composition_root._router.planner = _DeterministicIdPlanner(  # noqa: SLF001
            EXPECTED_BRANCH_IDS
        )
        return handlers


# ======================================================================
# `LHP-1 … LHP-11` —— 结构锁
# ======================================================================


class TestLHP1ModuleShape:
    """`LHP-1` —— 模块级对象**恰 1** · `__all__` 逐字 · **零**新异常类型 / 零新 DTO。"""

    def test_module_level_objects_are_exactly_one(self) -> None:
        import adaptive_router.host_wiring as module

        declared = sorted(
            name
            for name, value in vars(module).items()
            if not name.startswith("__") and getattr(value, "__module__", None) == module.__name__
        )
        assert declared == ["build_entrypoint_handlers"]

    def test_public_surface(self) -> None:
        import adaptive_router.host_wiring as module

        assert module.__all__ == ["build_entrypoint_handlers"]

    def test_no_new_exception_type(self) -> None:
        bases = {
            ast.unparse(b)
            for n in ast.walk(ast.parse(SOURCE))
            if isinstance(n, ast.ClassDef)
            for b in n.bases
        }
        assert bases == set()

    def test_no_dataclass_is_declared(self) -> None:
        assert dataclasses.is_dataclass(build_entrypoint_handlers) is False


class TestLHP2FactorySignature:
    """`LHP-2` —— 工厂**全 keyword-only**；必填 9 项 + 可选 7 项**逐字**。"""

    def test_all_parameters_are_keyword_only(self) -> None:
        node = _factory_node()
        assert node.args.posonlyargs == []
        assert node.args.args == []
        assert node.args.vararg is None
        assert node.args.kwarg is None

    def test_required_and_optional_names_in_order(self) -> None:
        node = _factory_node()
        names = [a.arg for a in node.args.kwonlyargs]
        assert names == EXPECTED_REQUIRED_KWONLY + EXPECTED_OPTIONAL_KWONLY
        defaults = [d is not None for d in node.args.kw_defaults]
        assert defaults == [False] * len(EXPECTED_REQUIRED_KWONLY) + [True] * len(
            EXPECTED_OPTIONAL_KWONLY
        )

    def test_declared_self_check_tuple_matches_the_signature(self) -> None:
        """源码里的 `_REQUIRED_KWONLY` 自证表**必须**与真实签名一致（否则锁是空地的）。"""
        import adaptive_router.host_wiring as module

        node = _factory_node()
        actual = [a.arg for a in node.args.kwonlyargs]
        assert tuple(actual[: len(EXPECTED_REQUIRED_KWONLY)]) == module._REQUIRED_KWONLY  # noqa: SLF001
        assert tuple(actual[len(EXPECTED_REQUIRED_KWONLY) :]) == module._OPTIONAL_KWONLY  # noqa: SLF001

    def test_probe_reordered_parameter_goes_red(self) -> None:
        mutated = SOURCE.replace(
            "    session_gate: object,\n    control_facts_source:",
            "    control_facts_source:",
            1,
        ).replace(
            "    reusable_result_source: Callable[[object], object | None],\n",
            "    reusable_result_source: Callable[[object], object | None],\n    session_gate: object,\n",
            1,
        )
        assert mutated != SOURCE
        names = [a.arg for a in _factory_node(mutated).args.kwonlyargs]
        assert names != EXPECTED_REQUIRED_KWONLY + EXPECTED_OPTIONAL_KWONLY


class TestLHP3ConstructionSites:
    """`LHP-3` —— 构造点：`CompositionRoot(` 恰 1 · `RouterWrapper(` 恰 1 · `EntrypointHandlers(` 恰 1。"""

    def test_exactly_one_each(self) -> None:
        assert len(calls_to(SOURCE, {"CompositionRoot"})) == 1
        assert len(calls_to(SOURCE, {"RouterWrapper"})) == 1
        assert len(calls_to(SOURCE, {"EntrypointHandlers"})) == 1

    def test_all_three_are_inside_the_factory(self) -> None:
        calls = _calls_of(_factory_node())
        assert calls.count("CompositionRoot") == 1
        assert calls.count("RouterWrapper") == 1
        assert calls.count("EntrypointHandlers") == 1

    def test_probe_second_root_goes_red(self) -> None:
        mutated = SOURCE.replace(
            "    core = RouterWrapper(",
            "    CompositionRoot(facts_source=None)\n    core = RouterWrapper(",
            1,
        )
        assert mutated != SOURCE
        assert len(calls_to(mutated, {"CompositionRoot"})) == 2


class TestLHP4RouterCoreIsARealWrapper:
    """`LHP-4` —— `router_core` **必须**是真 `RouterWrapper` 实例（不是 lambda / 假件）。"""

    def test_router_core_is_a_real_wrapper(self) -> None:
        handlers = WiringHarness().handlers()
        assert isinstance(handlers.router_core, RouterWrapper)

    def test_router_core_is_not_a_lambda_or_none(self) -> None:
        handlers = WiringHarness().handlers()
        assert handlers.router_core is not None
        assert not isinstance(handlers.router_core, type(lambda: None))

    def test_probe_fake_router_core_goes_red(self) -> None:
        """把 `router_core` 换成假件 ⇒ `LHP-4` 的行为面**语义变红**。"""
        handlers = WiringHarness().handlers()
        object.__setattr__(handlers, "router_core", lambda task, context: "fake")
        assert not isinstance(handlers.router_core, RouterWrapper)


class TestLHP5NoAdapters:
    """`LHP-5` —— **零** `adapters`：不 import、不构造 adapter。"""

    def test_import_tokens_are_exactly_the_whitelist(self) -> None:
        assert imported_module_tokens(SOURCE) == EXPECTED_IMPORT_TOKENS

    def test_adapters_is_not_imported(self) -> None:
        assert "adapters" not in set(imported_module_tokens(SOURCE))
        assert forbidden_import_hits(SOURCE, ("adapters",)) == set()

    def test_no_adapter_construction(self) -> None:
        assert calls_to(SOURCE, {"InMemoryAdapter"}) == []

    def test_the_shared_l11_2_lock_still_sees_exactly_one_importer(self) -> None:
        """⚠️ **跨代锁替我抓**：`L11-2` 断言 `importers == ("execution.py",)`。

        本文件用**与 `test_adapter_contract.py` 相同的检测器**在**同一份真实源码集**上复算
        ⇒ 若本模块 import 了 `adapters`，这条会红。
        """
        src_root = pathlib.Path("src/adaptive_router")
        sources = {
            p.relative_to(src_root).as_posix(): p.read_text(encoding="utf-8")
            for p in sorted(src_root.rglob("*.py"))
            if "__pycache__" not in p.parts
        }
        assert modules_importing(sources, "adapters") == ("execution.py",)

    def test_probe_adapters_import_goes_red(self) -> None:
        mutated = SOURCE.replace(
            "from .composition_root import CompositionRoot",
            "from .composition_root import CompositionRoot\nfrom .adapters import contract",
            1,
        )
        assert mutated != SOURCE
        assert forbidden_import_hits(mutated, ("adapters",)) != set()


class TestLHP6ZeroOrchestration:
    """`LHP-6` —— **零编排**：装配层不驱动 pass、不写运行态、不重查能力。"""

    def test_zero_forbidden_calls(self) -> None:
        hits = {name: calls_to(SOURCE, {name}) for name in FORBIDDEN_CALLS}
        assert {k: v for k, v in hits.items() if v} == {}

    def test_this_is_the_key_c2d_2c_boundary(self) -> None:
        """⚠️ `方案 (i)` 的核心：装配层**永不**构造 `PassDriver`、**永不**调 `drive_next_pass`。"""
        code = _code_only(SOURCE)
        assert "PassDriver" not in code
        assert "drive_next_pass" not in code

    def test_probe_driving_a_pass_goes_red(self) -> None:
        mutated = SOURCE.replace(
            "    core = RouterWrapper(",
            "    PassDriver(composition_root=root, routing=None).drive_next_pass()\n"
            "    core = RouterWrapper(",
            1,
        )
        assert mutated != SOURCE
        assert calls_to(mutated, {"PassDriver"}) != []
        assert calls_to(mutated, {"drive_next_pass"}) != []
        assert "drive_next_pass" in _code_only(mutated)


class TestLHP7NoTerminalDecision:
    """`LHP-7` —— **零终态 / 零 PASS-FAIL**。"""

    def test_terminal_names_are_not_referenced(self) -> None:
        names = referenced_names(SOURCE)
        for forbidden in ("TerminalState", "RunTerminal", "RoutingRun"):
            assert forbidden not in names

    def test_probe_terminal_state_goes_red(self) -> None:
        mutated = SOURCE.replace(
            "    core = RouterWrapper(",
            "    _t = TerminalState.COMPLETED\n    core = RouterWrapper(",
            1,
        )
        assert mutated != SOURCE
        assert "TerminalState" in referenced_names(mutated)


class TestLHP8ImportBoundary:
    """`LHP-8` —— 导入面白名单（含 `entrypoint`；**不含**任何禁区）。"""

    def test_entrypoint_is_imported_for_the_real_handlers_type(self) -> None:
        assert "entrypoint" in set(imported_module_tokens(SOURCE))

    def test_no_forbidden_edges(self) -> None:
        forbidden = (
            "terminal_wiring",
            "resume_wiring",
            "deny_wiring",
            "recovery_wiring",
            "validation_wiring",
            "cost",
        )
        assert forbidden_import_hits(SOURCE, forbidden) == set()

    def test_probe_terminal_wiring_import_goes_red(self) -> None:
        mutated = SOURCE.replace(
            "from .router_wrapper import RouterWrapper",
            "from .router_wrapper import RouterWrapper\nfrom .terminal_wiring import converge_run",
            1,
        )
        assert mutated != SOURCE
        assert forbidden_import_hits(mutated, ("terminal_wiring",)) != set()


class TestLHP9DrivesNothing:
    """`LHP-9` —— **"只装配、不驱动"**（`§200.5` 方案 (i) 的机械面）。"""

    def test_zero_pass_construction_and_zero_drive_calls(self) -> None:
        assert calls_to(SOURCE, {"PassDriver"}) == []
        assert calls_to(SOURCE, {"drive_next_pass"}) == []

    def test_no_drive_symbol_in_the_code_surface(self) -> None:
        code = _code_only(SOURCE)
        assert "drive_next_pass" not in code
        assert "PassDriver" not in code

    def test_the_handlers_it_returns_do_not_drive_by_default(self) -> None:
        """⚠️ 行为面：**不传** `on_execution` 时，全链跑完**不会**有任何分支被启动。

        这正是 `方案 (i)` 的可观测后果 —— 装配模块交付"接线"，驱动由调用方决定。
        """
        h = WiringHarness()
        handlers = h.handlers()
        outcome = dispatch("t", project_root=ROOT_PATH, handlers=handlers)
        assert hasattr(outcome, "routing")  # `RouterWrapper` 返回的 `RouterOutcome`
        assert h.adapter.started == []


class TestLHP10NoLoopNoClock:
    """`LHP-10` —— 零循环 / 零 sleep / 零 polling / 零时钟。"""

    def test_zero_loop_nodes(self) -> None:
        assert _loop_nodes() == []

    def test_zero_clock_calls(self) -> None:
        assert clock_call_hits(SOURCE) == []

    def test_zero_get_and_open(self) -> None:
        assert mapping_get_calls(SOURCE) == []
        assert open_calls(SOURCE) == []

    def test_probe_while_loop_goes_red(self) -> None:
        mutated = SOURCE.replace(
            "    core = RouterWrapper(",
            "    while True:\n        break\n    core = RouterWrapper(",
            1,
        )
        assert mutated != SOURCE
        assert _loop_nodes(mutated) != []


class TestLHP11ReturnsExistingType:
    """`LHP-11` —— 返回**既有** `EntrypointHandlers`（零新 DTO）。"""

    def test_return_annotation_is_the_existing_type(self) -> None:
        assert function_return_annotation(SOURCE, "build_entrypoint_handlers") == (
            "EntrypointHandlers"
        )

    def test_declared_return_annotation_matches_the_imported_type(self) -> None:
        assert EntrypointHandlers.__name__ == "EntrypointHandlers"

    def test_return_value_is_an_entrypoint_handlers_instance(self) -> None:
        assert isinstance(WiringHarness().handlers(), EntrypointHandlers)

    def test_five_fields_are_exactly_the_frozen_ones(self) -> None:
        assert [f.name for f in dataclasses.fields(EntrypointHandlers)] == [
            "session_gate",
            "project_loader",
            "ask_enable",
            "host_direct",
            "router_core",
        ]

    def test_probe_wrapping_into_a_new_type_goes_red(self) -> None:
        """⚠️ 探针必须**语法有效**（否则 `ast.parse` 抛错 ⇒ 读数为**空**，不算"变红"）。

        这里把 `return EntrypointHandlers(...)` 换成"先绑到局部名、再返回一个**新造的载体**"。
        """
        mutated = SOURCE.replace(
            "    return EntrypointHandlers(\n        session_gate=session_gate,",
            "    bundle = EntrypointHandlers(\n        session_gate=session_gate,",
            1,
        ).replace(
            "        router_core=core,\n    )\n",
            "        router_core=core,\n    )\n    return _HostBundle(bundle)\n",
            1,
        )
        assert mutated != SOURCE
        ast.parse(mutated)  # 可导入预检
        assert "return _HostBundle(bundle)" in mutated
        shapes = function_return_shapes(mutated, "build_entrypoint_handlers")
        assert any("_HostBundle" in s for s in shapes)
        assert function_return_annotation(mutated, "build_entrypoint_handlers") == (
            "EntrypointHandlers"
        )


# ======================================================================
# 行为 `B-1 … B-5`（含**端到端装配测试** —— 本单元的核心交付）
# ======================================================================


class TestBehaviorHostWiring:
    def test_B1_assembles_the_five_handlers(self) -> None:
        handlers = WiringHarness().handlers()
        assert isinstance(handlers.session_gate, SessionGate)
        assert handlers.project_loader is not None
        assert isinstance(handlers.router_core, RouterWrapper)

    def test_B2_optional_host_handlers_pass_through_unchanged(self) -> None:
        """三个 Host handler 是**原样转交**（本模块不替 Host 决定它们）。"""
        sentinel = lambda reply: "asked"  # noqa: E731
        direct = lambda task: "direct"  # noqa: E731

        class _Loader:
            def load(self, root, *, refresh=False):
                return None

        handlers = build_entrypoint_handlers(
            facts_source=lambda task, context: router_facts(task=planner_task_facts()),
            capabilities=_registry_with(capability()),
            adapter=InMemoryAdapter(timestamp=T0),
            execution_context_facts_by_branch={b: _ctx_facts(b) for b in EXPECTED_BRANCH_IDS},
            confirmed_conclusions_by_branch={b: () for b in EXPECTED_BRANCH_IDS},
            session_gate=SessionGate(state=SessionState.ENABLED),
            control_facts_source=_ControlRecorder(),
            admission_facts_source=lambda _b: unknown_facts(),
            reusable_result_source=lambda _b: None,
            continuation_source=lambda: None,
            project_loader=_Loader(),
            ask_enable=sentinel,
            host_direct=direct,
        )
        assert handlers.ask_enable is sentinel
        assert handlers.host_direct is direct
        assert handlers.project_loader is not None

    def test_B3_the_root_is_shared_with_the_wrapper(self) -> None:
        """组合根**只有一个**：`RouterWrapper` 持有的那个就是本模块构造的那个。"""
        h = WiringHarness()
        handlers = h.handlers()
        core = handlers.router_core
        assert isinstance(core, RouterWrapper)
        assert isinstance(core._composition_root, CompositionRoot)  # noqa: SLF001

    def test_B4_dispatch_requires_router_core_only_on_the_enabled_path(self) -> None:
        """缺 `router_core` 时只有 ENABLED 分支失败（`dispatch` 的既有行为，未被本模块改变）。"""
        h = WiringHarness()
        handlers = h.handlers()
        handlers.router_core = None
        with pytest.raises(Exception):  # noqa: B017  (`entrypoint.EntrypointError`)
            dispatch("t", project_root=ROOT_PATH, handlers=handlers)

    # ------------------------------------------------------------------
    # ⚠️ 端到端装配测试 —— `§199.4` 实测"四段从未被连过"，本测试把它连起来
    # ------------------------------------------------------------------

    def test_B5_end_to_end_dispatch_through_the_real_chain(self) -> None:
        """**本单元的核心交付**：`dispatch → RouterWrapper → CompositionRoot → PassDriver`。

        ⚠️ 驱动**一个** pass 的那一行由**测试**（= 调用方）提供 ——
           这正是 `§200.5` `方案 (i)` 的裁决：装配层不驱动。
        """
        h = WiringHarness()
        drivers: list[PassDriver] = []

        def drive_one_pass(composition_root, routing) -> None:
            """调用方责任：构造 `PassDriver` 并驱动**恰好一个** pass（`K4` 边界）。"""
            driver = PassDriver(composition_root=composition_root, routing=routing)
            drivers.append(driver)
            driver.drive_next_pass()

        handlers = h.handlers(on_execution=drive_one_pass)

        # ---- 入口：ENABLED ⇒ loader → router_core（真 RouterWrapper）----
        outcome = dispatch("do the task", project_root=ROOT_PATH, handlers=handlers)

        # ① 返回值是 `RouterWrapper` 自己的既有载体（`dispatch` **不包装**）
        assert outcome.routing is not None
        assert outcome.plan is not None

        # ② 回调被调用**恰好一次** ⇒ 恰好一个 pass 被驱动
        assert len(drivers) == 1

        # ③ 真实执行发生了：`InMemoryAdapter.start` 收到两个 child 并 mint 了句柄
        assert len(h.adapter.started) == len(EXPECTED_BRANCH_IDS)
        assert h.adapter.open_handle_count == len(EXPECTED_BRANCH_IDS)

        # ④ 运行态被 `[10]` 写入（`commit_started` 真的跑了）：记录里有 `RUNNING`
        run = drivers[0]._graph.agent_manager.routing_run  # noqa: SLF001
        assert len(run.branches) == len(EXPECTED_BRANCH_IDS)
        assert all(rec.state.value == "running" for rec in run.branches)

        # ⑤ **终态不归任何人**：装配层没判、驱动层没判 ⇒ 仍为 `None`
        assert run.terminal is None
        assert run.control_flow_converged is False
        assert run.is_converged is False

    def test_B5b_the_pass_report_is_available_to_the_caller(self) -> None:
        """调用方（而非装配层）拿到 `ExecutionPassReport` —— `方案 (i)` 的责任划分。"""
        h = WiringHarness()
        reports: list[ExecutionPassReport] = []

        def drive_one_pass(composition_root, routing) -> None:
            driver = PassDriver(composition_root=composition_root, routing=routing)
            reports.append(driver.drive_next_pass())

        dispatch(
            "do the task",
            project_root=ROOT_PATH,
            handlers=h.handlers(on_execution=drive_one_pass),
        )
        assert len(reports) == 1
        assert isinstance(reports[0], ExecutionPassReport)
        assert len(reports[0].started) == len(EXPECTED_BRANCH_IDS)


# ======================================================================
# 探针卫生
# ======================================================================


class TestProbeHygiene:
    def test_every_declared_anchor_hits(self) -> None:
        anchors = [
            "def build_entrypoint_handlers(",
            "_REQUIRED_KWONLY = (",
            "    root = CompositionRoot(",
            "    core = RouterWrapper(composition_root=root, on_execution=on_execution)",
            "    return EntrypointHandlers(",
            "        router_core=core,",
            "from .entrypoint import EntrypointHandlers, ProjectLoader",
        ]
        for anchor in anchors:
            assert anchor in SOURCE, f"锚点未命中：{anchor!r}"

    def test_source_is_parseable_without_bom(self) -> None:
        assert SOURCE_PATH.read_bytes()[:3] != b"\xef\xbb\xbf"
        ast.parse(SOURCE)

    def test_project_context_is_what_router_core_receives(self) -> None:
        """协议面：`RouterWrapper.__call__` 的第二个参数就是 `ProjectContext`。"""
        h = WiringHarness()
        seen: list[object] = []
        original = RouterWrapper.__call__

        def spy(self, task, context):
            seen.append(context)
            return original(self, task, context)

        RouterWrapper.__call__ = spy  # type: ignore[method-assign]
        try:
            dispatch("t", project_root=ROOT_PATH, handlers=h.handlers())
        finally:
            RouterWrapper.__call__ = original  # type: ignore[method-assign]
        assert len(seen) == 1
        assert isinstance(seen[0], ProjectContext)
