"""``[10]`` **start-side failure commit**（``commit_start_failure``）—— ``LSF-1…13`` 结构锁 +
行为锁 + probe（`5B.6.51.3-impl`）。

架构依据：`docs/architecture.md` 2 节 ``[10] Agent Manager``（**Amendment A13**，FROZEN）+
`5B.6.51.1`（语义范围稿）/ `5B.6.51.2`（delta 范围稿）。
范围、冻结骨架、`LSF` / `MSF` 计划见 `tests/behavior/README_step5b.md` **§247 / §248 / §249**；
实现记录与实测读数见 **`§250`**。

**本模块要守住的主张（逐条可判）：**

  1. **单一新 seam**：``commit_start_failure`` 恰 **3** 个 keyword-only 入参、全无默认值、返回 ``None``；
  2. **封闭状态面**：只接受 ``latest is None``（T1）与 ``latest.state is WAITING``（T2）；
     ``RUNNING`` / ``WAITING_INPUT`` / 任一 terminal ⇒ **显式失败**（⛔ 不抢占 ``record_outcome``
     的 ``RUNNING → FAILED`` authority）；
  3. **identity 规则**：T1 由 ``[10]`` **mint**（``attempt`` 必须 == 1）· T2 **沿用** ``agent_id``
     （⛔ 不 re-mint）· 且 T2 的守卫顺序 = **先** exact attempt、**再** ``WAITING``；
  4. **`[10]` 不拥有 failure 分类权**：signature / 类型引用 / 导入面**都不含** adapter 概念
     （⚠️ **NOT PROVEN HERE**：调用方不会把 ``CapabilityMismatch`` 送进来 —— 那是 **`P6` wiring**
     的生产可达性锁）。

⚠️ **禁止子串扫描**：本文件一律 **AST 级**（检测器见 `_start_failure_locks.py`）。
⚠️ 敏感性 probe 一律在**源码字符串**上完成，⛔ **不覆写** ``src/`` 里的真实文件；
   每条 probe 的锚点都先做 ``count == 1`` 预检（⛔ 不靠「替换后字符串变了」这一条弱判据）。
"""

from __future__ import annotations

import ast
import pathlib

import pytest

import test_agent_manager  # 权威 allowlist（L1 / L2b）
import test_terminal  # 权威冻结表（FROZEN_METHOD_HASHES / UPDATED_METHOD_HASHES / LT-4 计数）
from adaptive_router.agent_manager import AgentManager, AgentManagerError
from adaptive_router.contracts import (
    AgentRun,
    BranchId,
    BranchState,
    ContractError,
    ContextBundle,
    new_agent_id,
    new_branch_id,
    new_run_id,
)
from adaptive_router.dispatch_decisions import DispatchAction, DispatchDecision
from adaptive_router.runtime_control_facts import RuntimeControlFacts

from _start_failure_locks import (
    call_target_counts,
    calls_to,
    clock_call_hits,
    exception_subclass_names,
    imported_module_tokens,
    imported_names,
    manager_data_attribute_reads,
    manager_self_attribute_reads,
    method_source_hashes,
    module_constant_expression,
    raise_guards,
    referenced_names,
    signature_shape,
)

MODULE_DOC = __doc__ or ""

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
SRC = REPO_ROOT / "src/adaptive_router"
MANAGER_PATH = SRC / "agent_manager.py"
SOURCE = MANAGER_PATH.read_text(encoding="utf-8")

CLASS = "AgentManager"
ENTRY = "commit_start_failure"

#: `LSF-1` —— 精确 keyword-only 形状：3 项、全部无默认值、无 vararg / kwarg。
EXPECTED_SIGNATURE = {
    "positional": ["self"],
    "kwonly": ["branch_id", "attempt", "ended_at"],
    "defaults": [False, False, False],
    "vararg": None,
    "kwarg": None,
}

#: `LSF-10` —— 入口的**五个**显式失败点（顺序即冻结契约：形状 ×2 → T1 → T2 attempt → T2 state）。
EXPECTED_GUARDS = (
    ("type(attempt) is not int or attempt < 1", "AgentManagerError"),
    ("type(ended_at) is not str or not ended_at.strip()", "AgentManagerError"),
    ("attempt != 1", "AgentManagerError"),
    ("latest.attempt != attempt", "AgentManagerError"),
    ("latest.state is not BranchState.WAITING", "AgentManagerError"),
)

#: `LSF-2` —— 入口内允许出现的 ``BranchState.<成员>`` 引用（`WAITING` = 准入判定；`FAILED` = 写入的终态）。
EXPECTED_BRANCHSTATE_REFS = frozenset({"WAITING", "FAILED"})

#: `LSF-2` —— ⛔ 入口内**不得**出现的成员（那些属 `record_outcome` / stop-all 的语义）。
FORBIDDEN_BRANCHSTATE_REFS = frozenset(
    {"RUNNING", "WAITING_INPUT", "COMPLETED", "ADOPTED", "OVERRIDDEN", "STOPPED", "REJECTED"}
)

#: `LSF-7` —— `new_agent_id()` 调用点 = **4**（既有 3 + 新增恰 1，且新增的必须落在 T1 分支内）。
EXPECTED_NEW_AGENT_ID_CALLS = 4

#: `LSF-8` —— `AgentRun(` 构造点仍**恰 1**（= `_agent_run`）。
EXPECTED_AGENT_RUN_CONSTRUCTION_SITES = 1

#: 模块级写点计数 —— 授权新增 seam 必然移动这两个数（Lock Amendment `§250.4`；`§158.4 ②` 同口径）。
EXPECTED_REPLACE_CALLS = 5
EXPECTED_RECORDS_APPEND_CALLS = 4

