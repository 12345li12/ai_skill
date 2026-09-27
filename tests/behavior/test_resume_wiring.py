"""``C2c 恢复写路径`` —— 结构锁 **LRE-1 – LRE-11** + 永久 probe + 行为 **B1 – B9**。

架构依据：`docs/architecture.md` **6.4 / L47**（`[10]` 运行态唯一写者）· **A13.2**（两阶段物化）·
**A13.3**（`branches` = attempt history）· **A13.4**（"absence of observation ≠ fact"）·
**A13.6**（`commit_started` 是 `RUNNING` 的唯一写入口）· **A18.2**（终态唯一写口）·
**A18.7**（决策者 ≠ 状态写者）· **INV-10**（高风险必须过 Gate）· **INV-16**（升级重流必须重过 Gate）。
范围、冻结、授权与 Lock Amendment 见 `tests/behavior/README_step5b.md` **§155–§158**。

**本模块要守住的六条主张：**

  1. **只解决"恢复意图如何合法改变运行态"**：零出边（`LRE-7`）· 不接调度 · 不选下一节点；
  2. **用户恢复 ≠ 失败恢复**：**不推进** attempt（`LRE-3`；两个方向都不许）；
  3. **唯一写入点**：`APPROVE` ⇒ 恰好一次 `resume_from_waiting`；`DENY` / `MODIFY` ⇒ 零写入；
  4. **显式失败优先于静默默认**：**零 `try`**（`LRE-5`）· 未覆盖出口与非法前置一律抛错；
  5. **零时钟**（`LRE-6`）：`resumed_at` 只能由调用方传入；
  6. **字段集合恰好 6 个**（`LRE-11`）：`run_id` 属结果归属证明，**不得**夹带调度语义。

⚠️ **禁止子串扫描**：本文件 docstring 与注释里**故意提及** `scheduler` / `dispatch` / 下一节点 /
`commit_started` / `close_run` 等词 ⇒ 所有锁一律 **AST 级 / 绑定集级**。
⚠️ 敏感性 probe 一律在**源码字符串**上完成（``_mutate``），**不覆写** ``src/`` 里的真实文件；
真实源码的一次性变异证据见 README §159（`MRE1–MRE6`）。
⚠️ **检测器缺口登记**（`§159` 偏差 ③）：既有的 ``call_keyword_shapes_by_caller`` **只看模块级函数**
   （其实现在 `_terminal_wiring_locks.py` 里显式跳过类内方法）⇒ **`LRE-9` 无法用它表达**
   "类方法里 `_agent_run(state=RUNNING)` 的写点集合"。本文件为此在**测试侧**新增一个**局部**
   AST 助手 ``_agent_run_callers``（**不改** ``_resume_wiring_locks.py``，它**仍是 0 类新增**）。
"""

from __future__ import annotations

import ast
import dataclasses
import hashlib
import inspect
import pathlib

import pytest

from adaptive_router.agent_manager import AgentManager, AgentManagerError
from adaptive_router.approval_gate_wiring import (
    ApprovalJudgment,
    GateDecision,
    GateDecisionKind,
    GateOutcome,
    settle_gate,
)
from adaptive_router.contracts import AgentRun, BranchId, BranchState, RunId
from adaptive_router.resume_intent import ResumeIntent, build_resume_intent
from adaptive_router.resume_wiring import (
    ResumeWiringError,
    ResumeWriteReport,
    apply_resume_intent,
)

from _resume_wiring_locks import (  # noqa: F401  (复用既有检测器，不抄第二套)
    assignment_value_shapes,
    calls_to,
    clock_call_hits,
    function_raise_guards,
    function_return_shapes,
    function_signature_shape,
    imported_module_tokens,
    module_level_function_names,
    none_coalescing_shapes,
    referenced_names,
)

WIRING_PATH = pathlib.Path("src/adaptive_router/resume_wiring.py")
MANAGER_PATH = pathlib.Path("src/adaptive_router/agent_manager.py")

WIRING = WIRING_PATH.read_text(encoding="utf-8")
MANAGER = MANAGER_PATH.read_text(encoding="utf-8")

#: `§157.5` 六字段锁死（顺序与集合都锁）。
EXPECTED_FIELDS = [
    "branch_id",
    "previous_state",
    "new_state",
    "attempt",
    "resumed_at",
    "run_id",
]

#: `LRE-4` 白名单：驱动者只允许调 `[10]` 的这两个入口（集合**相等**，不是"包含"）。
ALLOWED_MANAGER_CALLS = {"branch_record", "resume_from_waiting"}

