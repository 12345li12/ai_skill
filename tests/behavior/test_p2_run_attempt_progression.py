"""`[10]` **Run 级 attempt 推进**（`commit_run_attempt`）+ **`E1` scalar ingress** ——
`LP2-1…10` 结构锁 + 行为锁 + `MP2-1…9` probe（`5B.6.52.3-impl`）。

架构依据：`docs/architecture.md` 2 节 ``[10] Agent Manager``（**Amendment A18**，FROZEN）+ ``[13]``/``[14]``。
范围、冻结骨架、锁编号与落点见 `tests/behavior/README_step5b.md`
**`§265`**（delta scope）· **`§266`**（授权申请）· **`§267`**（trigger-owner）· **`§268`**（ingress scope）
· **`§269`**（组合写集 / 闭包 / `E1` 冻结）· **`§270`**（实现记录与实测读数）。

**本模块要守住的主张（逐条可判）：**

  1. **唯一写点**：`commit_run_attempt` 是 `self._run_attempt` 的**唯一**推进点（`__init__` 只做声明）；
     repo-wide `src/` production 调用**恰 1**，owner = `execution.py::ExecutionCompositor.run_pass`；
  2. **触发 / 写入分离**：`[10]` ⛔ 不 import / 不引用恢复 DTO、⛔ 不自判 `should_recover`、⛔ 不读预算；
  3. **payload 零继承**：新入口体内零 `_records` / `_replace` / `AgentRun` 变更；
  4. **条件① 仍按全部 history**（`unfinished` 的源码形态**逐字**锁定）；
  5. **单调 +1**：四条守卫**顺序**与**源码形态**逐字锁定；
  6. **`E1` ingress**：`continuation_source` 为 **required** kw-only（⛔ 无默认值）· 原样透传 ·
     `run_pass` 的 6 项 kwonly **逐字不变** · 消费时序在 **stop 短路之后 / selection 之前**。

⚠️ **禁止子串扫描**：本文件一律 **AST 级**（检测器见 `_p2_run_attempt_locks.py`）。
⚠️ 敏感性 probe 一律在**源码字符串**上完成，⛔ **不覆写** ``src/`` 里的真实文件；
   每条 probe 的锚点先做 ``count == 1`` 预检（⛔ 不靠「替换后字符串变了」这一条弱判据）。
"""

from __future__ import annotations

import ast
import dataclasses
import pathlib

import pytest

from adaptive_router.agent_manager import AgentManager, AgentManagerError
from adaptive_router.composition_root import CompositionRootError
from adaptive_router.contracts import (
    AgentRun,
    BranchState,
    ContextBundle,
    RoutingRun,
    TerminalState,
    new_branch_id,
    new_agent_id,
    new_run_id,
)
from adaptive_router.dispatch_decisions import DispatchAction, DispatchDecision
from adaptive_router.execution import ExecutionCompositor
from adaptive_router.runtime_control_facts import RuntimeControlFacts
from adaptive_router.scheduler import Scheduler

import test_agent_manager
import test_composition_root
import test_execution
import test_host_wiring
import test_terminal