#: 第 16 个既有无表方法（`FROZEN_METHOD_HASHES` 11 + `UPDATED_METHOD_HASHES` 4 + 它 = 16）。
EXPECTED_CLOSE_RUN_HASH = "dc3049980ab67541"

#: `LSF-6` —— ⛔ 不得被引用的 adapter 概念（`referenced_names` = ImportFrom 原名 ∪ `Name.id` ∪ `Attribute.attr`）。
ADAPTER_TOKENS = ("AdapterFailure", "CapabilityMismatch", "AdapterStartReceipt")

#: `LSF-11` —— `agent_manager.py` 的**精确** import token 集（逐字不变）。
FROZEN_IMPORT_TOKENS = frozenset(
    {
        "__future__",
        "annotations",
        "typing",
        "Mapping",
        "contracts",
        "common",
        "contracts.common",
        "agent_start_requests",
        "dispatch_decisions",
        "runtime_control_facts",
    }
)

#: `LSF-13` —— repo-wide `record_outcome` 调用点（权威锁 = `test_completion_wiring.py::LCW-13`）。
EXPECTED_REPO_WIDE_RECORD_OUTCOME_CALLS = 1

# ⚠️ probe 锚点常量：**必须**成对使用才能唯一 —— 单看 `if latest.attempt != attempt:` 或
#    `if attempt != 1:` 都会同时命中**既有**的 `record_outcome` / `commit_started`（同一冻结形态）。
_T2_ATTEMPT_GUARD = (
    "        if latest.attempt != attempt:\n"
    "            raise AgentManagerError(\n"
    '                f"{branch_id} 的最新 attempt 是 {latest.attempt}，不是 {attempt}"\n'
    "            )\n"
)
_T2_WAITING_GUARD = (
    "        if latest.state is not BranchState.WAITING:\n"
    "            raise AgentManagerError(\n"
    '                f"{branch_id} 的 attempt {attempt} 处于 {latest.state.value}；"\n'
    '                "本入口只接受从未进入 RUNNING 的已派发失败（WAITING）"\n'
    "            )\n"
)
_T1_ATTEMPT_ONE_GUARD = (
    "        if latest is None:\n"
    "            # T1 · NO RECORD：**先**判 first-attempt 语义（在 licence 规则下必为 1）\n"
    "            if attempt != 1:"
)


# ==========================================================================
# 测试侧局部 AST helper（⛔ 不并入转发层 · 与 `_completion_wiring_locks.py` 同一先例）
# ==========================================================================


def _replace_once(source: str, old: str, new: str) -> str:
    """probe 专用：锚点**必须恰一处**（⛔ 不用「替换后变了」这种弱判据）。"""
    assert source.count(old) == 1, f"探针锚点 count = {source.count(old)} :: {old[:60]!r}"
    return source.replace(old, new, 1)


def _class_node(source: str = SOURCE, name: str = CLASS) -> ast.ClassDef:
    for node in ast.parse(source).body:
        if isinstance(node, ast.ClassDef) and node.name == name:
            return node
    raise AssertionError(f"未找到类 {name}")


def _method_node(source: str = SOURCE, name: str = ENTRY) -> ast.FunctionDef:
    for node in _class_node(source).body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"未找到方法 {CLASS}.{name}")


def _return_annotation(source: str = SOURCE, name: str = ENTRY) -> str:
    """方法级返回标注的源码形态（口径同 `_composition_root_locks.function_return_annotation`）。"""
    node = _method_node(source, name)
    assert node.returns is not None, f"{name} 缺少返回标注"
    return ast.unparse(node.returns)


def _guard_source(node: ast.If) -> str:
    return ast.unparse(node.test)


def _raise_sites(source: str = SOURCE, name: str = ENTRY) -> tuple[tuple[int, str], ...]:
    """入口内每个 ``raise`` 的 ``(行号, 最近守卫条件源码形态)``。

    口径与转发的 ``raise_guards`` **同源**（自证见 `test_helper_matches_forwarded_detector`），
    ⛔ 不是为了另立一套判断；`LSF-2` / `LSF-10` 的**顺序**断言需要**行号**，而
    ``raise_guards`` 只给条件与异常名。
    """
    node = _method_node(source, name)
    sites: list[tuple[int, str]] = []

    def walk(body: list[ast.stmt], guard: str) -> None:
        for stmt in body:
            if isinstance(stmt, ast.Raise):
                sites.append((stmt.lineno, guard))
            elif isinstance(stmt, ast.If):
                walk(stmt.body, _guard_source(stmt))
                walk(stmt.orelse, guard)
            else:
                walk(getattr(stmt, "body", []), guard)

    walk(node.body, "<top>")
    return tuple(sorted(sites))


def _branch_state_refs(node: ast.AST) -> set[str]:
    """``node`` 内引用到的 ``BranchState.<成员>`` 成员名（AST 级 · ⛔ 不是子串计数）。"""
    return {
        child.attr
        for child in ast.walk(node)
        if isinstance(child, ast.Attribute)
        and isinstance(child.value, ast.Name)
        and child.value.id == "BranchState"
    }


def _t1_if_node(source: str = SOURCE, name: str = ENTRY) -> ast.If:
    """T1 的准入分支（``if latest is None:``）—— 按**条件形态**定位，⛔ 不靠行号猜测。"""
    node = _method_node(source, name)
    for stmt in node.body:
        if isinstance(stmt, ast.If) and _guard_source(stmt) == "latest is None":
            return stmt
    raise AssertionError("未找到 `latest is None` 准入分支")


def _body_span(node: ast.If) -> tuple[int, int]:
    return node.body[0].lineno, node.body[-1].end_lineno