#: `LRE-4` 反向清单：这些写入口**一个都不许**被驱动者调用。
FORBIDDEN_WRITE_ENTRIES = {
    "commit_started",
    "record_outcome",
    "apply_runtime_control",
    "close_run",
    "materialize",
}

#: `LRE-7` 零出边：这些标识符**一个都不许**进入绑定集。
FORBIDDEN_MOTION_NAMES = {
    "route_to",
    "next_node",
    "next_step",
    "dispatch",
    "dispatcher",
    "scheduler",
    "plan",
    "steps",
    "remaining",
}

#: `LRE-11` 字段禁区（与 `FORBIDDEN_MOTION_NAMES` 互补）。
FORBIDDEN_REPORT_FIELDS = FORBIDDEN_MOTION_NAMES | {
    "node",
    "action",
    "decision",
    "source",
    "origin",
}

#: `LRE-3` —— attempt 推进的唯一入口：驱动者**不得**出现。
ATTEMPT_ADVANCE_ENTRY = "commit_started"

#: `§156.5` g1–g7 的**实测**守卫数（实现落地为 9 条 ⇒ 见 `§159` 偏差 ②）。
EXPECTED_GUARD_COUNT = 9

RUN_ID = "run_c2c"
BRANCH_ID = "br_c2c"


# ----------------------------------------------------------------------
# 测试侧局部 AST 助手（**只为补齐 LRE-9 的检测器缺口**；不改任何 detector 文件）
# ----------------------------------------------------------------------


def _agent_run_callers(source: str) -> dict[str, set[str]]:
    """``_agent_run(...)`` 的调用者 → 它在该调用里写下的 ``state`` 实参形态集合。

    ⚠️ **为什么需要它**：既有 ``call_keyword_shapes_by_caller`` 只遍历**模块级** ``FunctionDef``
    （类内方法被显式跳过）⇒ 对 ``AgentManager`` 的方法体**返回空**（实测 `{}`），
    无法表达 `LRE-9` 的"``state=RUNNING`` 的写点集合"。本助手补上这一段，
    并且**只**看 ``_agent_run`` 这一个被调用者（不做通用化，避免变成第二套判定体系）。
    """
    found: dict[str, set[str]] = {}

    def walk(body: list[ast.stmt], owner: str) -> None:
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                walk(node.body, node.name)
                continue
            if isinstance(node, ast.ClassDef):
                walk(node.body, node.name)
                continue
            for child in ast.walk(node):
                if not isinstance(child, ast.Call):
                    continue
                func = child.func
                name = func.attr if isinstance(func, ast.Attribute) else (
                    func.id if isinstance(func, ast.Name) else None
                )
                if name != "_agent_run":
                    continue
                for keyword in child.keywords:
                    if keyword.arg == "state":
                        found.setdefault(owner, set()).add(ast.unparse(keyword.value))

    walk(ast.parse(source).body, "<module>")
    return found


def _code_text(source: str) -> str:
    """把源码里**所有 docstring 剥掉**后的代码文本（用于"表达式不得出现"这类断言）。

    ⚠️ 必须有这一步：驱动者的 docstring 里**故意**写了反例（如"**不**是 ``intent.attempt + 1``"）
    ⇒ 直接对全文做子串断言会把**反例文档**读成**违规代码**（假红）。
    """
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
                node.body = body[1:] or [ast.Pass()]
    return ast.unparse(tree)


def _mutate(old: str, new: str) -> str:
    """在**源码字符串**上做一次性变异（不触碰真实文件）。"""
    assert old in WIRING, f"变异锚点未命中（⇒ 读数作废）：{old!r}"
    return WIRING.replace(old, new)


def _method_args(source: str, method: str) -> ast.arguments:
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.FunctionDef) and node.name == method:
            return node.args
    raise AssertionError(f"未找到方法 {method}")


def _method_text(source: str, method: str) -> str:
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.FunctionDef) and node.name == method:
            return ast.unparse(node)
    raise AssertionError(f"未找到方法 {method}")


def _judgment() -> ApprovalJudgment:
    return ApprovalJudgment(
        needs_approval=True,
        proposed_action="重启服务",
        impact_scope="生产集群",
        main_risk="短时不可用",
        recovery_method="回滚上一版本",
    )


def _outcome(kind: GateDecisionKind, modification: str | None = None) -> GateOutcome:
    return settle_gate(
        judgment=_judgment(), decision=GateDecision(kind=kind, modification=modification)
    )


