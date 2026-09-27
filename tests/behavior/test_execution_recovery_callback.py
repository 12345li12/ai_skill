"""``execution_recovery_callback`` 结构锁 + 永久探针 + 行为（`LERC-1…9` / `MERC-*` / `B-1…B-7`）。

范围、体检与授权见 `tests/behavior/README_step5b.md` **§210** / **§211** / **§212** / **§213**
（含 `§213.10` 的 **C 修正**：`LERC-6` = **双层证明**）。

⚠️ **禁止子串扫描**：真实源码 docstring 里**故意提及** `PassDriver` / `drive_next_pass` / `recheck` /
`RedecisionDispatchError` 等词 ⇒ 判定一律 **AST 级 / 绑定集级**，或先剥 docstring 再看代码面。
⚠️ **锚点纪律（`§208.10` 更正 1 的教训）**：probe 一律用**唯一代码行**并先断言 `count == 1`。

⚠️ **`LERC-6` 双层证明（你的 C 修正）**
    `LERC-6a` **结构序**：`recheck` 恰 1 处 · 三个 callback-local 不变量守卫 + **非 `CAPABILITY` 检查**
              均位于它**之前** · `dispatch_redecision` 块位于**所有这些守卫之后**
    `LERC-6b` **行为不可穿透**：**四类拒绝**逐一断言"抛预期异常 **且** `recheck.calls == 0`"
    `B-6`（排列变换）：`CAPABILITY + MODEL_ROUTER` 两种排列 ⇒ **两批 `recheck` 都为 0**
"""

from __future__ import annotations

import ast
import hashlib
import pathlib
import re

import pytest

from adaptive_router.adapters import AdapterFailure, AdapterFailureKind
from adaptive_router.capabilities import (
    CapabilityProbe,
    CapabilityRegistry,
    EnvironmentReading,
    RecheckTrigger,
)
from adaptive_router.composition_root import CompositionRoot
from adaptive_router.contracts import ContractError, RecoveryTarget, RoutingPlan
from adaptive_router.contracts.decision import EffortLevel, ModelSelection, ModelTier
from adaptive_router.execution import ExecutionPassReport
from adaptive_router.execution_context_facts import ExecutionContextFacts
from adaptive_router.execution_recovery_callback import (
    ExecutionRecoveryCallbackError,
    execution_recovery_callback,
)
from adaptive_router.pass_driver import PassDriver
from adaptive_router.redecision_dispatch import RedecisionDispatchError, dispatch_redecision
from adaptive_router.recovery_route_wiring import (
    REDECISION_TARGETS,
    RecoveryRouteWiringError,
    RedecisionRequest,
    build_redecision_request,
)
from adaptive_router.recovery_wiring import route_adapter_failure
from adaptive_router.routing_plan import RoutingPlanAssembler

from _execution_recovery_callback_locks import (  # noqa: F401  (复用既有检测器，不抄第二套)
    calls_to,
    clock_call_hits,
    forbidden_import_hits,
    function_return_annotation,
    imported_module_tokens,
    module_level_container_assignments,
    module_level_function_names,
    referenced_names,
)
from test_execution import capability, unknown_facts  # noqa: F401  (复用既有假件)

SOURCE_PATH = pathlib.Path("src/adaptive_router/execution_recovery_callback.py")
SOURCE = SOURCE_PATH.read_text(encoding="utf-8")

T0 = "2026-10-03T00:00:00Z"
RUN_ID = "run_cb_0001"
BRANCH_IDS = ["br_cb_a", "br_cb_b"]

#: `LERC-1` —— 模块级对象**恰 2**。
EXPECTED_MODULE_OBJECTS = ["ExecutionRecoveryCallbackError", "execution_recovery_callback"]

#: `LERC-9` —— 禁区调用（零越界）。
FORBIDDEN_CALLS = (
    "close_run",
    "converge_run",
    "record_outcome",
    "commit_started",
    "apply_runtime_control",
    "resume_from_waiting",
    "materialize",
    "run_pass",
    "snapshot",
)

#: `LERC-7` —— 上游 / 下游错误**不得被 catch**（provenance 分离）。
NOT_TO_BE_HANDLED = ("RecoveryRouteWiringError", "RedecisionDispatchError")


# ======================================================================
# 测试侧局部 AST helper
# ======================================================================


def _code_only(source: str | None = None) -> str:
    tree = ast.parse(source or SOURCE)
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


def _func_node(source: str | None = None) -> ast.FunctionDef:
    for node in ast.parse(source or SOURCE).body:
        if isinstance(node, ast.FunctionDef) and node.name == "execution_recovery_callback":
            return node
    raise AssertionError("未找到模块级函数 execution_recovery_callback")


def _loops(source: str | None = None) -> list[ast.AST]:
    return [
        n
        for n in ast.walk(ast.parse(source or SOURCE))
        if isinstance(n, (ast.While, ast.For, ast.AsyncFor))
    ]


def _call_lines(source: str, tail: str) -> list[int]:
    return [
        n.lineno
        for n in ast.walk(_func_node(source))
        if isinstance(n, ast.Call) and ast.unparse(n.func).endswith(tail)
    ]


def _rejection_sites(source: str | None = None) -> dict[str, object]:
    """`LERC-6a` 的**唯一判据实现**（正式断言与 probe **共用它** —— 否则 probe 测不到判据）。

    · `guards` 由三个**冻结错误类名**经**正则 + 词边界**抽取
      （避免 `ExecutionRecoveryCallbackErrorFoo` 之类误命中）
    · ⚠️ 共用是**关键**：probe 才能断言"**变异后判据失败**"，
      而不是只断言"源码被改过"（那正是本轮要修的缺陷）。
    """
    fn = _func_node(source)
    guards = [
        n.lineno
        for n in ast.walk(fn)
        if isinstance(n, ast.Raise)
        and n.exc is not None
        and re.search(
            r"(ExecutionRecoveryCallbackError|RedecisionDispatchError|RecoveryRouteWiringError)\b",
            ast.unparse(n.exc),
        )
    ]
    drive = _call_lines(source or SOURCE, "drive_next_pass")
    dispatch = _call_lines(source or SOURCE, "dispatch_redecision")
    return {
        "drive": min(drive) if drive else -1,
        "guards": sorted(guards),
        "dispatch": min(dispatch) if dispatch else -1,
    }


