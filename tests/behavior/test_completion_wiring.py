"""``completion_wiring`` 的结构锁（`LCW-1…15`）、行为（`B1…B11`）与 probe 卫生。

范围、冻结与授权见 `tests/behavior/README_step5b.md` **§236**（G3 Scope Review）/
**§237**（七项裁决 · 冻结签名与责任链）/ **§238**（范围稿 · `LCW-1…15` / `MCW-1…9`）/
**§239**（实现授权申请 · 七项 + DoD 十条）。

⚠️ **禁止子串扫描**：真实源码 docstring 里**故意提及** `record_outcome` / `materialize` /
`PassDriver` 等词 ⇒ 全部判定一律 **AST 级 / 绑定集级**（检测器由 `_completion_wiring_locks`
转发既有集合；只有四项"取所属函数 / 取 `If.test` 形状 / 数循环 / 取 `_graph` 读取点"
作为**本文件局部** AST helper 实现，⛔ 不并入转发层）。

⚠️ 本文件承载**两条 MUST 锁**：
    `LCW-5` 的跨文件半边（`γ′` friend access）+ `LCW-13`（repo-wide `record_outcome` = 1
    且必须落在 `terminal_wiring.py::_settle`）
—— 因此**不需要**修改已冻结的 `test_terminal_wiring.py`（`close_run` 旧锁 = UNCHANGED）。
"""

from __future__ import annotations

import ast
import pathlib
from unittest import mock

import pytest

from adaptive_router.adapters.in_memory import InMemoryAdapter
from adaptive_router.agent_manager import AgentManagerError
from adaptive_router.completion_wiring import CompletionWiringError, accept_completion
from adaptive_router.composition_root import CompositionRoot
from adaptive_router.contracts import (
    BranchState,
    Confidence,
    ConfirmationSource,
    EffortLevel,
)
from adaptive_router.contracts.common import ContractError
from adaptive_router.execution import ExecutionCompositor
from adaptive_router.pass_driver import PassDriver
from adaptive_router.result_handoff import (
    ActualFacts,
    ProviderResultPayload,
    RequestedFacts,
    RuntimeResultPayload,
)
from adaptive_router.validation_judgment import CriterionVerdict, ValidationJudgment
from adaptive_router.validation_wiring import BranchValidationOutcome
from test_pass_driver import DriverHarness

from _completion_wiring_locks import (
    call_keyword_shapes_by_caller,
    calls_to,
    clock_call_hits,
    function_raise_guards,
    function_return_annotation,
    function_return_shapes,
    function_signature_shape,
    imported_module_tokens,
    module_level_container_assignments,
    module_level_function_names,
    open_calls,
    referenced_names,
)

# ======================================================================
# 常量与局部 AST helper（⛔ 不新增检测器谓词）
# ======================================================================

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
SRC = REPO_ROOT / "src"
MODULE_REL = "src/adaptive_router/completion_wiring.py"
MODULE_PATH = REPO_ROOT / MODULE_REL
SOURCE = MODULE_PATH.read_text(encoding="utf-8")

PUBLIC_SURFACE = ["CompletionWiringError", "accept_completion"]
MODULE_FUNCTIONS = (
    "_require_driver",
    "_require_ended_at",
    "_require_judgment_source",
    "accept_completion",
)
SIGNATURE = {
    "positional": [],
    "kwonly": ["driver", "branch_id", "attempt", "payload", "ended_at", "judgment_source"],
    "defaults": [False, False, False, False, False, False],
    "vararg": None,
    "kwarg": None,
}
RAISE_GUARDS = {
    "_require_driver": (("not isinstance(driver, PassDriver)", "CompletionWiringError"),),
    "_require_ended_at": (
        ("type(ended_at) is not str or not ended_at.strip()", "CompletionWiringError"),
    ),
    "_require_judgment_source": (("not callable(source)", "CompletionWiringError"),),
    "accept_completion": (),
}
FORBIDDEN_CALLS = (
    "route_failed_branch",
    "route_adapter_failure",
    "dispatch_redecision",
    "drive_once",
    "drive_next_pass",
    "recheck",
    "close_run",
    "converge_run",
    "run_pass",
    "record_outcome",
    "materialize",
    "apply_runtime_control",
    "commit_started",
    "settle_denied_run",
)
#: `LCW-12` 的**冻结导入白名单**（实测 `imported_module_tokens` 的精确集合）。
EXPECTED_IMPORT_TOKENS = frozenset(
    {
        "annotations",
        "__future__",
        "typing",
        "TYPE_CHECKING",
        "Callable",
        "pass_driver",
        "terminal_wiring",
        "validation",
        "validation_wiring",
        "execution_results",
        "contracts",
        "result_handoff",
        "validation_judgment",
    }
)
#: `LCW-10` 期望的**禁区名**在 `src/` 内**不得**被本模块引用。
#: `LCW-11` 期望的模块级容器 = ∅。