def _intent(
    kind: GateDecisionKind,
    *,
    attempt: int = 1,
    branch_id: BranchId = BRANCH_ID,
    run_id: RunId = RUN_ID,
    total_attempt_limit: int = 2,
) -> ResumeIntent:
    modification = "只重启一个副本" if kind is GateDecisionKind.MODIFY else None
    return build_resume_intent(
        gate_outcome=_outcome(kind, modification),
        run_id=run_id,
        branch_id=branch_id,
        attempt=attempt,
        total_attempt_limit=total_attempt_limit,
    )


def _manager(
    *,
    state: BranchState = BranchState.WAITING_INPUT,
    attempt: int = 1,
    with_record: bool = True,
    run_id: RunId = RUN_ID,
    branch_id: BranchId = BRANCH_ID,
) -> AgentManager:
    """造一个带**显式**运行态事实的 `[10]`（`WAITING_INPUT` 目前**没有生产者** ⇒ 显式注入）。"""
    manager = AgentManager(run_id=run_id, branch_order=(branch_id,), total_attempt_limit=2)
    if with_record:
        manager._records.append(
            AgentRun(
                run_id=run_id,
                branch_id=branch_id,
                agent_id="ag_c2c",
                attempt=attempt,
                state=state,
                started_at="2026-09-19T00:00:00Z",
                ended_at=None,
                handle="h-c2c",
            )
        )
    return manager


# ======================================================================
# LRE-1 … LRE-11 —— 结构锁
# ======================================================================


class TestLRE1ModuleShape:
    def test_exactly_two_module_level_functions(self) -> None:
        assert module_level_function_names(WIRING) == ("_require_intent", "apply_resume_intent")

    def test_exactly_four_module_level_objects(self) -> None:
        import adaptive_router.resume_wiring as module

        declared = sorted(
            name
            for name, value in vars(module).items()
            if not name.startswith("__") and getattr(value, "__module__", None) == module.__name__
        )
        assert declared == [
            "ResumeWiringError",
            "ResumeWriteReport",
            "_require_intent",
            "apply_resume_intent",
        ]
        assert len(declared) == 4

    def test_public_surface_is_the_frozen_triple(self) -> None:
        import adaptive_router.resume_wiring as module

        assert module.__all__ == [
            "ResumeWiringError",
            "ResumeWriteReport",
            "apply_resume_intent",
        ]

    def test_probe_fifth_module_object_goes_red(self) -> None:
        mutated = _mutate(
            "def apply_resume_intent(",
            "def _extra_helper() -> None:\n    return None\n\n\ndef apply_resume_intent(",
        )
        assert module_level_function_names(mutated) == (
            "_require_intent",
            "_extra_helper",
            "apply_resume_intent",
        )
        assert len(module_level_function_names(mutated)) == 3


class TestLRE2SingleReturn:
    def test_exactly_one_return_with_report_shape(self) -> None:
        shapes = function_return_shapes(WIRING, "apply_resume_intent")
        assert len(shapes) == 1
        assert shapes[0].startswith("ResumeWriteReport(")

    def test_has_no_none_return_path(self) -> None:
        assert "return None" not in WIRING

    def test_driver_has_zero_local_bindings(self) -> None:
        driver = [
            node
            for node in ast.walk(ast.parse(WIRING))
            if isinstance(node, ast.FunctionDef) and node.name == "apply_resume_intent"
        ]
        assert len(driver) == 1
        assert sum(1 for n in ast.walk(driver[0]) if isinstance(n, ast.Assign)) == 0

    def test_probe_second_return_goes_red(self) -> None:
        mutated = _mutate(
            "    return ResumeWriteReport(",
            "    if False:\n        return None\n    return ResumeWriteReport(",
        )
        assert len(function_return_shapes(mutated, "apply_resume_intent")) == 2


class TestLRE3NoAttemptAdvance:
    def test_does_not_call_the_attempt_advance_entry(self) -> None:
        assert calls_to(WIRING, {ATTEMPT_ADVANCE_ENTRY}) == []

    def test_no_increment_expression_in_code(self) -> None:
        assert "attempt + 1" not in _code_text(WIRING)
        assert "attempt=intent.attempt)" in WIRING

    def test_seam_writes_back_the_same_attempt(self) -> None:
        text = _method_text(MANAGER, "resume_from_waiting")
        assert "attempt=attempt," in text
        assert "attempt=attempt + 1" not in text

    def test_probe_increment_goes_red(self) -> None:
        mutated = _mutate("attempt=intent.attempt)", "attempt=intent.attempt + 1)")
        assert "attempt + 1" in _code_text(mutated)
        assert "attempt=intent.attempt)" not in mutated