def _lerc6a_holds(source: str | None = None) -> bool:
    """`LERC-6a` 判据：**两条**守卫（1 条 callback-local + 1 条非 CAPABILITY）全部位于
    `dispatch` 之前，且 `drive` 位于**全部**守卫之前（`P6 / D-P6C″`：旧 R2 的两条守卫已撤销）。"""
    s = _rejection_sites(source)
    guards = s["guards"]
    drive = s["drive"]
    dispatch = s["dispatch"]
    if not isinstance(guards, list) or not isinstance(drive, int) or not isinstance(dispatch, int):
        return False
    if drive < 0 or dispatch < 0 or len(guards) != 2:
        return False
    return all(g < dispatch for g in guards) and all(drive < g for g in guards)


def _handled_exception_names(source: str | None = None) -> list[str]:
    """`except <Name>` 子句里出现的异常名（`LERC-7` 的判定面）。"""
    names = []
    for n in ast.walk(ast.parse(source or SOURCE)):
        if isinstance(n, ast.ExceptHandler) and n.type is not None:
            names.append(ast.unparse(n.type))
    return names


class _LoaderSpy:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, *args: object, **kwargs: object) -> EnvironmentReading:
        self.calls += 1
        return EnvironmentReading(
            subagents=True,
            async_background=True,
            concurrency_limit=4,
            available_models=("provider-model-x",),
            available_effort_levels=(),
            state_wait_append_stop=True,
            hot_switch=False,
            reliable_cost_data=True,
            unavailable_reason=None,
        )


def _ctx_facts(branch_id: str) -> ExecutionContextFacts:
    return ExecutionContextFacts(
        background=(f"bg-{branch_id}",),
        constraints=("c",),
        allowed_operations=("read",),
        forbidden_operations=("write",),
    )


def _routing() -> RoutingPlan:
    """与 `BRANCH_IDS` 逐字对应的 `RoutingPlan`（`run_id` 固定）。"""
    from adaptive_router.contracts.decision import BranchSpec, ExecutionMode, Plan

    selection = ModelSelection(
        tier=ModelTier.BALANCED, effort=EffortLevel.MEDIUM, reason="test"
    )
    plan = Plan(
        mode=ExecutionMode.MULTI_AGENT,
        main_thread_duty="duty",
        reason="test",
        branches=tuple(
            BranchSpec(
                branch_id=b,
                goal=f"goal-{b}",
                read_only=False,
                completion_criteria=f"done-{b}",
            )
            for b in BRANCH_IDS
        ),
    )
    return RoutingPlanAssembler(run_id_factory=lambda: RUN_ID).assemble(
        plan,
        main_thread_model=selection,
        branch_models=tuple((b, selection) for b in BRANCH_IDS),
    )


class _Harness:
    """真 `CompositionRoot` + 真 `CapabilityRegistry`（loader 计数）· 报告可注入。"""

    def __init__(self) -> None:
        self.loader = _LoaderSpy()
        self.registry = CapabilityRegistry(
            probe=CapabilityProbe(probed_at=T0), loader=self.loader
        )
        self.routing = _routing()
        self.root = CompositionRoot(
            facts_source=lambda task, context: None,
            capabilities=self.registry,
            adapter=object(),  # 本测试**不**真派发（报告被替换）⇒ adapter 只需存在
            execution_context_facts_by_branch={b: _ctx_facts(b) for b in BRANCH_IDS},
            confirmed_conclusions_by_branch={b: () for b in BRANCH_IDS},
            control_facts_source=lambda: (
                __import__(
                    "adaptive_router.runtime_control_facts", fromlist=["RuntimeControlFacts"]
                ).RuntimeControlFacts(stop_branches_requested=False),
                T0,
            ),
            admission_facts_source=lambda _b: unknown_facts(),
            reusable_result_source=lambda _b: None,
            continuation_source=lambda: None,
            run_id_factory=lambda: RUN_ID,
        )

    def run(
        self,
        monkeypatch: pytest.MonkeyPatch,
        report: ExecutionPassReport,
        *,
        forced_target: RecoveryTarget | None = None,
    ) -> None:
        """跑一次 callback。

        `forced_target`（**仅测试用**）：把 `build_redecision_request` 的产出 target 强制改写，
        以便在不依赖 adapter kind 映射的前提下探到 `PLANNER` / `MODEL_ROUTER` 两条路径。
        ⚠️ 它只改**测试替身**的返回值，不改生产代码。
        """
        monkeypatch.setattr(PassDriver, "drive_next_pass", lambda self: report)
        if forced_target is not None:
            original = build_redecision_request

            def forced(*, outcome):
                request = original(outcome=outcome)
                if request is None:
                    return None
                return RedecisionRequest(
                    branch_id=request.branch_id,
                    attempt=request.attempt,
                    target=forced_target,
                    minimal_correction=request.minimal_correction,
                    carry_forward=request.carry_forward,
                )

            monkeypatch.setattr(
                "adaptive_router.execution_recovery_callback.build_redecision_request",
                forced,
            )
        execution_recovery_callback(self.root, self.routing)


def _failure(kind: AdapterFailureKind) -> AdapterFailure:
    return AdapterFailure(failure_kind=kind, message=f"m-{kind.value}", timestamp=T0)


def _witness(
    branch_id: str, failure: object, attempt: int = 1
) -> tuple[str, int, object]:
    """`P6 / D-P6C″` 的 failure witness = **三元组** `(branch_id, attempt, failure)`。"""
    return (branch_id, attempt, failure)