def _calls_in_span(symbols: tuple[str, ...], span: tuple[int, int], source: str = SOURCE):
    lo, hi = span
    return [(name, line) for name, line in calls_to(source, symbols) if lo <= line <= hi]


def _calls_with_owner(source: str, names: tuple[str, ...]) -> list[tuple[int, str]]:
    """``(行号, 所属函数名)`` —— **全作用域** Call visitor（⛔ 不是子串计数）。

    ⚠️ 与 `test_completion_wiring.py::_calls_with_owner`（`5B.6.50.3.2-impl.1` 修正后）**同口径**：
    覆盖 class 方法 · `AsyncFunctionDef` · 嵌套函数 · 模块级 / class body 直接调用
    （模块级归属 ``<module>``）。权威的 repo-wide 锁仍是 `LCW-13`，这里只做**本单元**的交叉核对。
    """
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
            target = ast.unparse(node.func)
            if target in names or target.split(".")[-1] in names:
                out.append((node.lineno, self.owners[-1] if self.owners else "<module>"))
            self.generic_visit(node)

    Visitor().visit(ast.parse(source))
    return out


# ==========================================================================
# 行为测试的替身
# ==========================================================================


def bundle(goal: str = "g") -> ContextBundle:
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


def manager(*branch_ids: BranchId, limit: int = 2) -> AgentManager:
    return AgentManager(
        run_id=new_run_id(), branch_order=tuple(branch_ids), total_attempt_limit=limit
    )


def decide(branch_id: BranchId, action: DispatchAction) -> DispatchDecision:
    return DispatchDecision(branch_id=branch_id, action=action, reason=f"test-{action.value}")


def queue(subject: AgentManager, branch_id: BranchId) -> None:
    """把某分支物化成 ``WAITING`` 占位（已派发、⛔ 未进入 ``RUNNING``）。"""
    subject.materialize(decisions=(decide(branch_id, DispatchAction.QUEUE),), bundles={})


def start_and_commit(subject: AgentManager, branch_id: BranchId, *, handle: str = "h-1") -> None:
    subject.materialize(
        decisions=(decide(branch_id, DispatchAction.START),),
        bundles={branch_id: bundle(f"goal-{branch_id}")},
    )
    subject.commit_started(
        branch_id=branch_id, attempt=1, handle=handle, started_at="2026-09-20T00:00:00Z"
    )


def inject_record(
    subject: AgentManager,
    branch_id: BranchId,
    *,
    state: BranchState,
    attempt: int = 1,
    started_at: str | None = None,
    ended_at: str | None = None,
    handle: str | None = None,
) -> AgentRun:
    """直接注入一条历史记录（白盒 seam）。

    ⚠️ **先例 = `test_deny_wiring.py` / `test_resume_wiring.py`**：``WAITING_INPUT``
    **没有公开入口**（登记在 `test_agent_manager.py` 的模块 docstring），而封闭状态面**必须**
    覆盖它 ⇒ 只能直接构造记录。``agent_id`` 用**公开工厂**生成（⛔ 不手写字面量，避免绕过契约校验）。
    """
    record = AgentRun(
        run_id=subject.routing_run.run_id,
        branch_id=branch_id,
        agent_id=new_agent_id(),
        attempt=attempt,
        state=state,
        started_at=started_at,
        ended_at=ended_at,
        handle=handle,
    )
    subject._records.append(record)
    return record


# ==========================================================================
# LSF-1 签名
# ==========================================================================


class TestLSF1Signature:
    def test_exactly_three_kwonly_without_defaults(self) -> None:
        assert signature_shape(SOURCE, CLASS, ENTRY) == EXPECTED_SIGNATURE

    def test_returns_none(self) -> None:
        # state command，⛔ 不是 query seam：返回 BranchId 是重复信息、返回 AgentRun 会暴露内部 agent_id
        assert _return_annotation() == "None"

    def test_probe_default_value_goes_red(self) -> None:
        mutated = _replace_once(
            SOURCE,
            "        attempt: int,\n        ended_at: str,\n    ) -> None:",
            "        attempt: int = 1,\n        ended_at: str,\n    ) -> None:",
        )
        assert signature_shape(mutated, CLASS, ENTRY) != EXPECTED_SIGNATURE

    def test_probe_extra_parameter_goes_red(self) -> None:
        mutated = _replace_once(
            SOURCE,
            "        attempt: int,\n        ended_at: str,\n    ) -> None:",
            "        attempt: int,\n        failure: object,\n        ended_at: str,\n"
            "    ) -> None:",
        )
        assert signature_shape(mutated, CLASS, ENTRY) != EXPECTED_SIGNATURE

    def test_probe_non_none_return_goes_red(self) -> None:
        mutated = _replace_once(
            SOURCE,
            "        ended_at: str,\n    ) -> None:\n"
            '        """把一次**已派发但从未进入 RUNNING**',
            "        ended_at: str,\n    ) -> BranchId:\n"
            '        """把一次**已派发但从未进入 RUNNING**',
        )
        assert _return_annotation(mutated) != "None"


# ==========================================================================
# LSF-2 封闭状态面 + guard 顺序
# ==========================================================================