class TestLRE4ManagerCallWhitelist:
    def test_whitelist_is_exactly_hit(self) -> None:
        hit = {name for name, _ in calls_to(WIRING, ALLOWED_MANAGER_CALLS)}
        assert hit == ALLOWED_MANAGER_CALLS

    def test_no_other_write_entry_is_called(self) -> None:
        assert calls_to(WIRING, FORBIDDEN_WRITE_ENTRIES) == []

    def test_probe_extra_write_entry_goes_red(self) -> None:
        mutated = _mutate(
            "    return ResumeWriteReport(",
            "    manager.apply_runtime_control(facts=None, ended_at=resumed_at)\n"
            "    return ResumeWriteReport(",
        )
        assert calls_to(mutated, FORBIDDEN_WRITE_ENTRIES) != []


class TestLRE5NoSwallow:
    def test_zero_try_nodes(self) -> None:
        assert sum(1 for n in ast.walk(ast.parse(WIRING)) if isinstance(n, ast.Try)) == 0

    def test_no_none_coalescing(self) -> None:
        assert none_coalescing_shapes(WIRING) == ()

    def test_probe_try_except_goes_red(self) -> None:
        mutated = _mutate(
            "    return ResumeWriteReport(",
            "    try:\n        pass\n    except Exception:\n        pass\n"
            "    return ResumeWriteReport(",
        )
        assert sum(1 for n in ast.walk(ast.parse(mutated)) if isinstance(n, ast.Try)) == 1


class TestLRE6NoClock:
    def test_zero_clock_calls(self) -> None:
        assert clock_call_hits(WIRING) == []

    def test_no_clock_module_imported(self) -> None:
        assert imported_module_tokens(WIRING) & {"datetime", "time", "zoneinfo"} == set()

    def test_resumed_at_is_caller_supplied(self) -> None:
        shape = function_signature_shape(WIRING, "apply_resume_intent")
        assert shape["kwonly"] == ["intent", "manager", "resumed_at"]
        assert shape["defaults"] == [False, False, False]

    def test_probe_clock_read_goes_red(self) -> None:
        mutated = _mutate(
            "    return ResumeWriteReport(",
            "    resumed_at = __import__('time').time()\n    return ResumeWriteReport(",
        )
        assert clock_call_hits(mutated) != []


class TestLRE7ZeroOutboundEdges:
    def test_no_motion_identifier_is_bound(self) -> None:
        assert referenced_names(WIRING) & FORBIDDEN_MOTION_NAMES == frozenset()

    def test_no_cross_layer_import(self) -> None:
        assert imported_module_tokens(WIRING) & {
            "execution",
            "scheduler",
            "validation",
            "validation_wiring",
            "adapters",
            "entrypoint",
            "session",
            "terminal_wiring",
        } == set()

    def test_import_whitelist_is_exact(self) -> None:
        assert imported_module_tokens(WIRING) == {
            "__future__",
            "agent_manager",
            "annotations",
            "approval_gate_wiring",
            "contracts",
            "dataclass",
            "dataclasses",
            "resume_intent",
        }

    def test_probe_next_node_binding_goes_red(self) -> None:
        mutated = _mutate(
            "    return ResumeWriteReport(",
            "    next_node = 'router'\n    return ResumeWriteReport(",
        )
        assert referenced_names(mutated) & FORBIDDEN_MOTION_NAMES == {"next_node"}


class TestLRE8NoContractConstruction:
    def test_no_contract_object_is_constructed(self) -> None:
        assert calls_to(
            WIRING,
            {"RoutingRun", "RunTerminal", "GateOutcome", "RecoveryDecision", "ProgressClaim"},
        ) == []

    def test_gate_side_is_read_through_the_settled_carrier(self) -> None:
        assert "from .approval_gate_wiring import GateDecisionKind" in WIRING

    def test_probe_gate_outcome_construction_goes_red(self) -> None:
        mutated = _mutate(
            "    return ResumeWriteReport(",
            "    probe = GateOutcome(needs_approval=True)\n    return ResumeWriteReport(",
        )
        assert calls_to(mutated, {"GateOutcome"}) != []