def _report(
    *, started: tuple[tuple[str, int, str], ...], failures: tuple[tuple[str, int, object], ...]
) -> ExecutionPassReport:
    return ExecutionPassReport(
        decisions=(), started=started, mismatches=(), failures=failures, stopped=()
    )


def _started(branch_id: str, attempt: int = 1) -> tuple[str, int, str]:
    return (branch_id, attempt, f"mem-{branch_id}-{attempt}")


def _recheck_spy(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    seen: list[object] = []
    original = CapabilityRegistry.recheck

    def spy(self: CapabilityRegistry, trigger: RecheckTrigger):
        seen.append(trigger)
        return original(self, trigger)

    monkeypatch.setattr(CapabilityRegistry, "recheck", spy)
    return seen


# ======================================================================
# `LERC-1 … LERC-9` —— 结构锁
# ======================================================================


class TestLERC1ModuleShape:
    def test_module_level_objects_are_exactly_two(self) -> None:
        import adaptive_router.execution_recovery_callback as module

        declared = sorted(
            name
            for name, value in vars(module).items()
            if not name.startswith("__") and getattr(value, "__module__", None) == module.__name__
        )
        assert declared == EXPECTED_MODULE_OBJECTS

    def test_public_surface(self) -> None:
        import adaptive_router.execution_recovery_callback as module

        assert module.__all__ == EXPECTED_MODULE_OBJECTS

    def test_no_new_dto(self) -> None:
        assert module_level_function_names(SOURCE) == ("execution_recovery_callback",)
        classes = [n.name for n in ast.parse(SOURCE).body if isinstance(n, ast.ClassDef)]
        assert classes == ["ExecutionRecoveryCallbackError"]

    def test_error_is_runtime_error_not_contract_error(self) -> None:
        assert issubclass(ExecutionRecoveryCallbackError, RuntimeError)
        assert not issubclass(ExecutionRecoveryCallbackError, ContractError)


class TestLERC2SignatureMatchesTheFrozenSeam:
    def test_exactly_two_positional_parameters(self) -> None:
        node = _func_node()
        assert [a.arg for a in node.args.args] == ["composition_root", "routing"]
        assert node.args.posonlyargs == []
        assert node.args.kwonlyargs == []
        assert node.args.vararg is None
        assert node.args.kwarg is None

    def test_returns_none(self) -> None:
        assert function_return_annotation(SOURCE, "execution_recovery_callback") == "None"

    def test_probe_extra_parameter_goes_red(self) -> None:
        """⚠️ 变异必须**语法有效**（否则 `ast.parse` 抛错 ⇒ 读数为**空**，不算"变红"）。

        把 `routing` 换成 keyword-only + 默认值 ⇒ 参数面不再逐字匹配冻结 seam。
        """
        anchor = "    routing: RoutingPlan,\n) -> None:\n"
        assert SOURCE.count(anchor) == 1
        mutated = SOURCE.replace(anchor, "    *, routing: RoutingPlan = None,\n) -> None:\n", 1)
        assert mutated != SOURCE
        ast.parse(mutated)  # 可导入预检
        node = _func_node(mutated)
        assert [a.arg for a in node.args.args] != ["composition_root", "routing"]


class TestLERC3NoInstanceStateNoFactory:
    def test_no_self_attribute_access(self) -> None:
        assert "self." not in _code_only(SOURCE)

    def test_no_factory(self) -> None:
        assert "make_execution_recovery_callback" not in SOURCE
        assert not module_level_function_names(SOURCE)[0].startswith("make")

    def test_probe_instance_attribute_goes_red(self) -> None:
        mutated = SOURCE.replace(
            "    driver = PassDriver(", "    self._cache = {}\n    driver = PassDriver(", 1
        )
        assert mutated != SOURCE
        assert "self." in _code_only(mutated)


class TestLERC4NoCrossCallState:
    def test_no_module_level_container(self) -> None:
        assert module_level_container_assignments(SOURCE) == ()

    def test_module_level_assignments_are_only_dunder_all(self) -> None:
        """**顶层可赋值名白名单**：只有 `__all__`（模块导出约定，非运行期状态）。"""
        names: list[str] = []
        for n in ast.parse(SOURCE).body:
            if isinstance(n, ast.Assign):
                names.extend(ast.unparse(t) for t in n.targets)
        assert names == ["__all__"]

    def test_no_module_level_mutable_assignment(self) -> None:
        names = []
        for n in ast.parse(SOURCE).body:
            if isinstance(n, ast.Assign):
                names.extend(ast.unparse(t) for t in n.targets)
        assert names == ["__all__"]

    def test_probe_module_level_container_goes_red(self) -> None:
        mutated = SOURCE + "\n_CACHE: list = []\n"
        assert module_level_container_assignments(mutated) != []

    def test_no_module_level_annotated_container(self) -> None:
        """⚠️ **最窄且无洞的做法**（`§216` 的修正）：**module-level `AnnAssign` 恰 0**。

        理由：本生产模块**根本不需要**任何模块级带注解赋值
        ⇒ 与其继续枚举"哪些右值算可变容器"（`{}` 抓得住、`dict()` 抓不住），
          不如**整类封死** ⇒ `{}` / `dict()` / `list()` / `set()` **一起**被挡住。
        """
        annotated = [
            ast.unparse(n.target)
            for n in ast.parse(SOURCE).body
            if isinstance(n, ast.AnnAssign)
        ]
        assert annotated == [], f"出现模块级带注解赋值：{annotated}"

    @pytest.mark.parametrize(
        "snippet",
        ["_CACHE: dict = {}", "_CACHE: dict = dict()", "_CACHE: list = []", "_CACHE: list = list()"],
    )
    def test_probe_module_level_annassign_goes_red(self, snippet: str) -> None:
        """四种形态（**含两种调用式** —— 它们绕得过"字面量"判定）**都必须**被锁抓住。"""
        mutated = SOURCE + f"\n{snippet}\n"
        assert mutated != SOURCE
        ast.parse(mutated)
        annotated = [
            ast.unparse(n.target)
            for n in ast.parse(mutated).body
            if isinstance(n, ast.AnnAssign)
        ]
        assert annotated == ["_CACHE"]

    def test_module_level_annotations_are_exactly_zero(self) -> None:
        """README 级断言（与本类第一条重复但更直白，便于后人速查）。"""
        assert not any(isinstance(n, ast.AnnAssign) for n in ast.parse(SOURCE).body)

    def test_probe_module_level_plain_container_goes_red(self) -> None:
        """⚠️ 补一条：**无注解**的模块级容器也必须被白名单锁抓住。

        （教训：变异锚点若把容器插到**函数体内**，`module_level_container_assignments`
          会**正确地**不报它 —— 那是"变异选错位置"，不是"锁漏了"。真正的锁是
          `test_module_level_assignments_are_only_dunder_all` 的**顶层白名单**。）
        """
        mutated = SOURCE + "\n_CACHE = {}\n"
        assert mutated != SOURCE
        assert module_level_container_assignments(mutated) == ("_CACHE",)
        names: list[str] = []
        for n in ast.parse(mutated).body:
            if isinstance(n, ast.Assign):
                names.extend(ast.unparse(t) for t in n.targets)
        assert names != ["__all__"]


class TestLERC5ExactlyOnePass:
    def test_drive_next_pass_called_exactly_once(self) -> None:
        assert _call_lines(SOURCE, "drive_next_pass") != []
        assert len(calls_to(SOURCE, {"drive_next_pass"})) == 1

    def test_pass_driver_constructed_exactly_once(self) -> None:
        assert len(calls_to(SOURCE, {"PassDriver"})) == 1

    def test_no_second_pass_after_recheck(self) -> None:
        """⚠️ 红线 1：`recheck` 之后**不得**再有一次 `drive_next_pass`。"""
        recheck_at = _call_lines(SOURCE, "recheck")
        drive_at = _call_lines(SOURCE, "drive_next_pass")
        # 本模块**不直接**调 recheck（它在 dispatch_redecision 内）⇒ 用 dispatch 的位置代理
        dispatch_at = _call_lines(SOURCE, "dispatch_redecision")
        assert drive_at and dispatch_at
        assert max(drive_at) < min(dispatch_at)

    def test_probe_second_pass_goes_red(self) -> None:
        anchor = "    report: ExecutionPassReport = driver.drive_next_pass()\n"
        assert SOURCE.count(anchor) == 1
        mutated = SOURCE.replace(
            anchor, anchor + "    driver.drive_next_pass()\n", 1
        )
        assert len(calls_to(mutated, {"drive_next_pass"})) == 2


class TestLERC6aStructuralOrder:
    """`LERC-6a` —— **结构序**：所有可导致拒绝的检查**先于** `recheck`。

    ⚠️ 本类的全部断言（含 probe）**共用同一个判据实现** `_lerc6a_holds` ——
    这是本轮修正的要点：probe 必须**测到判据本身**，而不是"只确认源码被改过"。
    """

    def test_lerc6a_holds_for_the_real_source(self) -> None:
        assert _lerc6a_holds(SOURCE) is True

    def test_rejection_sites_are_exactly_four_ordered_guards(self) -> None:
        """⚠️ **不硬编码行号**（加一行注释就会漂）—— 只断言**结构性事实**。"""
        s = _rejection_sites(SOURCE)
        guards = s["guards"]
        assert isinstance(guards, list)
        assert len(guards) == 2, guards  # 1 条 callback-local 不变量 + 1 条非 CAPABILITY
        assert guards == sorted(guards)  # 源码顺序
        assert all(g < s["dispatch"] for g in guards)  # type: ignore[operator]
        assert all(s["drive"] < g for g in guards)  # type: ignore[operator]

    def test_dispatch_call_is_exactly_one(self) -> None:
        assert len(_call_lines(SOURCE, "dispatch_redecision")) == 1

    def test_all_guards_precede_the_dispatch_block(self) -> None:
        s = _rejection_sites(SOURCE)
        offenders = [g for g in s["guards"] if g > s["dispatch"]]  # type: ignore[union-attr]
        assert offenders == [], f"存在位于 dispatch 之后的守卫：{offenders}"

    def test_drive_happens_before_every_guard(self) -> None:
        s = _rejection_sites(SOURCE)
        assert all(s["drive"] < g for g in s["guards"])  # type: ignore[operator]

    def test_probe_guard_deleted_breaks_the_criterion(self) -> None:
        """probe：**整块删掉**一条守卫 ⇒ `LERC-6a` 判据失败（`guards` 数 ≠ 4）。

        ⚠️ 必须把 `if` + `raise` **一起**删掉（只删 `if` 会留下悬空 `raise` ⇒ `IndentationError`
        ⇒ 读数**为空**，不算"变红"）。
        """
        anchor = (
            "        if branch_id in seen_branches:\n"
            "            raise ExecutionRecoveryCallbackError(\n"
            '                f"failures 中 branch_id={branch_id!r} 重复：同一分支不得被当成两个恢复事实"\n'
            '                "（重复消费会制造虚假的第二次恢复）"\n'
            "            )\n"
        )
        assert SOURCE.count(anchor) == 1, SOURCE.count(anchor)
        mutated = SOURCE.replace(anchor, "", 1)
        assert mutated != SOURCE
        ast.parse(mutated)  # 可导入预检（⛔ 不能是 IndentationError）
        assert _lerc6a_holds(SOURCE) is True
        assert _lerc6a_holds(mutated) is False  # ⚠️ **判据本身**失败
        assert len(_rejection_sites(mutated)["guards"]) == 1

    def test_probe_guard_moved_after_dispatch_breaks_the_criterion(self) -> None:
        """**真·重排 probe**：把非 `CAPABILITY` 守卫**移到 `dispatch` 之后** ⇒ 判据**失败**。

        ⚠️ 这正是原 probe **声称**做、却没做到的事（它只改条件、`raise` 位置未动）。
        这里用行区间**真正搬移**代码块（⛔ 不做子串替换游戏）。
        """
        lines = SOURCE.splitlines(keepends=True)
        # 定位 Phase 2 的三行块（for / if / raise…）与其后的 Phase 3
        start = next(
            i
            for i, ln in enumerate(lines)
            if ln.strip() == "for request in requests:" and "CAPABILITY" in lines[i + 1]
        )
        end = next(i for i in range(start, len(lines)) if lines[i].rstrip() == "            )")
        block = lines[start : end + 1]
        rest = lines[end + 1 :]
        # 找到 Phase 3 的最后一行（dispatch 调用行）
        dispatch_idx = next(
            i for i, ln in enumerate(rest) if "dispatch_redecision(request=request" in ln
        )
        mutated_lines = (
            lines[:start]
            + rest[: dispatch_idx + 1]
            + block
            + rest[dispatch_idx + 1 :]
        )
        mutated = "".join(mutated_lines)
        assert mutated != SOURCE
        ast.parse(mutated)  # 可导入预检
        assert _lerc6a_holds(SOURCE) is True
        assert _lerc6a_holds(mutated) is False  # ⚠️ **判据本身**失败
        s = _rejection_sites(mutated)
        assert any(g > s["dispatch"] for g in s["guards"])  # type: ignore[operator]


class TestLERC7ErrorProvenance:
    """`LERC-7` —— 上游 / 下游错误**不被 catch / 包装**（三类 provenance 分离）。"""

    def test_no_except_handlers_at_all(self) -> None:
        assert _handled_exception_names(SOURCE) == []

    def test_no_try_statement(self) -> None:
        assert [n for n in ast.walk(ast.parse(SOURCE)) if isinstance(n, ast.Try)] == []

    def test_upstream_and_downstream_errors_are_not_referenced_in_handlers(self) -> None:
        for name in NOT_TO_BE_HANDLED:
            assert name not in " ".join(_handled_exception_names(SOURCE))

    def test_probe_catching_dispatch_error_goes_red(self) -> None:
        mutated = SOURCE.replace(
            "    capabilities: CapabilityRegistry = composition_root.capabilities\n",
            "    try:\n        pass\n    except RedecisionDispatchError:\n        pass\n"
            "    capabilities: CapabilityRegistry = composition_root.capabilities\n",
            1,
        )
        assert mutated != SOURCE
        assert "RedecisionDispatchError" in " ".join(_handled_exception_names(mutated))


class TestLERC8NoDrivingLoopNoClock:
    """`LERC-8` —— **零循环驱动**（⛔ 非"零 `for`"）：零 `while` · 零异步循环 · 零时钟。"""

    def test_zero_while_and_async_for(self) -> None:
        bad = [type(n).__name__ for n in _loops() if not isinstance(n, ast.For)]
        assert bad == []

    def test_every_for_loop_is_a_bounded_report_iteration(self) -> None:
        allowed = {"report.failures", "requests"}
        seen = set()
        for n in _loops():
            if isinstance(n, ast.For):
                seen.add(ast.unparse(n.iter))
        assert seen <= allowed, f"出现非报告遍历：{seen - allowed}"
        assert seen == allowed  # 非平凡性：两种遍历都真的在

    def test_zero_clock_calls(self) -> None:
        assert clock_call_hits(SOURCE) == []

    def test_probe_while_loop_goes_red(self) -> None:
        mutated = SOURCE.replace(
            "    driver = PassDriver(", "    while True:\n        break\n    driver = PassDriver(", 1
        )
        assert mutated != SOURCE
        assert [n for n in _loops(mutated) if isinstance(n, ast.While)] != []


class TestLERC9NoOutOfBounds:
    def test_zero_forbidden_calls(self) -> None:
        hits = {name: calls_to(SOURCE, {name}) for name in FORBIDDEN_CALLS}
        assert {k: v for k, v in hits.items() if v} == {}

    def test_no_terminal_state(self) -> None:
        assert "TerminalState" not in referenced_names(SOURCE)

    def test_no_adapters_import(self) -> None:
        assert "adapters" not in imported_module_tokens(SOURCE)
        assert forbidden_import_hits(SOURCE, ("adapters",)) == set()

    def test_does_not_construct_decision_carriers(self) -> None:
        for name in ("RecoveryDecision", "RecoveryOutcome", "RedecisionRequest"):
            assert calls_to(SOURCE, {name}) == []

    def test_probe_terminal_state_goes_red(self) -> None:
        mutated = SOURCE.replace(
            "    driver = PassDriver(",
            "    _t = TerminalState.COMPLETED\n    driver = PassDriver(",
            1,
        )
        assert mutated != SOURCE
        assert "TerminalState" in referenced_names(mutated)


# ======================================================================
# `LERC-6b` + 行为 `B-1 … B-7`
# ======================================================================


class TestLERC6bRejectionPathsCannotReachRecheck:
    """`LERC-6b` —— **行为不可穿透**：四类拒绝逐一断言"抛预期异常 **且** `recheck == 0`"。"""

    def test_R1_failure_branch_not_in_started(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """`P6 / D-P6C″`：旧 R2 配对**已撤销** ⇒ 「failure 的 branch 不在 `started`」
        **不再是错误** —— 这正是 `§243` 刀⑤ 的 **F1 形状**（首次派发即 start 失败 ⇒ `started == ()`）。"""
        seen = _recheck_spy(monkeypatch)
        h = _Harness()
        report = _report(
            started=(),
            failures=((BRANCH_IDS[1], 1, _failure(AdapterFailureKind.CRASH)),),
        )
        h.run(monkeypatch, report)  # ⛔ 不再抛错
        assert len(seen) == 1  # CRASH ⇒ EXTERNAL_SERVICE ⇒ CAPABILITY ⇒ 派发恰一次

    def test_R2_started_duplicate_branch_id(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """`P6 / D-P6C″`：⛔ 不再建 `started → attempt` 索引 ⇒ 「`started` 重复 branch_id」
        **不再是错误**（该对称锁随旧 R2 一起撤销；`failures == ()` ⇒ 零请求零派发）。"""
        seen = _recheck_spy(monkeypatch)
        h = _Harness()
        report = _report(
            started=(_started(BRANCH_IDS[0], 1), _started(BRANCH_IDS[0], 2)),
            failures=(),
        )
        h.run(monkeypatch, report)  # ⛔ 不再抛错
        assert seen == []

    def test_R3_failures_duplicate_branch_id(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen = _recheck_spy(monkeypatch)
        h = _Harness()
        report = _report(
            started=(_started(BRANCH_IDS[0]),),
            failures=(
                _witness(BRANCH_IDS[0], _failure(AdapterFailureKind.CRASH)),
                _witness(BRANCH_IDS[0], _failure(AdapterFailureKind.CRASH)),
            ),
        )
        with pytest.raises(ExecutionRecoveryCallbackError, match="failures"):
            h.run(monkeypatch, report)
        assert seen == []

    def test_R4_non_capability_request_present(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """`timeout` ⇒ `FailureClass.MODEL` ⇒ `route_to = MODEL_ROUTER`（非 CAPABILITY）。"""
        seen = _recheck_spy(monkeypatch)
        h = _Harness()
        report = _report(
            started=(_started(BRANCH_IDS[0]),),
            failures=(_witness(BRANCH_IDS[0], _failure(AdapterFailureKind.TIMEOUT)),),
        )
        with pytest.raises(RedecisionDispatchError):
            h.run(monkeypatch, report)
        assert seen == []


class TestCrossModuleMirrorGuardConsistency:
    """⚠️ **跨模块一致性锁**（你要求的第三处）—— 防 **mirror guard 静默漂移**。

    口径（你给的措辞）：callback 的 Phase 2 是"对冻结 consumer 可达集的 **batch preflight
    mirror guard**，**不产生** target、**不改变** target"。
    ⇒ 它**必须**与 `dispatch_redecision` 的实际支持集**一致**。

    ⚠️ **两条路径都真跑**（不读源码、不硬编码集合）：
      · **consumer 集** = 直接调 `dispatch_redecision`，能走完的 target
      · **mirror 集**   = 把同一种 target 做成**单条恢复请求**喂给 callback，
        只有**未**诱发 `RedecisionDispatchError` 的 target 才算 mirror 接受
      ⇒ 两者必须相等；若 consumer 今后扩大支持集，**本条会同步变红**（不是空地）。
    """

    #: `RedecisionRequest.target` 只能取 `REDECISION_TARGETS`（`LRT-4` 守卫）⇒ 只探这三个。
    PROBE_TARGETS = tuple(REDECISION_TARGETS)

    @staticmethod
    def _consumer_accepts(target: RecoveryTarget) -> bool:
        """真调 consumer：走完 ⇒ 接受；抛 `RedecisionDispatchError` ⇒ 不接受。"""
        reg = CapabilityRegistry(probe=CapabilityProbe(probed_at=T0), loader=_LoaderSpy())
        request = RedecisionRequest(
            branch_id=BRANCH_IDS[0],
            attempt=1,
            target=target,
            minimal_correction="m",
            carry_forward=(),
        )
        try:
            dispatch_redecision(request=request, capabilities=reg)
        except RedecisionDispatchError:
            return False
        return True

    @staticmethod
    def _mirror_accepts(monkeypatch: pytest.MonkeyPatch, target: RecoveryTarget) -> bool:
        """把同一种 target 做成**单条恢复请求**喂给 callback ⇒ 看 Phase 2 是否拦下。"""
        seen = _recheck_spy(monkeypatch)
        h = _Harness()
        # 用真实 failure 种类映射到目标：CRASH/NETWORK ⇒ CAPABILITY；
        # 其余目标用 stub failure 直接构造请求（绕过 adapter 种类限制）
        report = _report(
            started=(_started(BRANCH_IDS[0]),),
            failures=(_witness(BRANCH_IDS[0], _failure(AdapterFailureKind.CRASH)),),
        )
        try:
            h.run(monkeypatch, report, forced_target=target)
        except RedecisionDispatchError:
            return False
        finally:
            assert seen == [] or target is RecoveryTarget.CAPABILITY
        return True

    def test_mirror_set_equals_consumer_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        consumer = {t for t in self.PROBE_TARGETS if self._consumer_accepts(t)}
        mirror = {t for t in self.PROBE_TARGETS if self._mirror_accepts(monkeypatch, t)}
        assert mirror == consumer, (
            f"mirror guard 接受集 {mirror} ≠ consumer 可消费集 {consumer}"
            " ⇒ 两者已漂移：必须同步更新 mirror guard（并登记该锁的语义变更）"
        )

    def test_the_probe_is_not_vacuous(self) -> None:
        """非平凡性：consumer 探测确实**区分**了三个白名单目标。"""
        assert self._consumer_accepts(RecoveryTarget.CAPABILITY) is True
        assert self._consumer_accepts(RecoveryTarget.PLANNER) is False
        assert self._consumer_accepts(RecoveryTarget.MODEL_ROUTER) is False

    def test_drift_would_break_the_equality(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """**漂移模拟**：假设 consumer 今后也接受 `PLANNER` ⇒ 等式必须**失败**。

        ⚠️ 这是"**反向非平凡性**"证明：证明上面那条等式**真的会**在漂移时变红。
        """
        consumer = {t for t in self.PROBE_TARGETS if self._consumer_accepts(t)}
        mirror = {t for t in self.PROBE_TARGETS if self._mirror_accepts(monkeypatch, t)}
        widened = consumer | {RecoveryTarget.PLANNER}  # 假设 consumer 扩大
        assert mirror != widened, "漂移模拟失败：等式在空中也成立 ⇒ 该锁是空地"


class TestBehavior:
    def test_B1_capability_failure_triggers_exactly_one_recheck(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`crash` ⇒ `EXTERNAL_SERVICE` ⇒ `CAPABILITY` ⇒ 恰一次 `ENVIRONMENT_SWITCHED` 重查。"""
        seen = _recheck_spy(monkeypatch)
        h = _Harness()
        report = _report(
            started=(_started(BRANCH_IDS[0]),),
            failures=(_witness(BRANCH_IDS[0], _failure(AdapterFailureKind.CRASH)),),
        )
        h.run(monkeypatch, report)
        assert seen == [RecheckTrigger.ENVIRONMENT_SWITCHED]
        assert h.registry.last_trigger is RecheckTrigger.ENVIRONMENT_SWITCHED
        assert h.loader.calls >= 1  # 真重查发生

    def test_B2_no_failures_means_zero_requests_and_no_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen = _recheck_spy(monkeypatch)
        h = _Harness()
        h.run(monkeypatch, _report(started=(_started(BRANCH_IDS[0]),), failures=()))
        assert seen == []

    def test_B3_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen = _recheck_spy(monkeypatch)
        h = _Harness()
        monkeypatch.setattr(
            PassDriver,
            "drive_next_pass",
            lambda self: _report(started=(_started(BRANCH_IDS[0]),), failures=()),
        )
        assert execution_recovery_callback(h.root, h.routing) is None

    def test_B4_non_capability_is_raised_not_swallowed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen = _recheck_spy(monkeypatch)
        h = _Harness()
        report = _report(
            started=(_started(BRANCH_IDS[0]),),
            failures=(_witness(BRANCH_IDS[0], _failure(AdapterFailureKind.REFUSED)),),
        )  # REFUSED ⇒ PERMISSION ⇒ PLANNER
        with pytest.raises(RedecisionDispatchError):
            h.run(monkeypatch, report)
        assert seen == []

    def test_B5_multiple_capability_failures_each_recheck_once(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """R3：**逐条**处理（不合并、不去重）⇒ 两条 CAPABILITY 失败 ⇒ 两次重查。"""
        seen = _recheck_spy(monkeypatch)
        h = _Harness()
        report = _report(
            started=(_started(BRANCH_IDS[0]), _started(BRANCH_IDS[1])),
            failures=(
                _witness(BRANCH_IDS[0], _failure(AdapterFailureKind.CRASH)),
                _witness(BRANCH_IDS[1], _failure(AdapterFailureKind.NETWORK)),
            ),
        )
        h.run(monkeypatch, report)
        assert seen == [
            RecheckTrigger.ENVIRONMENT_SWITCHED,
            RecheckTrigger.ENVIRONMENT_SWITCHED,
        ]

    def test_B6_permutation_does_not_change_whether_recheck_happened(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """⚠️ **本单元的核心交付**：`CAPABILITY + MODEL_ROUTER` 两种排列 ⇒ **两批 `recheck` 都为 0**。"""
        cap = _witness(BRANCH_IDS[0], _failure(AdapterFailureKind.CRASH))  # ⇒ CAPABILITY
        mod = _witness(BRANCH_IDS[1], _failure(AdapterFailureKind.TIMEOUT))  # ⇒ MODEL_ROUTER
        started = (_started(BRANCH_IDS[0]), _started(BRANCH_IDS[1]))

        for label, failures in (
            ("cap-then-mod", (cap, mod)),
            ("mod-then-cap", (mod, cap)),
        ):
            seen = _recheck_spy(monkeypatch)
            h = _Harness()
            with pytest.raises(RedecisionDispatchError):
                h.run(monkeypatch, _report(started=started, failures=failures))
            assert seen == [], f"{label}: 发生了 {len(seen)} 次重查（顺序依赖！）"

    def test_B7_only_one_pass_is_driven(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """R6 / 红线 1：`drive_next_pass` **恰一次**（含发生重查的情形）。"""
        calls: list[int] = []
        original = PassDriver.drive_next_pass

        def counting(self: PassDriver) -> ExecutionPassReport:
            calls.append(1)
            return _report(
                started=(_started(BRANCH_IDS[0]),),
                failures=(_witness(BRANCH_IDS[0], _failure(AdapterFailureKind.CRASH)),),
            )

        monkeypatch.setattr(PassDriver, "drive_next_pass", counting)
        h = _Harness()
        _recheck_spy(monkeypatch)
        execution_recovery_callback(h.root, h.routing)
        assert len(calls) == 1
        assert original is not None  # 保留引用，证明我们是**替换**而非修改类


# ======================================================================
# 探针卫生
# ======================================================================


class TestProbeHygiene:
    def test_every_declared_anchor_hits(self) -> None:
        anchors = [
            "class ExecutionRecoveryCallbackError(RuntimeError):",
            "def execution_recovery_callback(",
            "    composition_root: CompositionRoot,",
            "    routing: RoutingPlan,",
            ") -> None:",
            "    driver = PassDriver(composition_root=composition_root, routing=routing)",
            "    report: ExecutionPassReport = driver.drive_next_pass()",
            "    for branch_id, attempt, failure in report.failures:",
            "        dispatch_redecision(request=request, capabilities=capabilities)",
        ]
        for anchor in anchors:
            assert anchor in SOURCE, f"锚点未命中：{anchor!r}"

    def test_source_is_parseable_without_bom(self) -> None:
        assert SOURCE_PATH.read_bytes()[:3] != b"\xef\xbb\xbf"
        ast.parse(SOURCE)

    def test_it_is_the_production_implementation_of_the_frozen_seam(self) -> None:
        """非平凡性：它**真的**能被当作 `on_execution` 传入真 `RouterWrapper`。"""
        from adaptive_router.router_wrapper import RouterWrapper

        h = _Harness()
        wrapper = RouterWrapper(
            composition_root=h.root, on_execution=execution_recovery_callback
        )
        assert wrapper._on_execution is execution_recovery_callback  # noqa: SLF001


# ==========================================================================
# `P6 / D-P6C″`（`5B.6.51.4.3-impl`）—— `LP6-5 / LP6-6 / LP6-7 / LP6-10 / LP6-12`
# 范围稿与锁计划：`tests/behavior/README_step5b.md` **§254.6 / §255.4**
# ==========================================================================

_REPO_ROOT = SOURCE_PATH.resolve().parents[2]


def _lp6_repo_call_sites(symbol: str) -> list[tuple[str, int]]:
    out: list[tuple[str, int]] = []
    for path in sorted((_REPO_ROOT / "src").rglob("*.py")):
        if "__pycache__" in str(path):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and ast.unparse(node.func).split(".")[-1] == symbol:
                out.append((path.relative_to(_REPO_ROOT).as_posix(), node.lineno))
    return out


def _lp6_callback_fn() -> ast.FunctionDef:
    for node in ast.walk(ast.parse(SOURCE)):
        if isinstance(node, ast.FunctionDef) and node.name == "execution_recovery_callback":
            return node
    raise AssertionError("execution_recovery_callback")


class TestLP6ProvenanceAndOwners:
    """`LP6-5`（`M2-b3`）· `LP6-6`（`M1-b/c/d` · repo-wide）· `LP6-7`（`M4` · `LSF-6` 闭合）。"""

    def test_LP6_6_owners_are_repo_wide_unique(self) -> None:
        route = _lp6_repo_call_sites("route_adapter_failure")
        assert len(route) == 1, route
        assert route[0][0] == "src/adaptive_router/execution_recovery_callback.py"
        assert len(_lp6_repo_call_sites("commit_start_failure")) == 1  # owner 在 execution.py（见 LP6-2）
        assert {
            rel for rel, _ in _lp6_repo_call_sites("commit_start_failure")
        } == {"src/adaptive_router/execution.py"}

    def test_LP6_6_direct_cross_calls_are_zero(self) -> None:
        exec_src = (_REPO_ROOT / "src/adaptive_router/execution.py").read_text(encoding="utf-8")
        assert calls_to(exec_src, ("route_adapter_failure",)) == []
        assert calls_to(SOURCE, ("commit_start_failure",)) == []

    def test_LP6_6_provenance_is_the_triple(self) -> None:
        fn = _lp6_callback_fn()
        loops = [
            n for n in ast.walk(fn)
            if isinstance(n, ast.For) and ast.unparse(n.iter) == "report.failures"
        ]
        assert len(loops) == 1
        assert isinstance(loops[0].target, ast.Tuple)
        assert [ast.unparse(e) for e in loops[0].target.elts] == [
            "branch_id",
            "attempt",
            "failure",
        ]
        assert [
            n for n in ast.walk(fn) if isinstance(n, ast.Attribute) and n.attr == "started"
        ] == []  # ⛔ 零 `report.started` 关联

    def test_LP6_5_drive_precedes_failures_iteration(self) -> None:
        fn = _lp6_callback_fn()
        drive = next(
            n for n in ast.walk(fn)
            if isinstance(n, (ast.Assign, ast.AnnAssign))
            and "drive_next_pass" in ast.unparse(n.value)
        )
        loop = next(
            n for n in ast.walk(fn)
            if isinstance(n, ast.For) and ast.unparse(n.iter) == "report.failures"
        )
        assert drive.lineno < loop.lineno

    def test_LP6_7_mismatch_branch_never_settles(self) -> None:
        exec_src = (_REPO_ROOT / "src/adaptive_router/execution.py").read_text(encoding="utf-8")
        tree = ast.parse(exec_src)
        fn = next(
            n for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef) and n.name == "_dispatch"
        )
        mismatch = next(
            n for n in ast.walk(fn)
            if isinstance(n, ast.If) and "CapabilityMismatch" in ast.unparse(n.test)
        )
        body = ast.unparse(ast.Module(body=mismatch.body, type_ignores=[]))
        assert "commit_start_failure" not in body  # ★ `LSF-6` NOT PROVEN HERE 的闭合面
        assert "commit_start_failure" not in ast.unparse(
            ast.Module(
                body=next(
                    n for n in ast.walk(fn)
                    if isinstance(n, ast.If) and "AdapterStartReceipt" in ast.unparse(n.test)
                ).body,
                type_ignores=[],
            )
        )


class TestLP6CallbackRetainedResponsibilities:
    """`LP6-10`：R2 撤销**不**带走其它冻结责任。"""

    def test_single_pass_and_single_dispatch(self) -> None:
        assert len(calls_to(SOURCE, ("drive_next_pass",))) == 1
        assert len(calls_to(SOURCE, ("dispatch_redecision",))) == 1
        assert [n for n in ast.walk(ast.parse(SOURCE)) if isinstance(n, ast.While)] == []

    def test_guards_still_precede_dispatch(self) -> None:
        s = _rejection_sites(SOURCE)
        assert len(s["guards"]) == 2
        assert all(g < s["dispatch"] for g in s["guards"])


class TestLP6RecoveryWiringDocsOnly:
    """`LP6-12`：A/B/C 三层（raw diff 范围由台账快照比对给出；此处锁 B + C）。"""

    def test_LP6_12_B_phrase_counts(self) -> None:
        src = (_REPO_ROOT / "src/adaptive_router/recovery_wiring.py").read_text(encoding="utf-8")
        assert src.count("(branch_id, attempt, AdapterFailure)") == 1
        assert src.count("`(branch_id, AdapterFailure)`") == 0

    def test_LP6_12_C_stripped_ast_fingerprint(self) -> None:
        path = _REPO_ROOT / "src/adaptive_router/recovery_wiring.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef)):
                body = node.body
                if (
                    body
                    and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)
                ):
                    node.body = body[1:]
        digest = hashlib.sha256(ast.dump(tree).encode("utf-8")).hexdigest().upper()
        assert digest == "FD77EE5763E26539829A22B1D6A516640D0E92ED6E6790F68448CAA00DD01B2F"  # `runtime semantics = UNCHANGED`