class TestLSF2SealedSurfaceAndGuardOrder:
    def test_admission_is_none_or_waiting_only(self) -> None:
        refs = _branch_state_refs(_method_node())
        assert refs == EXPECTED_BRANCHSTATE_REFS
        assert refs & FORBIDDEN_BRANCHSTATE_REFS == set()

    def test_t1_admission_condition_is_exactly_latest_is_none(self) -> None:
        assert _guard_source(_t1_if_node()) == "latest is None"

    def test_t2_admission_condition_is_exactly_waiting(self) -> None:
        guards = [condition for _line, condition in _raise_sites()]
        assert "latest.state is not BranchState.WAITING" in guards

    def test_guard_order_attempt_identity_before_waiting(self) -> None:
        guards = [condition for _line, condition in _raise_sites()]
        attempt_at = guards.index("latest.attempt != attempt")
        waiting_at = guards.index("latest.state is not BranchState.WAITING")
        assert attempt_at < waiting_at, "exact attempt identity 必须先于 WAITING 判定"

    def test_helper_matches_forwarded_detector(self) -> None:
        # 自证：局部 helper（带行号）与转发的 `raise_guards` 条件序列**逐条一致**（⛔ 不是第二套判定）
        assert tuple(condition for _line, condition in _raise_sites()) == tuple(
            condition for condition, _exc in raise_guards(SOURCE, CLASS, ENTRY)
        )

    def test_probe_running_admitted_goes_red(self) -> None:
        # `MSF-2` 靶：放宽 sealed state ⇒ `LSF-2` 必须红
        mutated = _replace_once(
            SOURCE,
            _T2_ATTEMPT_GUARD + _T2_WAITING_GUARD,
            _T2_ATTEMPT_GUARD + _T2_WAITING_GUARD.replace(
                "        if latest.state is not BranchState.WAITING:",
                "        if latest.state not in (BranchState.WAITING, BranchState.RUNNING):",
            ),
        )
        assert _branch_state_refs(_method_node(mutated)) != EXPECTED_BRANCHSTATE_REFS

    def test_probe_swapped_guard_order_goes_red(self) -> None:
        mutated = _replace_once(
            SOURCE,
            _T2_ATTEMPT_GUARD + _T2_WAITING_GUARD,
            _T2_WAITING_GUARD + _T2_ATTEMPT_GUARD,
        )
        guards = [condition for _line, condition in _raise_sites(mutated)]
        assert guards.index("latest.attempt != attempt") > guards.index(
            "latest.state is not BranchState.WAITING"
        )


# ==========================================================================
# LSF-3 / LSF-4 / LSF-5 冻结面
# ==========================================================================


class TestLSF3ModuleConstantsUnchanged:
    def test_live_states(self) -> None:
        assert module_constant_expression(SOURCE, "_LIVE_STATES") == (
            "(BranchState.RUNNING, BranchState.WAITING_INPUT)"
        )

    def test_outcome_states(self) -> None:
        assert module_constant_expression(SOURCE, "_OUTCOME_STATES") == (
            "(BranchState.COMPLETED, BranchState.FAILED, BranchState.ADOPTED,"
            " BranchState.OVERRIDDEN)"
        )

    def test_stoppable_states(self) -> None:
        assert module_constant_expression(SOURCE, "_STOPPABLE_STATES") == (
            "(BranchState.WAITING, BranchState.RUNNING, BranchState.WAITING_INPUT)"
        )

    def test_probe_waiting_into_live_states_goes_red(self) -> None:
        # `MSF-3` 靶
        mutated = _replace_once(
            SOURCE,
            "_LIVE_STATES = (BranchState.RUNNING, BranchState.WAITING_INPUT)",
            "_LIVE_STATES = (BranchState.RUNNING, BranchState.WAITING,"
            " BranchState.WAITING_INPUT)",
        )
        assert module_constant_expression(mutated, "_LIVE_STATES") != (
            "(BranchState.RUNNING, BranchState.WAITING_INPUT)"
        )


class TestLSF4RecordOutcomeUntouched:
    def test_hash_matches_authority_table(self) -> None:
        hashes = method_source_hashes(SOURCE, CLASS)
        assert hashes["record_outcome"] == test_terminal.FROZEN_METHOD_HASHES["record_outcome"]

    def test_probe_semantic_change_goes_red(self) -> None:
        mutated = _replace_once(
            SOURCE,
            "        if state not in _OUTCOME_STATES:",
            "        if state not in _OUTCOME_STATES and False:",
        )
        assert (
            method_source_hashes(mutated, CLASS)["record_outcome"]
            != test_terminal.FROZEN_METHOD_HASHES["record_outcome"]
        )


class TestLSF5LicenceStartUntouched:
    def test_hash_matches_authority_table(self) -> None:
        hashes = method_source_hashes(SOURCE, CLASS)
        assert hashes["_licence_start"] == test_terminal.FROZEN_METHOD_HASHES["_licence_start"]

    def test_probe_start_precreating_a_record_goes_red(self) -> None:
        # `MSF-1` 靶：START 预建记录（A13.2 两阶段物化被破坏）
        mutated = _replace_once(
            SOURCE,
            "        if branch_id not in bundles:",
            "        self._records.append(None)  # probe\n        if branch_id not in bundles:",
        )
        assert (
            method_source_hashes(mutated, CLASS)["_licence_start"]
            != test_terminal.FROZEN_METHOD_HASHES["_licence_start"]
        )


# ==========================================================================
# LSF-6 adapter-independence
# ==========================================================================