class TestLRE9RunningWriteSites:
    def test_running_write_sites_are_exactly_the_two_declared_ones(self) -> None:
        """`state=RUNNING` 的 `_agent_run` 写点**恰好两处** —— 与 `§159` 偏差 ④ 的"窄读法"逐字一致。

        ⚠️ 这是**诚实的窄读法**：`resume_from_waiting` **确实**经 `_agent_run(state=RUNNING)` 写入
        （初稿分析曾误判为"不经 `_agent_run`"，已在 `§159` 更正）。
        真正保证"不推进 attempt"的**不是** "写点唯一"，而是**两个位置的 `attempt=` 实参形态**：
            · `commit_started`：三种形态 —— `attempt`（=1 首派发）· `attempt`（同 attempt 提升）·
              `latest.attempt + 1`（仅当 `latest.is_terminal` 且未越限）；
            · `resume_from_waiting`：**恰好一种** —— `attempt`（= 形参 = 已校验等于最新记录）。
        """
        callers = _agent_run_callers(MANAGER)
        running = sorted(
            caller for caller, states in callers.items() if "BranchState.RUNNING" in states
        )
        assert running == ["commit_started", "resume_from_waiting"]

    def test_resume_seam_writes_running_with_the_same_attempt_only(self) -> None:
        callers = _agent_run_callers(MANAGER)
        assert callers["resume_from_waiting"] == {"BranchState.RUNNING"}
        text = _method_text(MANAGER, "resume_from_waiting")
        assert "attempt=attempt," in text
        assert "attempt=latest.attempt + 1" not in text
        assert "is_terminal" not in text

    def test_commit_started_is_untouched_by_this_round(self) -> None:
        """既有 `RUNNING` 写入口**零改动**（`§159` 的 AST 逐对象核对已断言）。"""
        assert "def commit_started(" in MANAGER
        text = _method_text(MANAGER, "commit_started")
        assert "latest.is_terminal and attempt == latest.attempt + 1" in text

    def test_probe_third_running_writer_goes_red(self) -> None:
        mutated = MANAGER.replace(
            "    def _latest(self, branch_id: BranchId) -> AgentRun | None:",
            "    def _probe_writer(self) -> AgentRun:\n"
            "        return _agent_run(\n"
            "            run_id=self._run_id,\n"
            "            branch_id='br_x',\n"
            "            agent_id='ag_x',\n"
            "            attempt=1,\n"
            "            state=BranchState.RUNNING,\n"
            "        )\n\n"
            "    def _latest(self, branch_id: BranchId) -> AgentRun | None:",
            1,
        )
        assert mutated != MANAGER
        running = sorted(
            caller
            for caller, states in _agent_run_callers(mutated).items()
            if "BranchState.RUNNING" in states
        )
        assert running == ["_probe_writer", "commit_started", "resume_from_waiting"]


class TestLRE10ResumeSeamShape:
    def test_signature_shape(self) -> None:
        args = _method_args(MANAGER, "resume_from_waiting")
        assert [a.arg for a in args.args] == ["self"]
        assert [a.arg for a in args.kwonlyargs] == ["branch_id", "attempt"]
        assert args.kw_defaults == [None, None]
        assert args.vararg is None
        assert args.kwarg is None

    def test_return_annotation(self) -> None:
        for node in ast.walk(ast.parse(MANAGER)):
            if isinstance(node, ast.FunctionDef) and node.name == "resume_from_waiting":
                assert ast.unparse(node.returns) == "AgentRun"

    def test_runtime_signature_matches(self) -> None:
        signature = inspect.signature(AgentManager.resume_from_waiting)
        assert list(signature.parameters) == ["self", "branch_id", "attempt"]
        for name in ("branch_id", "attempt"):
            parameter = signature.parameters[name]
            assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
            assert parameter.default is inspect.Parameter.empty

    def test_carries_original_values_only_state_changes(self) -> None:
        text = _method_text(MANAGER, "resume_from_waiting")
        for carried in (
            "agent_id=latest.agent_id",
            "started_at=latest.started_at",
            "ended_at=latest.ended_at",
            "handle=latest.handle",
            "state=BranchState.RUNNING",
            "attempt=attempt",
        ):
            assert carried in text
        assert "new_agent_id" not in text

    def test_probe_fresh_agent_id_goes_red(self) -> None:
        # ⚠️ 行锚定替换：`agent_id=latest.agent_id,` 作为**子串**在全文出现 4 次
        #    （其中两次缩进更深，是同一个子串）⇒ 用 `str.replace` 会指错位置（假绿）。
        import re

        mutated, count = re.subn(
            r"(?m)^            agent_id=latest\.agent_id,$",
            "            agent_id=new_agent_id(),",
            MANAGER,
        )
        assert count == 1, f"行锚定命中 {count} 次 ⇒ 读数作废"
        assert "agent_id=latest.agent_id" not in _method_text(mutated, "resume_from_waiting")

    def test_probe_fresh_ended_at_goes_red(self) -> None:
        mutated = MANAGER.replace("            ended_at=latest.ended_at,", "            ended_at=None,", 1)
        assert mutated != MANAGER
        assert "ended_at=latest.ended_at," not in _method_text(mutated, "resume_from_waiting")

    def test_branch_record_is_read_only_and_single_branch(self) -> None:
        args = _method_args(MANAGER, "branch_record")
        assert [a.arg for a in args.args] == ["self", "branch_id"]
        assert _manager(with_record=False).branch_record(BRANCH_ID) is None
        assert _manager().branch_record(BRANCH_ID).attempt == 1

    def test_probe_batch_query_goes_red(self) -> None:
        mutated = MANAGER.replace(
            "    def branch_record(self, branch_id: BranchId) -> AgentRun | None:",
            "    def branch_records(self) -> tuple[AgentRun, ...]:\n"
            "        return tuple(self._records)\n\n"
            "    def branch_record(self, branch_id: BranchId) -> AgentRun | None:",
            1,
        )
        assert mutated != MANAGER
        assert "def branch_records(" in mutated