def _callee(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _functions(tree: ast.Module) -> list[ast.FunctionDef]:
    return [n for n in tree.body if isinstance(n, ast.FunctionDef)]


def _calls_with_owner(source: str, names: tuple[str, ...]) -> list[tuple[int, str]]:
    """`(lineno, 所属函数名)` —— **全作用域** Call visitor（⛔ 不是子串计数）。

    ⚠️ **`5B.6.50.3.2-impl.1` 修正（`LCW-13` detector coverage）**：本 helper 初版只遍历
    ``[n for n in tree.body if isinstance(n, ast.FunctionDef)]`` ⇒ **漏掉** class 方法 ·
    `AsyncFunctionDef` · 嵌套函数 · **模块级直接调用** · class body 中的直接调用
    ⇒ 使 `LCW-13` 的"**repo-wide**"声明出现**假绿**（一个 class 方法里的第二个
    `record_outcome` 调用点**不会**被看见）。现改为**全作用域 visitor**：
    进入 `FunctionDef` / `AsyncFunctionDef` 时压栈、离开时弹栈；
    模块级 / class body 的调用归属 `<module>`。
    ⛔ **未改** `LCW-13` 的语义（"repo-wide 恰 1 且就是 `_settle`"不变）。
    """
    tree = ast.parse(source)
    out: list[tuple[int, str]] = []

    class Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.owners: list[str] = []

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self.owners.append(node.name)
            self.generic_visit(node)
            self.owners.pop()

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            self.owners.append(node.name)
            self.generic_visit(node)
            self.owners.pop()

        def visit_Call(self, node: ast.Call) -> None:
            if _callee(node) in names:
                owner = self.owners[-1] if self.owners else "<module>"
                out.append((node.lineno, owner))
            self.generic_visit(node)

    Visitor().visit(tree)
    return sorted(out)


def _call_lines(source: str, name: str) -> list[int]:
    return sorted(line for line, _owner in _calls_with_owner(source, (name,)))


def _raise_guard_tests(source: str) -> list[str]:
    """所有 `body` 内含 `raise` 的 `If.test` 的反解析文本（本层守卫的**全部**判据）。"""
    tree = ast.parse(source)
    out: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.If) and any(isinstance(s, ast.Raise) for s in node.body):
            out.append(ast.unparse(node.test))
    return sorted(out)


def _type_check_operands(source: str) -> set[str]:
    """`isinstance(...)` / `type(...)` 的**首个实参名**集合。"""
    tree = ast.parse(source)
    out: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in ("isinstance", "type")
            and node.args
            and isinstance(node.args[0], ast.Name)
        ):
            out.add(node.args[0].id)
    return out


def _graph_read_lines(source: str) -> list[int]:
    """``Attribute(attr="_graph")`` 的**读取**（`Load`）点行号。"""
    tree = ast.parse(source)
    return sorted(
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and node.attr == "_graph"
        and isinstance(node.ctx, ast.Load)
    )


def _loop_counts(source: str) -> tuple[int, int]:
    tree = ast.parse(source)
    return (
        sum(1 for n in ast.walk(tree) if isinstance(n, ast.While)),
        sum(1 for n in ast.walk(tree) if isinstance(n, ast.For)),
    )


def _if_nodes_in(source: str, func_name: str) -> list[ast.If]:
    """**某模块级函数体内**的全部 `If` 节点（⛔ 不含模块级 `if TYPE_CHECKING:`）。"""
    func = next(f for f in _functions(ast.parse(source)) if f.name == func_name)
    return [n for n in ast.walk(func) if isinstance(n, ast.If)]