class TestLSF6AdapterIndependence:
    def test_signature_has_no_failure_parameter(self) -> None:
        kwonly = signature_shape(SOURCE, CLASS, ENTRY)["kwonly"]
        assert "failure" not in kwonly
        assert set(kwonly) == {"branch_id", "attempt", "ended_at"}

    def test_no_adapter_type_references(self) -> None:
        assert sorted(set(ADAPTER_TOKENS) & set(referenced_names(SOURCE))) == []

    def test_no_adapter_imports(self) -> None:
        assert "adapters" not in imported_module_tokens(SOURCE)
        assert set(ADAPTER_TOKENS) & set(imported_names(SOURCE)) == set()

    def test_probe_adapter_type_reference_goes_red(self) -> None:
        # 自证：`referenced_names` **不是**空转 —— 塞一个 adapter 概念必须被抓到
        mutated = SOURCE + (
            "\n\ndef _peek(failure: object) -> str:\n    return AdapterFailure.__name__\n"
        )
        assert set(ADAPTER_TOKENS) & set(referenced_names(mutated)) == {"AdapterFailure"}

    def test_no_adapter_symbol_substring_in_source(self) -> None:
        """⚠️ **子串**口径的既有锁：`test_adapter_contract.py::TestL11_7_agent_manager_stays_clean`
        对 `agent_manager.py` **全文件**（含注释与 docstring）禁止这 4 个符号名。

        ⇒ 本单元的**实现文本**（含 `§249.1` 冻结骨架里那句原本提到适配层类型的 docstring）
        必须满足该约束 —— `§250.4` 登记的那处**口径修正**即由此而来（语义不变 · 只去掉符号名）。
        """
        for symbol in ("AdapterStartReceipt", "AdapterFailure", "AdapterError", "handleFact"):
            assert symbol not in SOURCE, symbol

    def test_not_proven_here_is_registered(self) -> None:
        """`LSF-6` 的**边界**（`§248.3`）：本层**只**证 `[10]` 不拥有 failure 分类权。

        ⛔ **不证**「`CapabilityMismatch` 调用方不会把失败送进本入口」—— 那是 **`P6` wiring**
        的生产可达性锁（call-path reachability invariant）⇒ 本断言确保该分层**被显式登记**，
        以防实现报告里被悄悄改写成 CLOSED。
        """
        assert "NOT PROVEN HERE" in MODULE_DOC
        assert "P6" in MODULE_DOC


# ==========================================================================
# LSF-7 / LSF-8 / LSF-9 / LSF-10 mint 点与 attempt 形状
# ==========================================================================


class TestLSF7NewAgentIdSites:
    def test_exactly_four_call_sites(self) -> None:
        assert len(calls_to(SOURCE, ("new_agent_id",))) == EXPECTED_NEW_AGENT_ID_CALLS

    def test_the_new_site_is_inside_the_t1_branch(self) -> None:
        node = _method_node()
        assert len(_calls_in_span(("new_agent_id",), (node.lineno, node.end_lineno))) == 1
        assert len(_calls_in_span(("new_agent_id",), _body_span(_t1_if_node()))) == 1

    def test_probe_missing_mint_goes_red(self) -> None:
        mutated = _replace_once(
            SOURCE,
            "                    agent_id=new_agent_id(),\n"
            "                    attempt=attempt,\n"
            "                    state=BranchState.FAILED,",
            '                    agent_id="agent-x",\n'
            "                    attempt=attempt,\n"
            "                    state=BranchState.FAILED,",
        )
        assert len(calls_to(mutated, ("new_agent_id",))) == EXPECTED_NEW_AGENT_ID_CALLS - 1


class TestLSF8SingleConstructionSite:
    def test_exactly_one_agent_run_construction(self) -> None:
        assert len(calls_to(SOURCE, ("AgentRun",))) == EXPECTED_AGENT_RUN_CONSTRUCTION_SITES

    def test_probe_second_construction_site_goes_red(self) -> None:
        # `MSF-6` 靶：绕过 `_agent_run` 直接造 AgentRun
        mutated = SOURCE + "\n\ndef _sneak(**kw):\n    return AgentRun(**kw)\n"
        assert len(calls_to(mutated, ("AgentRun",))) == EXPECTED_AGENT_RUN_CONSTRUCTION_SITES + 1


class TestLSF9NoMintOnT2:
    def test_t2_region_has_no_mint(self) -> None:
        node = _method_node()
        _t1_start, t1_end = _body_span(_t1_if_node())
        assert _calls_in_span(("new_agent_id",), (t1_end + 1, node.end_lineno)) == []

    def test_probe_t2_remint_goes_red(self) -> None:
        # `MSF-4` 靶：T2 也 mint ⇒ 身份不再沿用
        mutated = _replace_once(
            SOURCE,
            "                agent_id=latest.agent_id,\n"
            "                attempt=attempt,\n"
            "                state=BranchState.FAILED,",
            "                agent_id=new_agent_id(),\n"
            "                attempt=attempt,\n"
            "                state=BranchState.FAILED,",
        )
        node = _method_node(mutated)
        _t1_start, t1_end = _body_span(_t1_if_node(mutated))
        assert _calls_in_span(("new_agent_id",), (t1_end + 1, node.end_lineno), mutated) != []


class TestLSF10AttemptShapeGuards:
    def test_five_guards_in_frozen_order(self) -> None:
        assert raise_guards(SOURCE, CLASS, ENTRY) == EXPECTED_GUARDS

    def test_t1_requires_attempt_one_and_t2_requires_exact_attempt(self) -> None:
        guards = [condition for _line, condition in _raise_sites()]
        assert "attempt != 1" in guards
        assert "latest.attempt != attempt" in guards

    def test_probe_t1_attempt_two_allowed_goes_red(self) -> None:
        # `MSF-5` 靶：NO RECORD + attempt=2 放行
        mutated = _replace_once(
            SOURCE,
            _T1_ATTEMPT_ONE_GUARD,
            _T1_ATTEMPT_ONE_GUARD.replace(
                "            if attempt != 1:", "            if attempt != 1 and False:"
            ),
        )
        assert raise_guards(mutated, CLASS, ENTRY) != EXPECTED_GUARDS


# ==========================================================================
# LSF-11 零时钟 / 导入面
# ==========================================================================