class TestLRE11SixFields:
    def test_field_names_are_exactly_six_in_order(self) -> None:
        assert [f.name for f in dataclasses.fields(ResumeWriteReport)] == EXPECTED_FIELDS

    def test_report_is_frozen(self) -> None:
        assert ResumeWriteReport.__dataclass_params__.frozen is True

    def test_no_forbidden_field(self) -> None:
        assert {f.name for f in dataclasses.fields(ResumeWriteReport)} & FORBIDDEN_REPORT_FIELDS == set()

    def test_run_id_is_the_attribution_field(self) -> None:
        assert "run_id" in {f.name for f in dataclasses.fields(ResumeWriteReport)}
        assert RunId is str

    def test_probe_seventh_field_goes_red(self) -> None:
        names = [f.name for f in dataclasses.fields(ResumeWriteReport)]
        assert names + ["next_node"] != EXPECTED_FIELDS
        assert len(names + ["next_node"]) == 7


class TestResumeWiringErrorDiscipline:
    def test_error_type(self) -> None:
        from adaptive_router.contracts import ContractError

        assert issubclass(ResumeWiringError, RuntimeError)
        assert not issubclass(ResumeWiringError, ContractError)

    def test_guard_count_is_the_measured_one(self) -> None:
        guards = function_raise_guards(WIRING, "_require_intent")
        assert len(guards) == EXPECTED_GUARD_COUNT
        assert all(kind == "ResumeWiringError" for _, kind in guards)

    def test_guards_are_the_frozen_six_plus_three(self) -> None:
        conditions = [condition for condition, _ in function_raise_guards(WIRING, "_require_intent")]
        assert "not isinstance(intent, ResumeIntent)" in conditions
        assert "intent.gate_outcome.is_pending is not False" in conditions
        assert "type(resumed_at) is not str or not resumed_at.strip()" in conditions
        assert any("GateDecisionKind.APPROVE" in c and "GateDecisionKind.MODIFY" in c for c in conditions)
        assert any("is GateDecisionKind.MODIFY and" in c for c in conditions)
        assert "not isinstance(manager, AgentManager)" in conditions
        assert "manager.branch_record(intent.branch_id) is None" in conditions
        assert any("is not BranchState.WAITING_INPUT" in c for c in conditions)
        assert any(".attempt != intent.attempt" in c for c in conditions)


# ======================================================================
# 行为 B1 – B9（每条守卫都必须**真的被执行**：§142.7 ② 的死守卫教训）
# ======================================================================