def _repo_calls(name: str) -> list[tuple[str, int, str]]:
    """全 `src/` 内某调用名的 `(相对路径, lineno, 所属函数)` 列表。"""
    out: list[tuple[str, int, str]] = []
    for path in sorted(SRC.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        for line, owner in _calls_with_owner(source, (name,)):
            out.append((path.relative_to(REPO_ROOT).as_posix(), line, owner))
    return out


def _repo_graph_readers() -> dict[str, list[int]]:
    """全 `src/` 内读 `_graph` 的文件 → 行号（⛔ 排除 `pass_driver.py` 自身）。"""
    out: dict[str, list[int]] = {}
    for path in sorted(SRC.rglob("*.py")):
        if path.name == "pass_driver.py":
            continue
        lines = _graph_read_lines(path.read_text(encoding="utf-8"))
        if lines:
            out[path.relative_to(REPO_ROOT).as_posix()] = lines
    return out


# ======================================================================
# 行为 harness（真 `CompositionRoot` + `InMemoryAdapter` + `PassDriver`）
# ======================================================================

_TS = "2026-09-24T00:00:00Z"


def _payload() -> RuntimeResultPayload:
    return RuntimeResultPayload(
        requested=RequestedFacts(
            model_id="provider-model",
            effort=EffortLevel.MEDIUM,
            source=ConfirmationSource.REQUEST,
        ),
        actual=ActualFacts(
            model_id="provider-model",
            effort=EffortLevel.MEDIUM,
            source=ConfirmationSource.PROVIDER_API,
            confidence=Confidence.HIGH,
            observed=True,
        ),
        payload=ProviderResultPayload(
            provider_status="ok",
            conclusion_summary="done",
            key_evidence=("evidence-1",),
            residual_risks=(),
            main_agent_todo=(),
        ),
    )


def _judgment(*, satisfied: bool, calls: list[tuple[str, str]] | None = None):
    """判定来源工厂：`verdict.criterion` **回填**权威完成标准（⇒ 必然通过锚定校验）。"""

    def source(branch_return: object, criteria: str) -> ValidationJudgment:
        if calls is not None:
            calls.append((str(branch_return), criteria))
        return ValidationJudgment(
            verdicts=(
                CriterionVerdict(
                    criterion=criteria,
                    satisfied=satisfied,
                    evidence=("e",),
                ),
            ),
            detail="test-judgment",
        )

    return source


def _harness() -> tuple[PassDriver, CompositionRoot, object, tuple[str, int, str]]:
    h = DriverHarness()
    root = CompositionRoot(
        facts_source=lambda task, context: None,
        capabilities=h.root.capabilities,
        adapter=InMemoryAdapter(timestamp=_TS),
        execution_context_facts_by_branch=h.root.execution_context_facts_by_branch,
        confirmed_conclusions_by_branch=h.root.confirmed_conclusions_by_branch,
        control_facts_source=h.root.control_facts_source,
        admission_facts_source=h.root.admission_facts_source,
        reusable_result_source=h.root.reusable_result_source,
        continuation_source=h.root.continuation_source,
        run_id_factory=lambda: h.routing.run_id,
    )
    driver = PassDriver(composition_root=root, routing=h.routing)
    report = driver.drive_next_pass()
    return driver, root, h.routing, report.started[0]


def _state_of(driver: PassDriver, branch_id: str) -> BranchState:
    record = driver._graph.agent_manager.routing_run.branch_by_id(branch_id)  # noqa: SLF001
    return record.state


# ======================================================================
# `LCW-1 … LCW-2` —— 模块形状与签名
# ======================================================================


class TestLCW1ModuleShape:
    def test_public_surface_is_exactly_two(self) -> None:
        """`LCW-1`：`__all__` 恰 `["CompletionWiringError", "accept_completion"]`。"""
        tree = ast.parse(SOURCE)
        all_node = next(
            n
            for n in tree.body
            if isinstance(n, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "__all__" for t in n.targets)
        )
        assert list(ast.literal_eval(all_node.value)) == PUBLIC_SURFACE

    def test_module_level_functions_are_exactly_four(self) -> None:
        """`LCW-1`（**复审更正**）：模块级函数名集合**恰 4**，⛔ 不是 5。

        ⚠️ `CompletionWiringError` 是 **class**，⛔ 不计入函数名集合；本模块**没有** `__post_init__`。
        """
        assert module_level_function_names(SOURCE) == MODULE_FUNCTIONS
        assert len(MODULE_FUNCTIONS) == 4

    def test_module_level_objects_are_exactly_five(self) -> None:
        """`LCW-1`：模块级对象 = 1 异常类 + 4 函数（`__all__` 属赋值，另计）。"""
        classes = [n.name for n in ast.parse(SOURCE).body if isinstance(n, ast.ClassDef)]
        assert classes == ["CompletionWiringError"]


class TestLCW2Signature:
    def test_signature_shape(self) -> None:
        """`LCW-2`：恰 6 个 kw-only、全无默认值、无 `*args` / `**kwargs`。"""
        assert function_signature_shape(SOURCE, "accept_completion") == SIGNATURE

    def test_return_annotation_is_the_paired_witness(self) -> None:
        """`LCW-2`：返回注解 = `BranchValidationOutcome`。"""
        assert function_return_annotation(SOURCE, "accept_completion") == "BranchValidationOutcome"

    def test_signature_carries_no_validator_parameter(self) -> None:
        """`LCW-14` 的签名半边：⛔ 签名里**不得**出现 `validator`（构造权在 Core）。"""
        shape = function_signature_shape(SOURCE, "accept_completion")
        assert "validator" not in shape["kwonly"]
        assert "validator" not in shape["positional"]


# ======================================================================
# `LCW-3 … LCW-4` —— 本层守卫恰三类 + 顺序
# ======================================================================


class TestLCW3LocalGuards:
    def test_raise_guards_are_exactly_the_three_declared_ones(self) -> None:
        """`LCW-3`：本层守卫恰三类，且入口自体**零** `raise`。"""
        actual = {fn: function_raise_guards(SOURCE, fn) for fn in MODULE_FUNCTIONS}
        assert actual == RAISE_GUARDS

    def test_entry_point_has_no_local_raise(self) -> None:
        """`LCW-3`：`accept_completion` 体内 `raise` = 0（守卫全部委托给三个 helper）。"""
        assert function_raise_guards(SOURCE, "accept_completion") == ()

    def test_no_duplicate_downstream_guards(self) -> None:
        """`LCW-3`（**复审更正口径**）：判据是"**没有第二套 guard**"，⛔ 不是"名字没出现"。

        `branch_id` / `attempt` / `payload` **MAY** 出现在签名与被委托实参里（它们必然出现），
        但 **MUST NOT** 出现在：**local guard predicates** / `isinstance`·`type` 检查实参 /
        raise-guard conditions / local validation helper calls。
        """
        # ① 三名字**必然出现**（签名 + 被委托实参）——先证明"存在"，避免把锁写成"名字不存在"
        refs = referenced_names(SOURCE)
        for name in ("branch_id", "attempt", "payload"):
            assert name in refs, f"{name} 必须出现（签名 / 被委托实参）"

        # ② 却**不得**出现在任何 raise-guard 判据里
        for test_expr in _raise_guard_tests(SOURCE):
            for name in ("branch_id", "attempt", "payload"):
                assert name not in test_expr, f"本层守卫不得判 {name}：{test_expr}"

        # ③ 也**不得**出现在 `isinstance` / `type` 检查的实参里
        assert _type_check_operands(SOURCE) == {"driver", "ended_at"}

    def test_no_local_validation_helper_calls(self) -> None:
        """`LCW-3`：⛔ 不调用任何 `validate_*` 形状的本地 helper（规则只有一份）。"""
        tree = ast.parse(SOURCE)
        local_calls = {
            _callee(n)
            for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
        }
        assert not {c for c in local_calls if c is not None and c.startswith("_require_valid")}


class TestLCW4GuardOrder:
    def test_guards_precede_every_delegated_call(self) -> None:
        """`LCW-4` ★：三条守卫的行号 **<** `Validator(` 与 `validate_branch(` 的行号。"""
        guards = _call_lines(SOURCE, "_require_ended_at") + _call_lines(
            SOURCE, "_require_judgment_source"
        )
        delegated = _call_lines(SOURCE, "Validator") + _call_lines(SOURCE, "validate_branch")
        assert guards and delegated
        assert max(guards) < min(delegated)

    def test_all_guards_precede_the_graph_read(self) -> None:
        """`LCW-4`：三条守卫**全部**先于 `γ′` 的 friend read（`ended_at` → `_graph`）。"""
        guards = [
            _call_lines(SOURCE, name)[0]
            for name in ("_require_driver", "_require_ended_at", "_require_judgment_source")
        ]
        assert max(guards) < _graph_read_lines(SOURCE)[0]


# ======================================================================
# `LCW-5 … LCW-6` —— `γ′` friend access + 零自造
# ======================================================================


class TestLCW5GammaPrime:
    def test_module_reads_graph_exactly_once(self) -> None:
        """`LCW-5` ★：本模块 `driver._graph` 读取**恰 1 次**。"""
        reads = _graph_read_lines(SOURCE)
        assert len(reads) == 1, f"本模块 driver._graph 读取必须恰 1 次，实测 {reads}"

    def test_repo_wide_graph_readers_are_exactly_this_module(self) -> None:
        """`LCW-5` ★（**跨文件 MUST 锁**）：全 `src/`（除 `pass_driver.py`）读 `_graph` 的
        文件集合 **== { completion_wiring.py }**。"""
        readers = _repo_graph_readers()
        assert set(readers) == {MODULE_REL}, f"唯一 friend reader 必须是本模块，实测 {sorted(readers)}"
        assert len(readers[MODULE_REL]) == 1

    def test_graph_read_is_the_only_attribute_target(self) -> None:
        """`LCW-5`：本模块对 `driver` 的唯一属性访问就是 `_graph`。"""
        tree = ast.parse(SOURCE)
        driver_attrs = {
            n.attr
            for n in ast.walk(tree)
            if isinstance(n, ast.Attribute)
            and isinstance(n.value, ast.Name)
            and n.value.id == "checked_driver"
        }
        assert driver_attrs == {"_graph"}


class TestLCW6NoSelfConstruction:
    def test_no_rematerialization_or_construction(self) -> None:
        """`LCW-6` ★：`materialize` / `CompositionRoot` / `PassDriver` **调用点全 = 0**。"""
        assert calls_to(SOURCE, ("materialize",)) == []
        assert calls_to(SOURCE, ("CompositionRoot",)) == []
        assert calls_to(SOURCE, ("PassDriver",)) == []

    def test_pass_driver_appears_only_as_annotation_and_type_check(self) -> None:
        """`LCW-6`：`PassDriver` 只作**注解**与 `isinstance` 出现，⛔ 无构造调用。"""
        tree = ast.parse(SOURCE)
        assert any(
            isinstance(n, ast.Call)
            and isinstance(n.func, ast.Name)
            and n.func.id == "isinstance"
            and any(isinstance(a, ast.Name) and a.id == "PassDriver" for a in n.args)
            for n in ast.walk(tree)
        )
        assert calls_to(SOURCE, ("PassDriver",)) == []


# ======================================================================
# `LCW-7 … LCW-9` —— 责任链 exactly-once · 二分形态 · 返回同一见证
# ======================================================================


class TestLCW7ExactlyOnce:
    def test_chain_call_counts(self) -> None:
        """`LCW-7`：六个环节各**恰一次**（两守卫入口各 1 · 同一 `If` 的两支）。"""
        assert len(calls_to(SOURCE, ("assemble_result_handoff",))) == 1
        assert len(calls_to(SOURCE, ("build_branch_return",))) == 1
        assert len(calls_to(SOURCE, ("validate_branch",))) == 1
        assert len(calls_to(SOURCE, ("Validator",))) == 1
        assert len(calls_to(SOURCE, ("settle_pass",))) == 1
        assert len(calls_to(SOURCE, ("settle_failure",))) == 1

    def test_settle_entries_are_disjoint_but_share_arguments(self) -> None:
        """`LCW-7`：两入口**共用同一组实参形态**（`outcome` / `manager` / `ended_at`）。"""
        expect = {"outcome": ("paired",), "manager": ("graph.agent_manager",),
                  "ended_at": ("checked_ended_at",)}
        assert call_keyword_shapes_by_caller(SOURCE, "settle_pass") == {"accept_completion": expect}
        assert call_keyword_shapes_by_caller(SOURCE, "settle_failure") == {"accept_completion": expect}


class TestLCW8BinaryDispatch:
    def test_entry_has_exactly_one_non_guard_if(self) -> None:
        """`LCW-8` ★：入口体内**恰一个**非守卫 `If`（= 二分派发）；⛔ 无第三分支。"""
        guards = [
            n
            for n in _if_nodes_in(SOURCE, "accept_completion")
            if any(isinstance(s, ast.Raise) for s in n.body)
        ]
        dispatch = [
            n
            for n in _if_nodes_in(SOURCE, "accept_completion")
            if not any(isinstance(s, ast.Raise) for s in n.body)
        ]
        assert guards == []  # 入口自身零 raise（守卫全在三个 helper 内）
        assert len(dispatch) == 1

    def test_dispatch_test_is_identity_on_the_authoritative_bool(self) -> None:
        """`LCW-8` ★：`If.test` == `paired.validation.passed is True`（`Is` 节点，⛔ 非真值判断）。"""
        dispatch = _if_nodes_in(SOURCE, "accept_completion")[0]
        assert ast.unparse(dispatch.test) == "paired.validation.passed is True"
        ops = [
            type(op).__name__
            for c in ast.walk(dispatch.test)
            if isinstance(c, ast.Compare)
            for op in c.ops
        ]
        assert ops == ["Is"]

    def test_dispatch_uses_is_not_equality_or_self_report(self) -> None:
        """`LCW-8`：⛔ 无 `==`；⛔ 不纳入 `provider_status` / `validation_state`（分支自报，A15.4）。"""
        tree = ast.parse(SOURCE)
        ops = [
            type(op).__name__
            for c in ast.walk(tree)
            if isinstance(c, ast.Compare)
            for op in c.ops
        ]
        # 全模块只允许恒等比较：`Is`（派发）与 `IsNot`（`_require_ended_at` 的既有守卫）
        assert set(ops) <= {"Is", "IsNot"}, f"出现非恒等比较：{ops}"
        assert "Eq" not in ops and "NotEq" not in ops
        refs = referenced_names(SOURCE)
        for name in ("provider_status", "validation_state"):
            assert name not in refs

    def test_else_branch_calls_settle_failure(self) -> None:
        """`LCW-8`：`else` 支**恰**调用 `settle_failure`（无第三分支 ⇒ 不存在落空静默路径）。"""
        dispatch = _if_nodes_in(SOURCE, "accept_completion")[0]
        assert dispatch.orelse, "必须有 else 支（⛔ 不得落空）"
        assert [
            _callee(n)
            for stmt in dispatch.orelse
            for n in ast.walk(stmt)
            if isinstance(n, ast.Call)
        ] == ["settle_failure"]


class TestLCW9SameWitness:
    def test_return_is_the_local_paired_name(self) -> None:
        """`LCW-9` ★：返回值 = **局部名** `paired`（⛔ 不是新构造）。"""
        assert function_return_shapes(SOURCE, "accept_completion") == ("paired",)

    def test_paired_is_produced_by_the_single_validate_branch_call(self) -> None:
        """`LCW-9`：`paired` 的**唯一**赋值来源就是 `validate_branch(...)`。"""
        tree = ast.parse(SOURCE)
        assigns = [
            ast.unparse(n.value)
            for n in ast.walk(tree)
            if isinstance(n, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "paired" for t in n.targets)
        ]
        assert len(assigns) == 1
        assert assigns[0].startswith("validate_branch(")


# ======================================================================
# `LCW-10 … LCW-12` —— 禁区 · 零循环/时钟/IO/状态 · 导入白名单
# ======================================================================


class TestLCW10ForbiddenCalls:
    def test_no_forbidden_call(self) -> None:
        """`LCW-10`：14 个禁区调用名 **全 = ∅**。"""
        for name in FORBIDDEN_CALLS:
            assert calls_to(SOURCE, (name,)) == [], f"禁区调用出现：{name}"

    def test_does_not_name_the_runtime_state_writer(self) -> None:
        """`LCW-10`：本模块⛔ **不命名** `AgentManager`（⇒ 结构上不可能成为第二个写者）。"""
        assert "AgentManager" not in referenced_names(SOURCE)


class TestLCW11NoLoopClockIoState:
    def test_zero_loops(self) -> None:
        """`LCW-11`：`While` = 0 · `For` = 0。"""
        assert _loop_counts(SOURCE) == (0, 0)

    def test_zero_clock_and_io_and_state(self) -> None:
        """`LCW-11`：零时钟 · 零 `open(` · 零模块级可变容器。"""
        assert clock_call_hits(SOURCE) == []
        assert open_calls(SOURCE) == []
        assert module_level_container_assignments(SOURCE) == ()


class TestLCW12ImportBoundary:
    def test_import_whitelist_is_exact(self) -> None:
        """`LCW-12`：导入 token 集合**恰等于**冻结白名单。"""
        assert imported_module_tokens(SOURCE) == EXPECTED_IMPORT_TOKENS

    def test_does_not_import_adapters_or_agent_manager(self) -> None:
        """`LCW-12`：⛔ 不 import `adapters` · ⛔ 不 import `agent_manager`。"""
        tokens = imported_module_tokens(SOURCE)
        assert "adapters" not in tokens
        assert "agent_manager" not in tokens


# ======================================================================
# `LCW-13 … LCW-15` —— 两条 MUST 锁 + 零冻结面
# ======================================================================


class TestLCW13RepoWideRecordOutcome:
    def test_record_outcome_is_repo_wide_unique_and_lives_in_settle(self) -> None:
        """`LCW-13` ★（**跨文件 MUST 锁** · 照 `close_run` 先例并**更严**）：

        repo-wide 生产调用点 = **exactly 1**，且 file = `terminal_wiring.py`、
        enclosing function = `_settle`（⛔ 不只是"恰 1"）。
        """
        hits = _repo_calls("record_outcome")
        assert len(hits) == 1, f"record_outcome 必须全仓唯一，实测 {hits}"
        rel, _line, owner = hits[0]
        assert rel == "src/adaptive_router/terminal_wiring.py"
        assert owner == "_settle"


class TestLCW14ValidatorProvenance:
    def test_validator_is_constructed_once_in_the_entry(self) -> None:
        """`LCW-14` ★（**local provenance** · ⛔ 不是 repo-wide singleton）：

        本模块内 `Validator(...)` 构造**恰 1**，且 enclosing = `accept_completion`。
        """
        hits = _calls_with_owner(SOURCE, ("Validator",))
        assert len(hits) == 1, f"Validator 构造必须恰 1 次，实测 {hits}"
        assert hits[0][1] == "accept_completion"

    def test_validator_takes_the_checked_source(self) -> None:
        """`LCW-14`：构造时 `judgment_source=` 的实参**恰是** `checked_source`。"""
        assert call_keyword_shapes_by_caller(SOURCE, "Validator") == {
            "accept_completion": {"judgment_source": ("checked_source",)}
        }

    def test_module_level_does_not_hold_a_validator(self) -> None:
        """`LCW-14`：模块级**不持有** `Validator`（⛔ 无模块级赋值 / 注解）。"""
        tree = ast.parse(SOURCE)
        module_level_names: set[str] = set()
        for node in tree.body:
            if isinstance(node, ast.Assign):
                module_level_names |= {
                    t.id for t in node.targets if isinstance(t, ast.Name)
                }
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                module_level_names.add(node.target.id)
        assert module_level_names == {"__all__"}


class TestLCW15ZeroFrozenDrift:
    def test_no_frozen_artifact_is_touched_by_this_module(self) -> None:
        """`LCW-15`：本模块**只读**既有对象 —— 源码内⛔ 无对既有模块的属性赋值。"""
        tree = ast.parse(SOURCE)
        attribute_stores = [
            ast.unparse(n)
            for n in ast.walk(tree)
            if isinstance(n, ast.Assign)
            for t in n.targets
            if isinstance(t, ast.Attribute)
        ]
        assert attribute_stores == []

    def test_module_is_new_and_declared(self) -> None:
        """`LCW-15`：模块存在，且其身份即本单元新增的第三个 project artifact 之一。"""
        assert MODULE_PATH.is_file()
        assert MODULE_REL == "src/adaptive_router/completion_wiring.py"


# ======================================================================
# `B1 … B11` —— 行为
# ======================================================================


class TestBehaviour:
    def test_B1_pass_path_closes_the_attempt_as_completed(self) -> None:
        driver, _root, _routing, (branch_id, attempt, _h) = _harness()
        result = accept_completion(
            driver=driver,
            branch_id=branch_id,
            attempt=attempt,
            payload=_payload(),
            ended_at=_TS,
            judgment_source=_judgment(satisfied=True),
        )
        assert result.validation.passed is True
        assert _state_of(driver, branch_id) is BranchState.COMPLETED

    def test_B2_fail_path_closes_the_attempt_as_failed(self) -> None:
        driver, _root, _routing, (branch_id, attempt, _h) = _harness()
        result = accept_completion(
            driver=driver,
            branch_id=branch_id,
            attempt=attempt,
            payload=_payload(),
            ended_at=_TS,
            judgment_source=_judgment(satisfied=False),
        )
        assert result.validation.passed is False
        assert result.validation.failed_criteria
        assert _state_of(driver, branch_id) is BranchState.FAILED

    def test_B3_returns_the_exact_object_from_the_single_validate_branch(self) -> None:
        """`B3` ★（**复审更正措辞**）：返回值 **is** 本次唯一 `validate_branch(...)`
        调用产生的 **exact `paired`** 对象（wrapper / spy 捕获，⛔ 不重建 · ⛔ 不 copy）。
        """
        from adaptive_router import completion_wiring

        captured: list[object] = []
        real = completion_wiring.validate_branch

        def wrapper(**kwargs):
            out = real(**kwargs)
            captured.append(out)
            return out

        driver, _root, _routing, (branch_id, attempt, _h) = _harness()
        with mock.patch.object(completion_wiring, "validate_branch", wrapper):
            result = accept_completion(
                driver=driver,
                branch_id=branch_id,
                attempt=attempt,
                payload=_payload(),
                ended_at=_TS,
                judgment_source=_judgment(satisfied=True),
            )
        assert len(captured) == 1, "本次必须恰有一次 validate_branch"
        assert result is captured[0]

    def test_B4a_rejects_non_pass_driver(self) -> None:
        with pytest.raises(CompletionWiringError):
            accept_completion(
                driver=object(),
                branch_id="b",
                attempt=1,
                payload=_payload(),
                ended_at=_TS,
                judgment_source=_judgment(satisfied=True),
            )

    def test_B4b_rejects_invalid_ended_at(self) -> None:
        driver, _root, _routing, (branch_id, attempt, _h) = _harness()
        for bad in ("", "   ", None, 1):
            with pytest.raises(CompletionWiringError):
                accept_completion(
                    driver=driver,
                    branch_id=branch_id,
                    attempt=attempt,
                    payload=_payload(),
                    ended_at=bad,  # type: ignore[arg-type]
                    judgment_source=_judgment(satisfied=True),
                )
        # 三者都不产生任何运行态写入
        assert _state_of(driver, branch_id) is BranchState.RUNNING

    def test_B4c_rejects_non_callable_judgment_source(self) -> None:
        driver, _root, _routing, (branch_id, attempt, _h) = _harness()
        with pytest.raises(CompletionWiringError):
            accept_completion(
                driver=driver,
                branch_id=branch_id,
                attempt=attempt,
                payload=_payload(),
                ended_at=_TS,
                judgment_source=None,  # type: ignore[arg-type]
            )
        assert _state_of(driver, branch_id) is BranchState.RUNNING

    def test_B5_invalid_ended_at_never_triggers_the_external_capability(self) -> None:
        """`B5` ★ **顺序**：`ended_at` 非法时，`judgment_source` 的调用计数 **== 0**。"""
        driver, _root, _routing, (branch_id, attempt, _h) = _harness()
        calls: list[tuple[str, str]] = []
        with pytest.raises(CompletionWiringError):
            accept_completion(
                driver=driver,
                branch_id=branch_id,
                attempt=attempt,
                payload=_payload(),
                ended_at="",
                judgment_source=_judgment(satisfied=True, calls=calls),
            )
        assert calls == []

    def test_B6_writeback_lands_on_the_caller_s_own_graph(self) -> None:
        """`B6` ★ **同一张图**：写入落在**调用方那只** driver 的图上；
        另造一只 driver（= 另一次物化）**看不到**该记录。"""
        driver, root, routing, (branch_id, attempt, _h) = _harness()
        manager_before = id(driver._graph.agent_manager)  # noqa: SLF001
        accept_completion(
            driver=driver,
            branch_id=branch_id,
            attempt=attempt,
            payload=_payload(),
            ended_at=_TS,
            judgment_source=_judgment(satisfied=True),
        )
        assert id(driver._graph.agent_manager) == manager_before  # noqa: SLF001
        assert _state_of(driver, branch_id) is BranchState.COMPLETED

        other = PassDriver(composition_root=root, routing=routing)
        assert other._graph.agent_manager.routing_run.branches == ()  # noqa: SLF001
        assert other._graph.agent_manager is not driver._graph.agent_manager  # noqa: SLF001

    def test_B7_delegated_exception_propagates_unchanged(self) -> None:
        """`B7` **所有 delegated exceptions 原样传播**：⛔ 不被包装成 `CompletionWiringError`。"""

        class _Boom(RuntimeError):
            pass

        def exploding(_branch_return: object, _criteria: str) -> ValidationJudgment:
            raise _Boom("capability failure")

        driver, _root, _routing, (branch_id, attempt, _h) = _harness()
        with pytest.raises(_Boom):
            accept_completion(
                driver=driver,
                branch_id=branch_id,
                attempt=attempt,
                payload=_payload(),
                ended_at=_TS,
                judgment_source=exploding,
            )
        assert _state_of(driver, branch_id) is BranchState.RUNNING

    def test_B8_duplicate_completion_fails_explicitly(self) -> None:
        """`B8` **duplicate** ⇒ `AgentManagerError`（⛔ 无 dedupe · ⛔ 无幂等）。"""
        driver, _root, _routing, (branch_id, attempt, _h) = _harness()
        kwargs = dict(
            driver=driver,
            branch_id=branch_id,
            attempt=attempt,
            payload=_payload(),
            ended_at=_TS,
            judgment_source=_judgment(satisfied=True),
        )
        accept_completion(**kwargs)  # type: ignore[arg-type]
        with pytest.raises(AgentManagerError):
            accept_completion(**kwargs)  # type: ignore[arg-type]

    def test_B9_unknown_or_mismatched_identity_fails_explicitly(self) -> None:
        """`B9`：未知 `branch_id` / 不匹配 `attempt` ⇒ 被委托层**显式失败**。"""
        driver, _root, _routing, (branch_id, attempt, _h) = _harness()
        with pytest.raises(ContractError):
            accept_completion(
                driver=driver,
                branch_id="no-such-branch",
                attempt=1,
                payload=_payload(),
                ended_at=_TS,
                judgment_source=_judgment(satisfied=True),
            )
        with pytest.raises(ContractError):
            accept_completion(
                driver=driver,
                branch_id=branch_id,
                attempt=attempt + 5,
                payload=_payload(),
                ended_at=_TS,
                judgment_source=_judgment(satisfied=True),
            )
        assert _state_of(driver, branch_id) is BranchState.RUNNING

    def test_B10_witness_is_handoff_ready_for_the_fail_side(self) -> None:
        """`B10`（**只断言可交接性** · ⛔ 不实现 `[14]` 接线）：FAIL 侧拿到的就是
        同一次配对的**同一个对象**，类型与身份齐备。"""
        driver, _root, _routing, (branch_id, attempt, _h) = _harness()
        result = accept_completion(
            driver=driver,
            branch_id=branch_id,
            attempt=attempt,
            payload=_payload(),
            ended_at=_TS,
            judgment_source=_judgment(satisfied=False),
        )
        assert isinstance(result, BranchValidationOutcome)
        assert result.branch_return.branch_id == branch_id
        assert result.branch_return.attempt == attempt
        assert result.validation.branch_id == branch_id
        assert result.validation.passed is False

    def test_B11_this_entry_never_drives_a_pass(self) -> None:
        """`B11`：`accept_completion` 全程 **零** pass 驱动（`run_pass` 调用计数 = 0），
        且 `PassDriver._first_pass` **未被改动**。"""
        driver, _root, _routing, (branch_id, attempt, _h) = _harness()
        first_pass_before = driver._first_pass  # noqa: SLF001
        with mock.patch.object(ExecutionCompositor, "run_pass", autospec=True) as spy:
            accept_completion(
                driver=driver,
                branch_id=branch_id,
                attempt=attempt,
                payload=_payload(),
                ended_at=_TS,
                judgment_source=_judgment(satisfied=True),
            )
        assert spy.call_count == 0
        assert driver._first_pass is first_pass_before  # noqa: SLF001


# ======================================================================
# 探针卫生
# ======================================================================


class TestDetectorCoverage:
    """`LCW-13` detector **自证**（`5B.6.50.3.2-impl.1`）：⛔ 不只改 helper 就相信它。

    ⚠️ 由来：`_calls_with_owner` 初版只扫"模块顶层普通 `FunctionDef`" ⇒ 一切其它作用域
    **逃逸** ⇒ `LCW-13` 的 "repo-wide" 是**假绿**。本类用**纯源码 fixture** 机械证明
    coverage 已完整，并**同时证明该 fixture 有判别力**（旧口径会漏）。
    """

    SAMPLE = """manager.record_outcome()

class X:
    manager.record_outcome()

    def method(self):
        manager.record_outcome()

    async def async_method(self):
        manager.record_outcome()

    def outer(self):
        def inner():
            manager.record_outcome()
        return inner
"""

    @staticmethod
    def _scope_limited(source: str, name: str) -> list[tuple[int, str]]:
        """**旧（有缺陷的）口径**：只扫模块顶层普通 `FunctionDef` —— 仅用于证明判别力。"""
        tree = ast.parse(source)
        out: list[tuple[int, str]] = []
        for func in [n for n in tree.body if isinstance(n, ast.FunctionDef)]:
            for node in ast.walk(func):
                if isinstance(node, ast.Call) and _callee(node) == name:
                    out.append((node.lineno, func.name))
        return out

    def test_fixture_detects_every_scope(self) -> None:
        """机械证明：**同一 fixture 的 5 处**全被捕获（module / class body / method /
        async method / nested function），⛔ 不只是第一种、⛔ 不是 0 个。"""
        hits = _calls_with_owner(self.SAMPLE, ("record_outcome",))
        assert len(hits) == 5, f"必须捕获全部 5 处，实测 {hits}"
        owners = sorted(owner for _line, owner in hits)
        assert owners == ["<module>", "<module>", "async_method", "inner", "method"]

    def test_named_scopes_are_individually_present(self) -> None:
        """逐条点名（你指定的三条 + 两条曾被同一缺陷漏掉的）："""
        owners = {owner for _line, owner in _calls_with_owner(self.SAMPLE, ("record_outcome",))}
        assert "<module>" in owners, "module scope 未被捕获"
        assert "method" in owners, "class method 未被捕获"
        assert "async_method" in owners, "async method 未被捕获"
        assert "inner" in owners, "nested function 未被捕获"

    def test_fixture_is_discriminating_against_the_old_scope_limited_form(self) -> None:
        """**判别力证明**：旧口径在这个 fixture 上**必然少捕获** ⇒ 该自证测试真能抓住回归。"""
        old = self._scope_limited(self.SAMPLE, "record_outcome")
        new = _calls_with_owner(self.SAMPLE, ("record_outcome",))
        # ⚠️ 实测（非预期值）：旧口径在本 fixture 上看到 **0** 处 ——
        #    因为 5 处调用**全部**落在 class 内（class body / method / async method / nested），
        #    而旧口径只看**模块顶层**普通 `FunctionDef` ⇒ 一个也看不到。
        assert len(old) == 0, f"旧口径应完全看不到（全部在 class 内），实测 {old}"
        assert len(new) == 5
        assert len(old) < len(new)

    def test_unrelated_names_are_not_counted(self) -> None:
        """负对照：⛔ 不把别的名字算进来。"""
        assert _calls_with_owner(self.SAMPLE, ("other_outcome",)) == []

    def test_real_repo_scan_is_really_repo_wide(self) -> None:
        """真实仓扫描在**完整**作用域覆盖下仍为：恰 1 且 `terminal_wiring.py::_settle`。"""
        hits = _repo_calls("record_outcome")
        assert len(hits) == 1, f"record_outcome 必须全仓唯一，实测 {hits}"
        assert hits[0][0] == "src/adaptive_router/terminal_wiring.py"
        assert hits[0][2] == "_settle"

    def test_forwarded_calls_to_detector_is_also_scope_complete(self) -> None:
        """**旁证**：转发层 `calls_to`（既有检测器）本就是全作用域（`ast.walk`）——
        实测它在同一 fixture 上与本地 helper **计数一致** ⇒ 缺口只在本地 helper，⛔ 不在转发层。"""
        import _context_locks

        fixture_imported = "from x import record_outcome\n" + self.SAMPLE
        assert len(_context_locks.calls_to(fixture_imported, {"record_outcome"})) == 5


class TestProbeHygiene:
    def test_every_declared_anchor_hits_once(self) -> None:
        anchors = [
            '"""``completion_wiring``',
            '__all__ = ["CompletionWiringError", "accept_completion"]',
            "class CompletionWiringError(RuntimeError):",
            "def _require_driver(driver: object) -> PassDriver:",
            "def _require_ended_at(ended_at: object) -> str:",
            "def _require_judgment_source(",
            "def accept_completion(",
            "    graph = checked_driver._graph",
            "    if paired.validation.passed is True:",
            "    return paired",
            "from .execution_results import build_branch_return",
            "from .terminal_wiring import settle_failure, settle_pass",
            "from .validation import Validator",
            "from .validation_wiring import validate_branch",
        ]
        for anchor in anchors:
            assert SOURCE.count(anchor) == 1, f"锚点不唯一或缺失：{anchor!r}"

    def test_declared_line_numbers_are_stable(self) -> None:
        """锚点行号与实测一致（`LCW-4` / `LCW-5` / `LCW-7` / `LCW-8` 的行号判据以此为基）。"""
        lines = SOURCE.split("\n")
        assert lines[105] == "    graph = checked_driver._graph"
        assert lines[118].strip() == "validator=Validator(judgment_source=checked_source),"
        assert lines[120] == "    if paired.validation.passed is True:"
        assert lines[124] == "    return paired"
        assert len([ln for ln in lines if ln == "    return paired"]) == 1

    def test_lock_module_forwards_without_new_predicates(self) -> None:
        lock_source = (
            pathlib.Path(__file__).with_name("_completion_wiring_locks.py")
        ).read_text(encoding="utf-8")
        assert "def " not in lock_source, "转发层⛔ 不得新增判定函数"