class TestLSF11NoClockNoImportDrift:
    def test_no_clock_calls(self) -> None:
        assert clock_call_hits(SOURCE) == []

    def test_import_token_set_unchanged(self) -> None:
        assert set(imported_module_tokens(SOURCE)) == FROZEN_IMPORT_TOKENS

    def test_probe_import_datetime_goes_red(self) -> None:
        mutated = SOURCE + "\n\nimport datetime  # probe\n"
        assert set(imported_module_tokens(mutated)) != FROZEN_IMPORT_TOKENS


# ==========================================================================
# LSF-12 方法集 / 哈希（`§248.6` ⑤）
# ==========================================================================


class TestLSF12MethodSetAndHashes:
    def test_method_set_is_exactly_seventeen(self) -> None:
        methods = method_source_hashes(SOURCE, CLASS)
        assert set(methods) == set(test_terminal.FROZEN_METHOD_HASHES) | set(
            test_terminal.UPDATED_METHOD_HASHES
        ) | {"close_run"}
        # `5B.6.52.3`（P2）：授权新增 `commit_run_attempt` ⇒ 17 → 18。
        # ⛔ Test ID **不改号**（继承同 class 内 `test_existing_sixteen_hashes_unchanged` 的冻结 ID 惯例：
        # 只移动常量，不改测试名）；表仍是**唯一权威** `test_terminal.UPDATED_METHOD_HASHES`。
        assert len(methods) == 18

    def test_new_entry_is_declared_in_the_update_table(self) -> None:
        # `§248.6` ⑤ 原样：新方法必须**正式加入** `UPDATED_METHOD_HASHES`（⛔ 不是"旧 16 ∪ 新 = 17"）
        assert ENTRY in test_terminal.UPDATED_METHOD_HASHES
        assert (
            method_source_hashes(SOURCE, CLASS)[ENTRY]
            == test_terminal.UPDATED_METHOD_HASHES[ENTRY]
        )

    def test_existing_sixteen_hashes_unchanged(self) -> None:
        hashes = method_source_hashes(SOURCE, CLASS)
        for name, digest in test_terminal.FROZEN_METHOD_HASHES.items():
            assert hashes[name] == digest, name
        for name, digest in test_terminal.UPDATED_METHOD_HASHES.items():
            if name == ENTRY:
                continue
            assert hashes[name] == digest, name
        assert hashes["close_run"] == EXPECTED_CLOSE_RUN_HASH

    def test_probe_undeclared_new_method_goes_red(self) -> None:
        mutated = _replace_once(
            SOURCE,
            "    def close_run(",
            "    def undeclared_seam(self) -> None:\n        return None\n\n    def close_run(",
        )
        methods = method_source_hashes(mutated, CLASS)
        assert set(methods) != set(test_terminal.FROZEN_METHOD_HASHES) | set(
            test_terminal.UPDATED_METHOD_HASHES
        ) | {"close_run"}

    def test_probe_renamed_entry_goes_red(self) -> None:
        mutated = _replace_once(
            SOURCE, "    def commit_start_failure(\n", "    def commit_start_failure_x(\n"
        )
        assert ENTRY not in method_source_hashes(mutated, CLASS)


# ==========================================================================
# LSF-13 `record_outcome` 未被新入口调用
# ==========================================================================


class TestLSF13RecordOutcomeNotDelegated:
    def test_agent_manager_never_calls_record_outcome(self) -> None:
        assert calls_to(SOURCE, ("record_outcome",)) == []
        assert calls_to(SOURCE, ("self.record_outcome",)) == []

    def test_repo_wide_call_sites_still_one(self) -> None:
        sites: list[tuple[str, int, str]] = []
        for path in sorted(SRC.rglob("*.py")):
            if "__pycache__" in str(path):
                continue
            for line, owner in _calls_with_owner(
                path.read_text(encoding="utf-8"), ("record_outcome",)
            ):
                sites.append((str(path.relative_to(REPO_ROOT)).replace("\\", "/"), line, owner))
        assert len(sites) == EXPECTED_REPO_WIDE_RECORD_OUTCOME_CALLS
        assert sites[0][0].endswith("terminal_wiring.py")
        assert sites[0][2] == "_settle"

    def test_probe_delegation_goes_red(self) -> None:
        # `MSF-7` 靶：新入口改调 `record_outcome`
        mutated = _replace_once(
            SOURCE,
            "        latest = self._latest(branch_id)\n        if latest is None:\n"
            "            # T1 · NO RECORD",
            "        self.record_outcome(\n"
            "            branch_id=branch_id, attempt=attempt,"
            " state=BranchState.FAILED, ended_at=ended_at\n"
            "        )\n"
            "        latest = self._latest(branch_id)\n        if latest is None:\n"
            "            # T1 · NO RECORD",
        )
        assert calls_to(mutated, ("record_outcome",)) != []

    def test_helper_sees_calls_inside_class_methods(self) -> None:
        # 覆盖自证（`LCW-13` 修正教训）：class 方法内的调用**必须**被看见
        probe = "class _C:\n    def m(self):\n        return record_outcome()\n"
        assert _calls_with_owner(probe, ("record_outcome",)) == [(3, "m")]


# ==========================================================================
# 行为：T1（无历史记录 ⇒ 新 mint + FAILED）
# ==========================================================================