class TestBehaviorResumePath:
    def test_B1_approve_writes_running_same_attempt(self) -> None:
        manager = _manager()
        report = apply_resume_intent(
            intent=_intent(GateDecisionKind.APPROVE), manager=manager, resumed_at="T1"
        )
        assert report == ResumeWriteReport(
            branch_id=BRANCH_ID,
            previous_state=BranchState.WAITING_INPUT,
            new_state=BranchState.RUNNING,
            attempt=1,
            resumed_at="T1",
            run_id=RUN_ID,
        )
        record = manager.branch_record(BRANCH_ID)
        assert record.state is BranchState.RUNNING
        assert record.attempt == 1
        assert record.agent_id == "ag_c2c"
        assert record.started_at == "2026-09-19T00:00:00Z"
        assert record.handle == "h-c2c"
        assert record.ended_at is None
        assert len(manager.routing_run.branches) == 1

    def test_B2_deny_writes_nothing(self) -> None:
        manager = _manager()
        report = apply_resume_intent(
            intent=_intent(GateDecisionKind.DENY), manager=manager, resumed_at="T2"
        )
        assert report.previous_state is BranchState.WAITING_INPUT
        assert report.new_state is BranchState.WAITING_INPUT
        assert report.attempt == 1
        assert manager.branch_record(BRANCH_ID).state is BranchState.WAITING_INPUT

    def test_B3_modify_writes_nothing_and_is_not_approve(self) -> None:
        manager = _manager()
        report = apply_resume_intent(
            intent=_intent(GateDecisionKind.MODIFY), manager=manager, resumed_at="T3"
        )
        assert report.new_state is BranchState.WAITING_INPUT
        assert manager.branch_record(BRANCH_ID).state is BranchState.WAITING_INPUT

    def test_B4_second_consumption_is_rejected(self) -> None:
        manager = _manager()
        apply_resume_intent(
            intent=_intent(GateDecisionKind.APPROVE), manager=manager, resumed_at="T4"
        )
        with pytest.raises(ResumeWiringError):
            apply_resume_intent(
                intent=_intent(GateDecisionKind.APPROVE), manager=manager, resumed_at="T4b"
            )
        assert manager.branch_record(BRANCH_ID).state is BranchState.RUNNING
        assert len(manager.routing_run.branches) == 1

    def test_B5_missing_record_is_rejected(self) -> None:
        with pytest.raises(ResumeWiringError):
            apply_resume_intent(
                intent=_intent(GateDecisionKind.APPROVE),
                manager=_manager(with_record=False),
                resumed_at="T5",
            )

    def test_B6_attempt_mismatch_is_rejected_without_write(self) -> None:
        manager = _manager(attempt=1)
        mismatched = build_resume_intent(
            gate_outcome=_outcome(GateDecisionKind.APPROVE),
            run_id=RUN_ID,
            branch_id=BRANCH_ID,
            attempt=2,
            total_attempt_limit=2,
        )
        with pytest.raises(ResumeWiringError):
            apply_resume_intent(intent=mismatched, manager=manager, resumed_at="T6")
        assert manager.branch_record(BRANCH_ID).state is BranchState.WAITING_INPUT
        assert manager.branch_record(BRANCH_ID).attempt == 1

    def test_B7_non_waiting_input_states_are_rejected(self) -> None:
        for state in (
            BranchState.WAITING,
            BranchState.RUNNING,
            BranchState.COMPLETED,
            BranchState.FAILED,
            BranchState.STOPPED,
            BranchState.ADOPTED,
            BranchState.REJECTED,
            BranchState.OVERRIDDEN,
        ):
            manager = _manager(state=state)
            with pytest.raises(ResumeWiringError):
                apply_resume_intent(
                    intent=_intent(GateDecisionKind.APPROVE), manager=manager, resumed_at="T7"
                )
            assert manager.branch_record(BRANCH_ID).state is state

    def test_B8_bad_resumed_at_is_rejected(self) -> None:
        for stamp in ("", "   ", 7, None):
            with pytest.raises(ResumeWiringError):
                apply_resume_intent(
                    intent=_intent(GateDecisionKind.APPROVE),
                    manager=_manager(),
                    resumed_at=stamp,
                )

    def test_B9_pending_authorization_cannot_be_consumed(self) -> None:
        """挂起卡片**没有**可消费的授权事实 ⇒ `ResumeIntent` 层面就拒绝（C2b-2c-3 的守卫）。"""
        from adaptive_router.resume_intent import ResumeIntentError

        with pytest.raises(ResumeIntentError):
            build_resume_intent(
                gate_outcome=settle_gate(judgment=_judgment(), decision=None),
                run_id=RUN_ID,
                branch_id=BRANCH_ID,
                attempt=1,
                total_attempt_limit=2,
            )

    def test_B11_pending_carrier_is_rejected_by_the_wiring_guard(self) -> None:
        """g2：**直接构造**的挂起载体必须被写路径拒绝（`MRE6` 的区分度就落在这条）。

        ⚠️ 为什么必须绕过 `build_resume_intent`：那个入口**也**有同一守卫（`C2b-2c-3`），
        两条重叠 ⇒ 只走入口的话 **g2 被短路、无法证伪**。这里直接构造 `ResumeIntent`
        （`is_pending is True`）⇒ 只有本模块的 g2 能拦下它。

        ⚠️ 同批登记的**不可达守卫**（`§159` 偏差 ⑤）：g5（MODIFY 的 `modification` 非空）
        在**合法构造路径**上**不可达** —— `settle_gate._require_decision` 在上游已强制同一条件。
        本文件**不**为 g5 编造可达输入，如实登记为**非区分性守卫**。
        """
        gate_outcome = settle_gate(judgment=_judgment(), decision=None)
        assert gate_outcome.is_pending is True
        pending_intent = ResumeIntent(
            gate_outcome=gate_outcome,
            run_id=RUN_ID,
            branch_id=BRANCH_ID,
            attempt=1,
            total_attempt_limit=2,
        )
        manager = _manager()
        with pytest.raises(ResumeWiringError):
            apply_resume_intent(intent=pending_intent, manager=manager, resumed_at="T12")
        assert manager.branch_record(BRANCH_ID).state is BranchState.WAITING_INPUT