from _p2_run_attempt_locks import (
    annotated_attribute_targets,
    attribute_assignment_targets,
    calls_to,
    class_method_node,
    function_node,
    imported_module_tokens,
    manager_data_attribute_reads,
    manager_self_attribute_reads,
    method_call_target_counts,
    method_source_hashes,
    method_sources,
    referenced_names,
    signature_shape,
)
from test_execution import (
    ControlRecorder,
    Harness,
    capability,
    context_bundle,
    no_result,
    routing_plan,
    unknown_facts,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
SRC = REPO_ROOT / "src/adaptive_router"
AM_PATH = SRC / "agent_manager.py"
EX_PATH = SRC / "execution.py"
CR_PATH = SRC / "composition_root.py"
HW_PATH = SRC / "host_wiring.py"
AM_SOURCE = AM_PATH.read_text(encoding="utf-8")
EX_SOURCE = EX_PATH.read_text(encoding="utf-8")
CR_SOURCE = CR_PATH.read_text(encoding="utf-8")
HW_SOURCE = HW_PATH.read_text(encoding="utf-8")

#: 本单元冻结的新入口名（`§266` 七项体例 ①）。
NEW_ENTRY = "commit_run_attempt"
#: `LP2-1` / `LP2-10` 冻结的**唯一** production owner。
OWNER = ("execution.py", "ExecutionCompositor", "run_pass")
#: `LP2-10` 明令"零调用 / 零 mutation"的五个模块（⛔ 连调用都不许）。
NON_OWNER_MODULES = (
    "execution_recovery_callback.py",
    "recovery_wiring.py",
    "pass_driver.py",
    "completion_wiring.py",
    "terminal_wiring.py",
)
#: `LP2-6` —— `routing_run` 推进必须是**内部计数**，⛔ 不是字面量。
EXPECTED_ATTEMPT_EXPRESSION = "self._run_attempt"
#: `LP2-4` —— 4.5 条件① 的**逐字**形态（⛔ 不得改成"只看最新 attempt"）。
EXPECTED_CONDITION_ONE = (
    "[record.branch_id for record in self._records if not record.is_terminal]"
)
#: `LP2-6` / `§266` —— 四条守卫**顺序即契约**（逐个 `ast.unparse(test)`）。
EXPECTED_GUARDS = (
    "type(attempt) is not int or attempt < 1",
    "self._terminal is not None",
    "unfinished",
    "attempt != self._run_attempt + 1",
)
#: `LP2-9` —— `RoutingRun` 的冻结字段（⛔ 零新 DTO / 零新字段）。
EXPECTED_ROUTING_RUN_FIELDS = (
    "run_id",
    "branches",
    "terminal",
    "attempt",
    "total_attempt_limit",
    "control_flow_converged",
)
#: `LP2-9` —— `[10]` 的公开面（⛔ 零新增，除**恰一个** state command）。
EXPECTED_PUBLIC_METHODS = (
    "active_subagent_count",
    "pending_admission_branch_ids",
    "materialize",
    "commit_started",
    "record_outcome",
    "apply_runtime_control",
    "routing_run",
    "branch_record",
    "resume_from_waiting",
    "commit_start_failure",
    "commit_run_attempt",
    "close_run",
)
#: `[14]` 侧**不得**出现的恢复 DTO / 自判符号（`LP2-2`）。
FORBIDDEN_RECOVERY_SYMBOLS = ("RecoveryOutcome", "RecoveryDecision", "should_recover")
#: `LP2-3` —— 新入口体内**允许**的被调用者（`type(...)` 是形状守卫的既有 idiom）。
EXPECTED_ENTRY_CALLEES = {"AgentManagerError", "type"}
#: ⚠️ probe 锚点必须**全局唯一**：`agent_manager.py` 的 `close_run` 里有两条**同形**语句
#: （`unfinished = […]` 与 `if self._terminal is not None:`）⇒ 锚点必须带上**唯一的报错文案**。
_ANCHOR_UNFINISHED_BLOCK = '''        unfinished = [record.branch_id for record in self._records if not record.is_terminal]
        if unfinished:
            raise AgentManagerError(
                f"分支尚未全部终止，不得推进 Run 级 attempt（未终止：{unfinished}）（4.5 条件①）"
            )
'''
_ANCHOR_TERMINAL_GUARD = '''        if self._terminal is not None:
            raise AgentManagerError(
                f"本 Run 已收口为 {self._terminal.state.value}，不得再推进 attempt"
'''


# --------------------------------------------------------------------------
# P2 专用 AST 提取（**不重实现任何既有检测器**；只在共用检测器给的节点上取值）
# --------------------------------------------------------------------------


def _method_body(source: str, class_name: str, method_name: str) -> ast.FunctionDef:
    return class_method_node(source, class_name, method_name)


def _assign_expression(body: ast.AST, name: str) -> str:
    """方法体内 ``name = <expr>`` 的 ``<expr>`` 源码形态。"""
    for node in ast.walk(body):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    return ast.unparse(node.value)
    raise AssertionError(f"未找到赋值 {name} = …")


def _guard_tests(body: ast.AST) -> tuple[str, ...]:
    """方法体内**按出现顺序**的 ``if <test>:`` 源码形态。"""
    return tuple(ast.unparse(node.test) for node in ast.walk(body) if isinstance(node, ast.If))


def _attribute_writes(body: ast.AST, target: str) -> tuple[str, ...]:
    """方法体内 ``<target> = …``（``Assign``）的赋值点源码形态。"""
    return tuple(
        ast.unparse(t)
        for node in ast.walk(body)
        if isinstance(node, ast.Assign)
        for t in node.targets
        if isinstance(t, ast.Attribute) and ast.unparse(t) == target
    )


def _annotated_writes(body: ast.AST, target: str) -> tuple[str, ...]:
    """方法体内 ``<target>: T = …``（``AnnAssign``）的声明点源码形态。"""
    return tuple(
        ast.unparse(node.target)
        for node in ast.walk(body)
        if isinstance(node, ast.AnnAssign) and ast.unparse(node.target) == target
    )


def _keyword_expressions(body: ast.AST, call_name: str) -> dict[str, str]:
    """方法体内 ``call_name(...)`` 的 ``{kwarg: 源码形态}``。"""
    for node in ast.walk(body):
        if isinstance(node, ast.Call) and ast.unparse(node.func) == call_name:
            return {kw.arg: ast.unparse(kw.value) for kw in node.keywords if kw.arg}
    raise AssertionError(f"未找到调用 {call_name}(…)")


def _mutate(source: str, anchor: str, replacement: str) -> str:
    """probe 用：锚点 ``count == 1`` 预检 ⇒ 替换 ⇒ ``ast.parse`` 复核。"""
    assert source.count(anchor) == 1, f"探针锚点 count = {source.count(anchor)}：{anchor!r}"
    mutated = source.replace(anchor, replacement, 1)
    assert mutated != source
    ast.parse(mutated)
    return mutated


def _repo_sources() -> dict[str, str]:
    return {
        str(path.relative_to(SRC)).replace("\\", "/"): path.read_text(encoding="utf-8")
        for path in sorted(SRC.rglob("*.py"))
    }


def _module_referenced_names(source: str) -> set[str]:
    return set(referenced_names(source))


# --------------------------------------------------------------------------
# 测试替身（沿用 `test_terminal.py` 的最小构造路径）
# --------------------------------------------------------------------------


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


def manager(*branch_ids: str, limit: int = 2) -> AgentManager:
    return AgentManager(
        run_id=new_run_id(), branch_order=tuple(branch_ids), total_attempt_limit=limit
    )


def decide(branch_id: str, action: DispatchAction) -> DispatchDecision:
    return DispatchDecision(
        branch_id=branch_id, action=action, reason=f"test-{action.value}"
    )


def start_and_commit(subject: AgentManager, branch_id: str, attempt: int = 1) -> None:
    subject.materialize(
        decisions=(decide(branch_id, DispatchAction.START),),
        bundles={branch_id: bundle(f"goal-{branch_id}")},
    )
    subject.commit_started(
        branch_id=branch_id,
        attempt=attempt,
        handle=f"h-{attempt}",
        started_at="2026-09-19T00:00:00Z",
    )


def terminated_branch(subject: AgentManager, branch_id: str, attempt: int = 1) -> None:
    start_and_commit(subject, branch_id, attempt=attempt)
    subject.record_outcome(
        branch_id=branch_id,
        attempt=attempt,
        state=BranchState.COMPLETED,
        ended_at="T-end",
    )


def sealed(*branch_ids: str, limit: int = 2) -> AgentManager:
    """一个有 N 个**已终止**分支的 manager（⇒ 条件① 满足）。"""
    subject = manager(*branch_ids, limit=limit)
    for branch_id in branch_ids:
        terminated_branch(subject, branch_id)
    return subject


# ==========================================================================
# `LP2-1` —— 唯一写点恰 1（repo-wide）· owner = `[10]`
# ==========================================================================


class TestLP2_1SingleWriter:
    def test_the_only_advancing_write_is_the_new_entry(self) -> None:
        """`__init__` 只做**声明**（`AnnAssign`）；唯一**赋值推进**点在新入口内。"""
        init = _method_body(AM_SOURCE, "AgentManager", "__init__")
        assert _annotated_writes(init, "self._run_attempt") == ("self._run_attempt",)
        assert _attribute_writes(init, "self._run_attempt") == ()

        entry = _method_body(AM_SOURCE, "AgentManager", NEW_ENTRY)
        assert _attribute_writes(entry, "self._run_attempt") == ("self._run_attempt",)

    def test_no_other_agent_manager_method_assigns_the_counter(self) -> None:
        """模块内**除 `__init__` 与新入口**之外，零 `self._run_attempt` 赋值点。"""
        offenders: list[str] = []
        for node in ast.walk(ast.parse(AM_SOURCE)):
            if not isinstance(node, ast.ClassDef) or node.name != "AgentManager":
                continue
            for item in node.body:
                if not isinstance(item, ast.FunctionDef):
                    continue
                if item.name in ("__init__", NEW_ENTRY):
                    continue
                if _attribute_writes(item, "self._run_attempt") or _annotated_writes(
                    item, "self._run_attempt"
                ):
                    offenders.append(item.name)
        assert offenders == []

    def test_repo_wide_no_module_outside_agent_manager_writes_the_counter(self) -> None:
        offenders: list[str] = []
        for rel, source in _repo_sources().items():
            if rel == "agent_manager.py":
                continue
            hits = [
                t
                for t in attribute_assignment_targets(source) + annotated_attribute_targets(source)
                if t == "self._run_attempt"
            ]
            if hits:
                offenders.append(f"{rel}: {hits}")
        assert offenders == [], offenders


# ==========================================================================
# `LP2-2` —— `[10]` 零恢复 DTO 引用 · import token 集不变
# ==========================================================================


class TestLP2_2NoRecoveryDtoCoupling:
    def test_no_recovery_symbol_is_referenced(self) -> None:
        hits = sorted(_module_referenced_names(AM_SOURCE) & set(FORBIDDEN_RECOVERY_SYMBOLS))
        assert hits == [], hits

    def test_new_entry_body_is_free_of_recovery_symbols(self) -> None:
        body = ast.unparse(_method_body(AM_SOURCE, "AgentManager", NEW_ENTRY))
        hits = sorted(set(referenced_names(body)) & set(FORBIDDEN_RECOVERY_SYMBOLS))
        assert hits == [], hits

    def test_import_token_set_is_the_frozen_one(self) -> None:
        assert imported_module_tokens(AM_SOURCE) == test_agent_manager.FROZEN_IMPORT_TOKENS

    def test_data_and_self_read_allowlists_are_the_authoritative_ones(self) -> None:
        assert (
            manager_data_attribute_reads(
                AM_SOURCE, enum_bases=test_agent_manager.ENUM_BASES
            )
            == test_agent_manager.ALLOWED_ATTRIBUTE_READS
        )
        assert (
            manager_self_attribute_reads(AM_SOURCE) == test_agent_manager.ALLOWED_SELF_READS
        )


# ==========================================================================
# `LP2-3` —— payload 零继承
# ==========================================================================


class TestLP2_3ZeroPayloadInheritance:
    def test_new_entry_never_touches_the_record_store(self) -> None:
        counts = method_call_target_counts(AM_SOURCE, "AgentManager", NEW_ENTRY)
        assert counts.get("self._records.append", 0) == 0
        assert counts.get("self._replace", 0) == 0
        assert counts.get("AgentRun", 0) == 0
        assert counts.get("_latest_by_branch", 0) == 0

    def test_new_entry_only_calls_the_error_type(self) -> None:
        counts = method_call_target_counts(AM_SOURCE, "AgentManager", NEW_ENTRY)
        assert set(counts) <= EXPECTED_ENTRY_CALLEES, sorted(counts)

    def test_self_read_allowlist_is_exactly_seven_names(self) -> None:
        reads = manager_self_attribute_reads(AM_SOURCE)
        assert reads == test_agent_manager.ALLOWED_SELF_READS
        assert len(reads) == 7
        assert "_run_attempt" in reads


# ==========================================================================
# `LP2-4` —— 条件① 仍按**全部 history**
# ==========================================================================


class TestLP2_4ConditionOneUsesFullHistory:
    def test_condition_one_source_form_is_frozen_verbatim(self) -> None:
        body = _method_body(AM_SOURCE, "AgentManager", NEW_ENTRY)
        assert _assign_expression(body, "unfinished") == EXPECTED_CONDITION_ONE

    def test_condition_one_iterates_the_record_store_not_a_latest_projection(self) -> None:
        body = _method_body(AM_SOURCE, "AgentManager", NEW_ENTRY)
        expr = _assign_expression(body, "unfinished")
        assert "self._records" in expr
        assert "_latest_by_branch" not in expr

    def test_under_the_contract_invariant_full_history_equals_latest_per_branch(self) -> None:
        """`RoutingRun` 的顺序不变量（契约强校验）⇒ 两种读法在**合法**历史上等价。

        本测试用**真契约**构造合法历史：若有人放宽 `RoutingRun.__post_init__`，
        构造本身就会失败（⛔ 不是把等价性写成同义反复）。
        """
        run_id = new_run_id()
        first, second = new_branch_id(), new_branch_id()
        histories = (
            # 两分支都已终止（各自的 attempt 1 为 terminal）
            (
                AgentRun(
                    run_id=run_id,
                    branch_id=first,
                    agent_id=new_agent_id(),
                    attempt=1,
                    state=BranchState.COMPLETED,
                    ended_at="T",
                ),
                AgentRun(
                    run_id=run_id,
                    branch_id=second,
                    agent_id=new_agent_id(),
                    attempt=1,
                    state=BranchState.FAILED,
                    ended_at="T",
                ),
            ),
            # 一分支存在更高的非终止 attempt（低 attempt 已 terminal ⇒ 合法）
            (
                AgentRun(
                    run_id=run_id,
                    branch_id=first,
                    agent_id=new_agent_id(),
                    attempt=1,
                    state=BranchState.COMPLETED,
                    ended_at="T",
                ),
                AgentRun(
                    run_id=run_id,
                    branch_id=first,
                    agent_id=new_agent_id(),
                    attempt=2,
                    state=BranchState.RUNNING,
                    started_at="T",
                ),
            ),
        )
        for branches in histories:
            rr = RoutingRun(
                run_id=run_id,
                branches=branches,
                terminal=None,
                attempt=1,
                total_attempt_limit=2,
            )
            full = {r.branch_id for r in rr.branches if not r.is_terminal}
            latest: dict[str, AgentRun] = {}
            for record in rr.branches:
                latest[record.branch_id] = record
            per_latest = {b for b, r in latest.items() if not r.is_terminal}
            assert full == per_latest

    def test_any_unfinished_branch_blocks_progress(self) -> None:
        first, second = new_branch_id(), new_branch_id()
        subject = manager(first, second)
        terminated_branch(subject, first)
        start_and_commit(subject, second)
        with pytest.raises(AgentManagerError):
            subject.commit_run_attempt(attempt=2)


# ==========================================================================
# `LP2-5` —— budget 一致性
# ==========================================================================


class TestLP2_5BudgetConsistency:
    def test_new_entry_never_reads_the_attempt_budget(self) -> None:
        body = ast.unparse(_method_body(AM_SOURCE, "AgentManager", NEW_ENTRY))
        assert "_total_attempt_limit" not in referenced_names(body)

    def test_branch_level_budget_semantics_are_untouched(self) -> None:
        """三处分支级语义仍在（预算的**唯一**写点 / 自读面 / 投影字段）。"""
        assert (
            attribute_assignment_targets(AM_SOURCE).count("self._total_attempt_limit") == 1
        )
        assert "_total_attempt_limit" in manager_self_attribute_reads(AM_SOURCE)
        projection = _keyword_expressions(
            _method_body(AM_SOURCE, "AgentManager", "routing_run"), "RoutingRun"
        )
        assert projection["total_attempt_limit"] == "self._total_attempt_limit"

    def test_run_attempt_is_not_constrained_by_the_branch_budget(self) -> None:
        """`total_attempt_limit=2` 之下，Run attempt 仍可推进到 **3**（预算属分支级）。"""
        subject = sealed(new_branch_id(), limit=2)
        assert subject.routing_run.total_attempt_limit == 2
        subject.commit_run_attempt(attempt=2)
        subject.commit_run_attempt(attempt=3)
        assert subject.routing_run.attempt == 3

    def test_progress_never_mutates_the_budget(self) -> None:
        subject = sealed(new_branch_id(), limit=2)
        before = subject.routing_run.total_attempt_limit
        subject.commit_run_attempt(attempt=2)
        assert subject.routing_run.total_attempt_limit == before


# ==========================================================================
# `LP2-6` —— 投影自内部计数 · 推进 = 旧值 + 1
# ==========================================================================


class TestLP2_6ProjectionAndMonotonicity:
    def test_projection_reads_the_internal_counter_not_a_literal(self) -> None:
        projection = _keyword_expressions(
            _method_body(AM_SOURCE, "AgentManager", "routing_run"), "RoutingRun"
        )
        assert projection["attempt"] == EXPECTED_ATTEMPT_EXPRESSION

    def test_guard_sequence_is_frozen_verbatim_and_in_order(self) -> None:
        body = _method_body(AM_SOURCE, "AgentManager", NEW_ENTRY)
        assert _guard_tests(body) == EXPECTED_GUARDS

    def test_fresh_run_projects_attempt_one(self) -> None:
        subject = manager(new_branch_id())
        assert subject.routing_run.attempt == 1

    def test_progress_is_previous_plus_one(self) -> None:
        subject = sealed(new_branch_id())
        subject.commit_run_attempt(attempt=2)
        assert subject.routing_run.attempt == 2
        subject.commit_run_attempt(attempt=3)
        assert subject.routing_run.attempt == 3

    @pytest.mark.parametrize("attempt", [1, 3, 4, 99])
    def test_skipping_or_reverting_is_rejected(self, attempt: int) -> None:
        subject = sealed(new_branch_id())
        with pytest.raises(AgentManagerError):
            subject.commit_run_attempt(attempt=attempt)
        assert subject.routing_run.attempt == 1


# ==========================================================================
# `LP2-7` / `LP2-8` —— 方法集与冻结区域（权威表在 `test_terminal.py`）
# ==========================================================================


class TestLP2_7And8MethodSetAndFrozenRegions:
    def test_method_set_is_eighteen_and_new_entry_is_declared(self) -> None:
        methods = method_source_hashes(AM_SOURCE, "AgentManager")
        assert set(methods) == set(test_terminal.FROZEN_METHOD_HASHES) | set(
            test_terminal.UPDATED_METHOD_HASHES
        ) | {"close_run"}
        assert len(methods) == 18
        assert NEW_ENTRY in test_terminal.UPDATED_METHOD_HASHES
        assert methods[NEW_ENTRY] == test_terminal.UPDATED_METHOD_HASHES[NEW_ENTRY]

    def test_declared_updates_match_the_authoritative_table(self) -> None:
        methods = method_source_hashes(AM_SOURCE, "AgentManager")
        for name, digest in test_terminal.UPDATED_METHOD_HASHES.items():
            assert methods[name] == digest, name

    def test_close_run_never_advances_the_run_attempt(self) -> None:
        body = _method_body(AM_SOURCE, "AgentManager", "close_run")
        assert _attribute_writes(body, "self._run_attempt") == ()
        assert _annotated_writes(body, "self._run_attempt") == ()

    def test_resume_from_waiting_never_advances_the_run_attempt(self) -> None:
        body = _method_body(AM_SOURCE, "AgentManager", "resume_from_waiting")
        assert _attribute_writes(body, "self._run_attempt") == ()
        assert _annotated_writes(body, "self._run_attempt") == ()


# ==========================================================================
# `LP2-9` —— ⛔ 零新公开面 · ⛔ 零新 DTO
# ==========================================================================


class TestLP2_9NoNewPublicSurface:
    def test_public_method_surface_gained_exactly_one_state_command(self) -> None:
        public = tuple(
            sorted(
                name
                for name in method_source_hashes(AM_SOURCE, "AgentManager")
                if not name.startswith("_")
            )
        )
        assert public == tuple(sorted(EXPECTED_PUBLIC_METHODS))

    def test_module_all_is_unchanged(self) -> None:
        for node in ast.parse(AM_SOURCE).body:
            if isinstance(node, ast.Assign) and ast.unparse(node.targets[0]) == "__all__":
                assert ast.unparse(node.value) == "['AgentManager', 'AgentManagerError']"
                return
        raise AssertionError("未找到 __all__")

    def test_routing_run_contract_gained_no_field(self) -> None:
        assert (
            tuple(f.name for f in dataclasses.fields(RoutingRun)) == EXPECTED_ROUTING_RUN_FIELDS
        )

    def test_contracts_module_dto_stays_frozen(self) -> None:
        """`LP2-9`：⛔ `contracts/` 零改动（`RoutingRun` 仍是 **frozen** dataclass）。

        ⚠️ 逐字节"零改动"由既有 `test_terminal_wiring.TestLTW8ProtectedArtifacts` 的
        `contracts/runtime.py` 身份锁承担（⛔ 本文件不复制第二份哈希表）；
        这里只补**语义形态**这一半。
        """
        assert RoutingRun.__dataclass_params__.frozen is True  # type: ignore[attr-defined]


# ==========================================================================
# `LP2-10` —— repo-wide owner 恰 1 · 五个模块零调用
# ==========================================================================


class TestLP2_10RepoWideOwnerIsExactlyOne:
    def test_exactly_one_production_call_site_repo_wide(self) -> None:
        sites: list[str] = []
        for rel, source in _repo_sources().items():
            for _symbol, lineno in calls_to(source, {NEW_ENTRY}):
                sites.append(f"{rel}:{lineno}")
        assert sites == [f"{OWNER[0]}:{_owner_call_lineno()}"], sites

    def test_the_owner_call_sits_inside_run_pass(self) -> None:
        counts = method_call_target_counts(EX_SOURCE, OWNER[1], OWNER[2])
        assert counts.get("self._agent_manager.commit_run_attempt", 0) == 1
        chain = [
            ast.unparse(child.func)
            for child in ast.walk(_method_body(EX_SOURCE, OWNER[1], OWNER[2]))
            if isinstance(child, ast.Call) and ast.unparse(child.func).endswith(NEW_ENTRY)
        ]
        assert chain == ["self._agent_manager.commit_run_attempt"]

    @pytest.mark.parametrize("module", NON_OWNER_MODULES)
    def test_named_modules_do_not_even_call_it(self, module: str) -> None:
        source = (SRC / module).read_text(encoding="utf-8")
        assert calls_to(source, {NEW_ENTRY}) == []

    def test_execution_module_never_writes_the_counter_directly(self) -> None:
        hits = [
            t
            for t in attribute_assignment_targets(EX_SOURCE)
            + annotated_attribute_targets(EX_SOURCE)
            if t == "self._run_attempt"
        ]
        assert hits == []

    def test_no_module_imports_a_recovery_dto_into_the_manager(self) -> None:
        assert calls_to(AM_SOURCE, set(FORBIDDEN_RECOVERY_SYMBOLS)) == []


def _owner_call_lineno() -> int:
    for _symbol, lineno in calls_to(EX_SOURCE, {NEW_ENTRY}):
        return lineno
    raise AssertionError("owner 调用点不存在")


# ==========================================================================
# `E1` ingress —— required seam · 同一对象穿透 · `run_pass` 签名不变
# ==========================================================================


class TestE1ScalarIngress:
    def test_host_factory_declares_the_seam_as_required(self) -> None:
        node = function_node(HW_SOURCE, "build_entrypoint_handlers")
        names = [a.arg for a in node.args.kwonlyargs]
        required = len(test_host_wiring.EXPECTED_REQUIRED_KWONLY)
        assert names == list(test_host_wiring.EXPECTED_REQUIRED_KWONLY) + list(
            test_host_wiring.EXPECTED_OPTIONAL_KWONLY
        )
        assert "continuation_source" in names[:required]
        defaults = [d is not None for d in node.args.kw_defaults]
        assert defaults == [False] * required + [True] * (
            len(names) - required
        ), "seam 必须有默认值 ⇒ 生产路径可静默省略（假闭合）"

    def test_composition_root_field_is_required(self) -> None:
        fields = {f.name: f for f in dataclasses.fields(test_composition_root.CompositionRoot)}
        assert "continuation_source" in fields
        field = fields["continuation_source"]
        assert field.default is dataclasses.MISSING
        assert field.default_factory is dataclasses.MISSING  # type: ignore[misc]

    def test_compositor_init_gained_exactly_one_keyword(self) -> None:
        shape = signature_shape(EX_SOURCE, OWNER[1], "__init__")
        assert shape["kwonly"] == test_execution.EXPECTED_INIT_KWONLY
        assert "continuation_source" in shape["kwonly"]
        assert shape["defaults"] == [False] * len(shape["kwonly"])

    def test_run_pass_signature_is_verbatim_unchanged(self) -> None:
        shape = signature_shape(EX_SOURCE, OWNER[1], "run_pass")
        assert shape["kwonly"] == test_execution.EXPECTED_RUN_PASS_KWONLY
        assert shape["defaults"] == [False] * 6
        assert "continuation_source" not in shape["kwonly"]

    def test_the_same_callable_object_travels_the_whole_chain(self) -> None:
        sentinel = _SourceRecorder(None)
        root = test_composition_root.build_root(continuation_source=sentinel)
        graph = root.materialize(test_composition_root.build_routing(root))
        assert graph.compositor._continuation_source is sentinel  # noqa: SLF001

    def test_root_passes_the_seam_through_to_the_compositor(self) -> None:
        body = ast.unparse(class_method_node(CR_SOURCE, test_composition_root.CompositionRoot.__name__, "materialize"))
        assert "continuation_source=self.continuation_source" in body

    def test_composition_root_rejects_a_non_callable_seam(self) -> None:
        with pytest.raises(CompositionRootError):
            test_composition_root.build_root(continuation_source=object())

    def test_consumption_sits_after_the_stop_short_circuit_and_before_selection(self) -> None:
        body = _method_body(EX_SOURCE, OWNER[1], OWNER[2])
        commit_lines: list[int] = []
        selection_lines: list[int] = []
        return_lines: list[int] = []
        for child in ast.walk(body):
            if isinstance(child, ast.Call):
                dotted = ast.unparse(child.func)
                if dotted.endswith(NEW_ENTRY):
                    commit_lines.append(child.lineno)
                if dotted.endswith("_branches_under_admission"):
                    selection_lines.append(child.lineno)
            if isinstance(child, ast.Return):
                return_lines.append(child.lineno)
        assert len(commit_lines) == 1 and len(selection_lines) == 1
        assert min(return_lines) < commit_lines[0] < selection_lines[0], (
            return_lines,
            commit_lines,
            selection_lines,
        )

    def test_first_pass_is_an_explicit_switch_around_the_seam(self) -> None:
        body = _method_body(EX_SOURCE, OWNER[1], OWNER[2])
        tests = _guard_tests(body)
        assert "not first_pass" in tests


# --------------------------------------------------------------------------
# 可控 continuation 源（测试替身）
# --------------------------------------------------------------------------


class _SourceRecorder:
    """记录**调用次数**并返回**注入值**的 continuation 源（`LP2-12` 口径的卡尺）。"""

    def __init__(self, value: int | None = None) -> None:
        self.value = value
        self.calls = 0

    def __call__(self) -> int | None:
        self.calls += 1
        return self.value


class _P2Harness(Harness):
    """在 `test_execution.Harness` 之上把 `continuation_source` 换成可控记录器。"""

    def __init__(self, value: int | None = None) -> None:
        super().__init__()
        self.source = _SourceRecorder(value)
        self.compositor = ExecutionCompositor(
            scheduler=Scheduler(),
            agent_manager=self.manager,
            adapter=self.adapter,
            admission_facts_source=lambda _b: unknown_facts(),
            reusable_result_source=no_result,
            continuation_source=self.source,
        )


class TestE1ConsumptionCardinality:
    """消费语义：**次数** · **时机** · **原样透传**（`§269.1 ①` / `§269.1 ③`）。"""

    def test_first_pass_never_consumes_the_source(self) -> None:
        harness = _P2Harness(2)
        harness.pass_(first_pass=True)
        assert harness.source.calls == 0
        assert harness.manager.routing_run.attempt == 1

    def test_stop_short_circuit_never_consumes_the_source(self) -> None:
        harness = _P2Harness(2)
        harness.stop_all()
        assert harness.source.calls == 0
        assert harness.manager.routing_run.attempt == 1

    def test_none_means_no_progress_and_exactly_one_read(self) -> None:
        harness = _P2Harness(None)
        harness.pass_(first_pass=True)
        harness.pass_(first_pass=False)
        assert harness.source.calls == 1
        assert harness.manager.routing_run.attempt == 1

    def test_an_int_reaches_the_manager_verbatim(self) -> None:
        harness = _P2Harness(2)
        harness.pass_(first_pass=True)
        for branch_id in (harness.first, harness.second):
            harness.manager.record_outcome(
                branch_id=branch_id,
                attempt=1,
                state=BranchState.COMPLETED,
                ended_at="T-end",
            )
        harness.pass_(first_pass=False)
        assert harness.source.calls == 1
        assert harness.manager.routing_run.attempt == 2

    def test_control_source_and_continuation_source_are_both_read_once(self) -> None:
        harness = _P2Harness(None)
        harness.pass_(first_pass=True)
        harness.pass_(first_pass=False)
        assert harness.source.calls == 1
        assert harness.recorder.calls == 2


# ==========================================================================
# 行为锁 —— Run 级 attempt 推进
# ==========================================================================


class TestBehaviorRunAttemptProgression:
    @pytest.mark.parametrize("bad", [True, False, 0, -1, 2.0, "2", None])
    def test_non_int_or_non_positive_is_rejected(self, bad: object) -> None:
        subject = sealed(new_branch_id())
        with pytest.raises(AgentManagerError):
            subject.commit_run_attempt(attempt=bad)  # type: ignore[arg-type]
        assert subject.routing_run.attempt == 1

    def test_bool_is_rejected_even_though_it_is_an_int_subclass(self) -> None:
        subject = sealed(new_branch_id())
        with pytest.raises(AgentManagerError):
            subject.commit_run_attempt(attempt=True)  # type: ignore[arg-type]

    def test_progress_after_a_sealed_terminal_state_is_rejected(self) -> None:
        subject = sealed(new_branch_id())
        subject.close_run(
            terminal_state=TerminalState.SUCCESS,
            no_pending_approval=True,
            no_pending_recovery=True,
            ended_at="T-run-end",
        )
        with pytest.raises(AgentManagerError):
            subject.commit_run_attempt(attempt=2)

    def test_progress_leaves_the_record_store_untouched(self) -> None:
        first = new_branch_id()
        subject = sealed(first)
        before = subject.routing_run.branches
        subject.commit_run_attempt(attempt=2)
        assert subject.routing_run.branches == before

    def test_progress_is_visible_through_the_projection_only(self) -> None:
        subject = sealed(new_branch_id())
        subject.commit_run_attempt(attempt=2)
        assert subject.routing_run.attempt == 2
        assert subject.active_subagent_count == 0

    def test_terminal_projection_is_not_touched_by_progress(self) -> None:
        subject = sealed(new_branch_id())
        subject.commit_run_attempt(attempt=2)
        assert subject.routing_run.terminal is None
        assert subject.routing_run.control_flow_converged is False


# ==========================================================================
# `MP2-1…9` —— 敏感性 probe（**源码字符串**上完成 · ⛔ 不覆写真实文件）
# ==========================================================================


class TestMP2Probes:
    def test_MP2_1_removing_the_only_write_turns_LP2_6_red(self) -> None:
        mutated = _mutate(
            AM_SOURCE,
            "        self._run_attempt = attempt\n",
            "",
        )
        assert _attribute_writes(
            _method_body(mutated, "AgentManager", NEW_ENTRY), "self._run_attempt"
        ) == ()

    def test_MP2_2_a_foreign_direct_write_turns_LP2_1_red(self) -> None:
        foreign = EX_SOURCE + "\n\ndef _probe(manager) -> None:\n    manager._run_attempt = 2\n"
        ast.parse(foreign)
        hits = [
            t
            for t in attribute_assignment_targets(foreign)
            + annotated_attribute_targets(foreign)
            if t == "manager._run_attempt"
        ]
        assert hits == ["manager._run_attempt"]

    def test_MP2_3_accepting_a_recovery_dto_turns_LP2_2_red(self) -> None:
        mutated = _mutate(
            AM_SOURCE,
            "    def close_run(\n",
            "    def _probe(self, outcome: RecoveryOutcome) -> None:\n"
            "        return None\n\n    def close_run(\n",
        )
        assert sorted(
            _module_referenced_names(mutated) & set(FORBIDDEN_RECOVERY_SYMBOLS)
        ) == ["RecoveryOutcome"]

    def test_MP2_4_inheriting_payload_turns_LP2_3_red(self) -> None:
        mutated = _mutate(
            AM_SOURCE,
            "        self._run_attempt = attempt\n",
            "        self._records.append(self._records[-1])\n        self._run_attempt = attempt\n",
        )
        counts = method_call_target_counts(mutated, "AgentManager", NEW_ENTRY)
        assert counts["self._records.append"] == 1

    def test_MP2_5_latest_only_condition_turns_LP2_4_red(self) -> None:
        mutated = _mutate(
            AM_SOURCE,
            _ANCHOR_UNFINISHED_BLOCK,
            '''        latest: dict[str, object] = {}
        for record in self._records:
            latest[record.branch_id] = record
        unfinished = [
            r.branch_id for r in latest.values() if not r.is_terminal  # type: ignore[attr-defined]
        ]
        if unfinished:
            raise AgentManagerError(
                f"分支尚未全部终止，不得推进 Run 级 attempt（未终止：{unfinished}）（4.5 条件①）"
            )
''',
        )
        body = _method_body(mutated, "AgentManager", NEW_ENTRY)
        assert _assign_expression(body, "unfinished") != EXPECTED_CONDITION_ONE

    def test_MP2_6_budget_constraint_turns_LP2_5_red(self) -> None:
        mutated = _mutate(
            AM_SOURCE,
            _ANCHOR_TERMINAL_GUARD,
            '''        if attempt > self._total_attempt_limit:
            raise AgentManagerError("Run attempt 超出预算")
'''
            + _ANCHOR_TERMINAL_GUARD,
        )
        body = ast.unparse(_method_body(mutated, "AgentManager", NEW_ENTRY))
        assert "_total_attempt_limit" in referenced_names(body)

    def test_MP2_7_skipping_numbers_turns_LP2_6_red(self) -> None:
        mutated = _mutate(
            AM_SOURCE,
            "        if attempt != self._run_attempt + 1:",
            "        if attempt < self._run_attempt:",
        )
        assert _guard_tests(_method_body(mutated, "AgentManager", NEW_ENTRY)) != EXPECTED_GUARDS

    def test_MP2_8_self_judging_should_recover_turns_LP2_2_red(self) -> None:
        mutated = _mutate(
            AM_SOURCE,
            _ANCHOR_UNFINISHED_BLOCK,
            '''        if outcome.should_recover:
            return None
'''
            + _ANCHOR_UNFINISHED_BLOCK,
        )
        assert "should_recover" in _module_referenced_names(mutated)

    def test_MP2_9_rewriting_records_turns_LP2_3_red(self) -> None:
        mutated = _mutate(
            AM_SOURCE,
            "        self._run_attempt = attempt\n",
            "        self._replace(0, self._records[-1])\n        self._run_attempt = attempt\n",
        )
        counts = method_call_target_counts(mutated, "AgentManager", NEW_ENTRY)
        assert counts["self._replace"] == 1

    def test_the_probe_anchor_precheck_is_not_vacuous(self) -> None:
        with pytest.raises(AssertionError):
            _mutate(AM_SOURCE, "这是一个不存在的锚点", "")