class TestT1FreshRecord:
    def test_creates_failed_record_without_start_facts(self) -> None:
        branch = new_branch_id()
        subject = manager(branch)
        subject.commit_start_failure(branch_id=branch, attempt=1, ended_at="T-fail")

        record = subject.branch_record(branch)
        assert record is not None
        assert record.run_id == subject.routing_run.run_id
        assert record.branch_id == branch
        assert record.attempt == 1
        assert record.state is BranchState.FAILED
        assert record.started_at is None
        assert record.handle is None
        assert record.ended_at == "T-fail"
        assert record.is_terminal is True
        assert isinstance(record.agent_id, str) and record.agent_id.strip()

    def test_exactly_one_record_and_no_capacity_leak(self) -> None:
        branch = new_branch_id()
        subject = manager(branch)
        subject.commit_start_failure(branch_id=branch, attempt=1, ended_at="T-fail")
        assert len(subject.routing_run.branches) == 1
        assert subject.active_subagent_count == 0
        assert subject.pending_admission_branch_ids == ()

    def test_does_not_close_the_run(self) -> None:
        branch = new_branch_id()
        subject = manager(branch)
        subject.commit_start_failure(branch_id=branch, attempt=1, ended_at="T-fail")
        assert subject.routing_run.terminal is None
        assert subject.routing_run.control_flow_converged is False

    def test_second_call_on_same_attempt_fails(self) -> None:
        # T1 之后记录已 terminal ⇒ 同 attempt 再次提交必须失败（⛔ 不是幂等写点）
        branch = new_branch_id()
        subject = manager(branch)
        subject.commit_start_failure(branch_id=branch, attempt=1, ended_at="T-fail")
        with pytest.raises(AgentManagerError):
            subject.commit_start_failure(branch_id=branch, attempt=1, ended_at="T-fail-2")
        assert subject.branch_record(branch).ended_at == "T-fail"

    def test_other_branches_are_untouched(self) -> None:
        first, second = new_branch_id(), new_branch_id()
        subject = manager(first, second)
        queue(subject, second)
        subject.commit_start_failure(branch_id=first, attempt=1, ended_at="T-fail")
        assert subject.branch_record(second).state is BranchState.WAITING
        assert subject.pending_admission_branch_ids == (second,)


class TestT1AttemptGuards:
    def test_attempt_two_without_history_is_rejected(self) -> None:
        branch = new_branch_id()
        subject = manager(branch, limit=3)
        with pytest.raises(AgentManagerError) as excinfo:
            subject.commit_start_failure(branch_id=branch, attempt=2, ended_at="T-fail")
        assert "T1 只接受 attempt == 1" in str(excinfo.value)
        assert subject.branch_record(branch) is None

    @pytest.mark.parametrize("bad", [True, False, 0, -1, "1", 1.0, None])
    def test_non_positive_or_non_int_attempt_is_rejected(self, bad: object) -> None:
        branch = new_branch_id()
        subject = manager(branch)
        with pytest.raises(AgentManagerError):
            subject.commit_start_failure(
                branch_id=branch, attempt=bad, ended_at="T-fail"  # type: ignore[arg-type]
            )
        assert subject.branch_record(branch) is None

    @pytest.mark.parametrize("bad", ["", "   ", None, 1, b"x"])
    def test_ended_at_must_be_explicit_non_empty_str(self, bad: object) -> None:
        branch = new_branch_id()
        subject = manager(branch)
        with pytest.raises(AgentManagerError):
            subject.commit_start_failure(
                branch_id=branch, attempt=1, ended_at=bad  # type: ignore[arg-type]
            )
        assert subject.branch_record(branch) is None

    @pytest.mark.parametrize("bad", ["", "   ", None, 7])
    def test_branch_id_is_validated(self, bad: object) -> None:
        subject = manager(new_branch_id())
        with pytest.raises(ContractError):
            subject.commit_start_failure(
                branch_id=bad, attempt=1, ended_at="T-fail"  # type: ignore[arg-type]
            )


# ==========================================================================
# 行为：T2（已有 WAITING ⇒ 沿用身份 · 全字段 carry-over）
# ==========================================================================


class TestT2WaitingPromotion:
    def test_public_path_queue_then_failure(self) -> None:
        branch = new_branch_id()
        subject = manager(branch)
        queue(subject, branch)
        waiting = subject.branch_record(branch)
        assert waiting.state is BranchState.WAITING

        subject.commit_start_failure(branch_id=branch, attempt=1, ended_at="T-fail-2")

        record = subject.branch_record(branch)
        assert len(subject.routing_run.branches) == 1  # ⛔ 不新建 attempt
        assert record.attempt == 1
        assert record.state is BranchState.FAILED
        assert record.ended_at == "T-fail-2"
        assert record.agent_id == waiting.agent_id
        assert record.started_at is None
        assert record.handle is None

    def test_all_fields_carried_over_verbatim(self) -> None:
        # 强断言（`§247` 修正 ③）：不是只看 agent_id —— 六个字段逐字段沿用
        branch = new_branch_id()
        subject = manager(branch)
        waiting = inject_record(
            subject,
            branch,
            state=BranchState.WAITING,
            started_at="T-queued",
            ended_at="T-queued-end",
            handle="h-queued",
        )
        subject.commit_start_failure(branch_id=branch, attempt=1, ended_at="T-fail-3")

        record = subject.branch_record(branch)
        assert record.run_id == waiting.run_id
        assert record.branch_id == waiting.branch_id
        assert record.attempt == waiting.attempt
        assert record.agent_id == waiting.agent_id
        assert record.started_at == waiting.started_at
        assert record.handle == waiting.handle
        # 只有 state 与 ended_at 改变
        assert record.state is BranchState.FAILED
        assert record.ended_at == "T-fail-3"

    def test_wrong_attempt_is_rejected_and_record_unchanged(self) -> None:
        branch = new_branch_id()
        subject = manager(branch, limit=3)
        queue(subject, branch)
        with pytest.raises(AgentManagerError):
            subject.commit_start_failure(branch_id=branch, attempt=2, ended_at="T-fail")
        assert subject.branch_record(branch).state is BranchState.WAITING

    @pytest.mark.parametrize("bad", [True, False])
    def test_bool_attempt_cannot_pass_first_attempt_rule(self, bad: bool) -> None:
        # `§249` ③：`True == 1` ⇒ 若无严格形状守卫，`True` 会穿过 T1 的 attempt != 1
        branch = new_branch_id()
        subject = manager(branch)
        with pytest.raises(AgentManagerError):
            subject.commit_start_failure(branch_id=branch, attempt=bad, ended_at="T-fail")