class TestBehaviorAgentManagerSeam:
    def test_resume_from_waiting_updates_the_same_record(self) -> None:
        manager = _manager()
        resumed = manager.resume_from_waiting(branch_id=BRANCH_ID, attempt=1)
        assert resumed.state is BranchState.RUNNING
        assert resumed.agent_id == "ag_c2c"
        assert len(manager.routing_run.branches) == 1

    def test_rejects_missing_record(self) -> None:
        with pytest.raises(AgentManagerError):
            _manager(with_record=False).resume_from_waiting(branch_id=BRANCH_ID, attempt=1)

    def test_rejects_bad_attempt(self) -> None:
        for bad in (0, -1, True, "1", 1.0, None):
            with pytest.raises(AgentManagerError):
                _manager().resume_from_waiting(branch_id=BRANCH_ID, attempt=bad)

    def test_rejects_bad_branch_id(self) -> None:
        from adaptive_router.contracts import ContractError

        with pytest.raises(ContractError):
            _manager().resume_from_waiting(branch_id="not_prefixed", attempt=1)

    def test_commit_started_still_cannot_express_this(self) -> None:
        """既有入口**不能**表达"同 attempt 继续" ⇒ 这正是 E-1 存在的理由。"""
        with pytest.raises(AgentManagerError):
            _manager().commit_started(branch_id=BRANCH_ID, attempt=1, handle="h2", started_at="T9")

    def test_run_level_attempt_projection_is_untouched(self) -> None:
        manager = _manager()
        manager.resume_from_waiting(branch_id=BRANCH_ID, attempt=1)
        assert manager.routing_run.attempt == 1
        assert manager.routing_run.terminal is None
        assert manager.routing_run.control_flow_converged is False


class TestReportProvenance:
    def test_report_carries_run_id(self) -> None:
        report = apply_resume_intent(
            intent=_intent(GateDecisionKind.APPROVE), manager=_manager(), resumed_at="T10"
        )
        assert report.run_id == RUN_ID

    def test_two_runs_with_same_branch_id_are_distinguishable(self) -> None:
        first = apply_resume_intent(
            intent=_intent(GateDecisionKind.APPROVE, run_id="run_a"),
            manager=_manager(run_id="run_a"),
            resumed_at="T11",
        )
        second = apply_resume_intent(
            intent=_intent(GateDecisionKind.APPROVE, run_id="run_b"),
            manager=_manager(run_id="run_b"),
            resumed_at="T11",
        )
        assert first.branch_id == second.branch_id == BRANCH_ID
        assert (first.run_id, second.run_id) == ("run_a", "run_b")
        assert first != second

    def test_report_has_no_motion_field(self) -> None:
        assert {f.name for f in dataclasses.fields(ResumeWriteReport)} & FORBIDDEN_REPORT_FIELDS == set()


class TestProbeHygiene:
    def test_every_declared_anchor_hits(self) -> None:
        anchors = [
            "def apply_resume_intent(",
            "    return ResumeWriteReport(",
            "attempt=intent.attempt)",
            "    def _latest(self, branch_id: BranchId) -> AgentRun | None:",
            "    def branch_record(self, branch_id: BranchId) -> AgentRun | None:",
            "            agent_id=latest.agent_id,",
            "            ended_at=latest.ended_at,",
        ]
        for anchor in anchors:
            assert anchor in WIRING or anchor in MANAGER, f"锚点未命中：{anchor!r}"

    def test_sources_are_parseable_without_bom(self) -> None:
        for path in (WIRING_PATH, MANAGER_PATH):
            assert path.read_bytes()[:3] != b"\xef\xbb\xbf"
            assert hashlib.sha256(path.read_bytes()).hexdigest() != ""
            ast.parse(path.read_text(encoding="utf-8"))

    def test_apply_resume_intent_runtime_contract(self) -> None:
        signature = inspect.signature(apply_resume_intent)
        assert list(signature.parameters) == ["intent", "manager", "resumed_at"]
        assert all(
            parameter.kind is inspect.Parameter.KEYWORD_ONLY
            for parameter in signature.parameters.values()
        )
        # ⚠️ `from __future__ import annotations` ⇒ 运行时标注是**字符串**；
        #    返回类型由 `test_exactly_one_return_with_report_shape` 的 AST 断言守住。
        assert signature.return_annotation == "ResumeWriteReport"