# ==========================================================================
# 行为：封闭状态面（sealed surface）
# ==========================================================================


class TestSealedSurfaceBehavior:
    @pytest.mark.parametrize(
        "state",
        [
            BranchState.COMPLETED,
            BranchState.FAILED,
            BranchState.ADOPTED,
            BranchState.OVERRIDDEN,
        ],
    )
    def test_terminal_states_are_rejected(self, state: BranchState) -> None:
        branch = new_branch_id()
        subject = manager(branch)
        start_and_commit(subject, branch)
        subject.record_outcome(branch_id=branch, attempt=1, state=state, ended_at="T-outcome")
        with pytest.raises(AgentManagerError):
            subject.commit_start_failure(branch_id=branch, attempt=1, ended_at="T-fail")
        record = subject.branch_record(branch)
        assert record.state is state
        assert record.ended_at == "T-outcome"

    def test_running_is_rejected_and_keeps_authority_with_record_outcome(self) -> None:
        branch = new_branch_id()
        subject = manager(branch)
        start_and_commit(subject, branch)
        with pytest.raises(AgentManagerError) as excinfo:
            subject.commit_start_failure(branch_id=branch, attempt=1, ended_at="T-fail")
        assert "只接受从未进入 RUNNING" in str(excinfo.value)
        record = subject.branch_record(branch)
        assert record.state is BranchState.RUNNING
        assert record.ended_at is None
        assert record.handle == "h-1"

    def test_waiting_input_is_rejected(self) -> None:
        branch = new_branch_id()
        subject = manager(branch)
        inject_record(subject, branch, state=BranchState.WAITING_INPUT, started_at="T-in")
        with pytest.raises(AgentManagerError):
            subject.commit_start_failure(branch_id=branch, attempt=1, ended_at="T-fail")
        assert subject.branch_record(branch).state is BranchState.WAITING_INPUT

    def test_stopped_is_rejected(self) -> None:
        branch = new_branch_id()
        subject = manager(branch)
        queue(subject, branch)
        subject.apply_runtime_control(
            facts=RuntimeControlFacts(stop_branches_requested=True), ended_at="T-stop"
        )
        with pytest.raises(AgentManagerError):
            subject.commit_start_failure(branch_id=branch, attempt=1, ended_at="T-fail")
        assert subject.branch_record(branch).state is BranchState.STOPPED

    def test_entry_does_not_own_planning_scope(self) -> None:
        """与 `commit_started` **同形**：本入口只校验 id 形状，⛔ 不校验 `branch_order`。

        （既有入口 `commit_started` 亦无 `branch_order` 守卫 ⇒ 这是**冻结骨架的既定语义**，
        ⛔ 不是本轮新增授权；规划权属 `[09]`，越界检查只存在于 `materialize`。）
        """
        outside = new_branch_id()
        subject = manager(new_branch_id())
        subject.commit_start_failure(branch_id=outside, attempt=1, ended_at="T-fail")
        assert subject.branch_record(outside).state is BranchState.FAILED
        assert subject.active_subagent_count == 0


class TestEntryBoundaries:
    def test_no_new_exception_type(self) -> None:
        assert exception_subclass_names(SOURCE) == ("AgentManagerError",)

    def test_attribute_and_self_reads_stay_within_allowlists(self) -> None:
        # `§249.3` 登记的实现边界：两个 allowlist **不扩集**（对照权威表，⛔ 不复制第二份）
        assert (
            manager_data_attribute_reads(SOURCE, enum_bases=("BranchState", "DispatchAction"))
            == test_agent_manager.ALLOWED_ATTRIBUTE_READS
        )
        assert manager_self_attribute_reads(SOURCE) == test_agent_manager.ALLOWED_SELF_READS

    def test_module_level_write_counts_follow_the_authorized_seam(self) -> None:
        # Lock Amendment `§250.4`（`§158.4 ②` 同口径）：授权新增 seam 必然移动这两个计数
        counts = call_target_counts(SOURCE)
        assert counts["self._replace"] == EXPECTED_REPLACE_CALLS
        assert counts["self._records.append"] == EXPECTED_RECORDS_APPEND_CALLS
        # 与权威表 `test_terminal.py` 的 LT-4 常量**逐值一致**（⛔ 两处不得各自漂移）
        assert EXPECTED_REPLACE_CALLS == test_terminal.EXPECTED_REPLACE_CALLS
        assert EXPECTED_RECORDS_APPEND_CALLS == test_terminal.EXPECTED_RECORDS_APPEND_CALLS

    def test_entry_never_opens_the_next_attempt(self) -> None:
        # 本入口 ⛔ 不造 `attempt + 1` / ⛔ 不 retry / ⛔ 不产 recovery 决策（`A19` = NOT TRIGGERED）
        branch = new_branch_id()
        subject = manager(branch, limit=3)
        subject.commit_start_failure(branch_id=branch, attempt=1, ended_at="T-fail")
        assert [record.attempt for record in subject.routing_run.branches] == [1]
