"""``Phase A``（`[10]` 终态写入）—— 结构锁 **LT-1 – LT-6** + **PA-7** + 行为测试 **B1 – B9**。

架构依据：`docs/architecture.md` 2 节 ``[10] Agent Manager`` 的 **Amendment A18**（FROZEN）：
A18.1（问题定义）· A18.2（唯一写入口 `close_run` + 三条禁止）· A18.3（两个收敛事实的来源）·
A18.4（四条禁止推断）· A18.5（V1 完备性声明）· A18.6（FAIL 时序）· A18.7（attempt ownership）·
A18.9（`LT-1…LT-6`）· A18.10（`INV-21` / `TEST-STATE-002`）。
范围、冻结项与基线锚点见 `tests/behavior/README_step5b.md` **§96**（Phase A Implementation Checklist）。

**本模块要守住的三条主张：**

  1. **唯一终态写入口**：`RunTerminal(` 的**构造点只有一处**（`close_run` 内），`terminal` /
     `control_flow_converged` 的**再赋值各只有一处**（同在 `close_run`）；
  2. **只写不判**：`close_run` 不推导 `terminal_state`（模块内**无** `TerminalState.<成员>` 的判定用途）、
     不读时钟、不碰 attempt lifecycle；
  3. **授权边界可机械检查（PA-7）**：既有 13 个方法中**11 个逐字节不变**，
     `__init__` / `routing_run` 的改动等于声明形式。

⚠️ **禁止子串扫描**：本文件 docstring 里**故意提及** `RunTerminal` / `control_flow_converged` /
`no_pending_*` 等词 ⇒ 所有锁一律 **AST 级**（`call_target_counts` / `attribute_chains` / 逐方法哈希）。
⚠️ 敏感性 probe 一律在**源码字符串**上完成，**不覆写** `src/` 里的真实文件；
真实源码的一次性变异证据见 README §97.5。
"""

from __future__ import annotations

import pathlib

import pytest

from adaptive_router.agent_manager import AgentManager, AgentManagerError
from adaptive_router.contracts import (
    AgentRun,
    BranchId,
    BranchState,
    ContextBundle,
    RunTerminal,
    TerminalState,
    new_branch_id,
    new_run_id,
)
from adaptive_router.dispatch_decisions import DispatchAction, DispatchDecision

from _terminal_locks import (
    annotated_attribute_targets,
    attribute_assignment_targets,
    attribute_chains,
    call_target_counts,
    clock_call_hits,
    exception_subclass_names,
    forbidden_import_hits,
    imported_module_tokens,
    imported_names,
    method_call_target_counts,
    method_source_hashes,
    method_sources,
    open_calls,
    raise_guards,
    referenced_names,
    signature_shape,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
SRC = REPO_ROOT / "src/adaptive_router"
SOURCE = (SRC / "agent_manager.py").read_text(encoding="utf-8")

#: PA-7a —— 既有 13 个方法中**允许保持不变**的 11 个（逐方法源码 SHA256，基线见 README §96.8）。
FROZEN_METHOD_HASHES = {
    "active_subagent_count": "f29b8a4b0335795f",
    "pending_admission_branch_ids": "6d722ad4e34b4b70",
    "materialize": "adb9fc81a78677ae",
    "_licence_start": "05d4ad103bdd5dbe",
    "_materialize_queue": "d72d16728554e047",
    "_drop_never_dispatched": "65b114db341c09d7",
    "commit_started": "f63fec182e9c4c5e",
    "record_outcome": "96a5f856ce864b89",
    "apply_runtime_control": "7d24416b4e7b9fbc",
    "_latest": "9c5ec87854a3c2da",
    "_replace": "99cbd5a3d16a66ee",
}

#: PA-7b —— 两个**声明允许修改**的方法：改后的形态（本文件只锁"改动后的哈希"，
#: 而"改动内容是否等于声明"由 B1/B2 的行为 + §96.3 的字段锁共同承担）。
UPDATED_METHOD_HASHES = {
    "__init__": "25c693ea12ec8dbb",
    "routing_run": "db826c0e3748808b",
    # C2c 授权新增 seam（`5B.6.28.2`）：见 README §158 的 Lock Amendment 登记
    "branch_record": "ab957ae906bb92e4",
    "resume_from_waiting": "750212ace2bdeb8b",
    # `5B.6.51.3` 授权新增 seam（start-side failure commit）：见 README §250 的 Lock Amendment 登记
    "commit_start_failure": "886fc733553886bd",
    # `5B.6.52.3` 授权新增 seam（P2 · Run 级 attempt 推进）：见 README §270 的 Lock Amendment 登记
    "commit_run_attempt": "413b47900ed29f8b",
}

#: LT-2 —— `close_run` 的精确 keyword-only 形状（四参数、全无默认值）。
EXPECTED_CLOSE_RUN_SHAPE = {
    "positional": ["self"],
    "kwonly": [
        "terminal_state",
        "no_pending_approval",
        "no_pending_recovery",
        "ended_at",
    ],
    "defaults": [False, False, False, False],
    "vararg": None,
    "kwarg": None,
}

#: LT-4 —— attempt / 记录写入的计数锚点（**基线值，必须不变**）。
#: ⚠️ `5B.6.51.3` Lock Amendment（README §250.4 · 口径同 `§158.4 ②`）：
#: `commit_start_failure` = **授权新增 seam**，它必然新增**恰一个** `self._replace` 写点（T2）
#: 与**恰一个** `self._records.append` 写点（T1）⇒ 计数锚点随授权写点移动（3 → 4 · 4 → 5），
#: ⛔ **不是**放宽守卫、⛔ **不是**为迁就旧锁而减少写点。
EXPECTED_REPLACE_CALLS = 5
EXPECTED_RECORDS_APPEND_CALLS = 4

#: LT-2 / LT-5 —— `close_run` 的**六个显式失败点**（实测；顺序即契约）：
#: 类型 → 两个 fact → ended_at → 重复收口 → C1① 分支全终止 → C1② 控制流收敛。
EXPECTED_CLOSE_RUN_GUARDS = (
    ("not isinstance(terminal_state, TerminalState)", "AgentManagerError"),
    ("type(value) is not bool", "AgentManagerError"),
    ("type(ended_at) is not str or not ended_at.strip()", "AgentManagerError"),
    ("self._terminal is not None", "AgentManagerError"),
    ("unfinished", "AgentManagerError"),
    ("not (no_pending_approval and no_pending_recovery)", "AgentManagerError"),
)


# --------------------------------------------------------------------------
# 测试替身（不修改既有测试文件，这里独立重建最小构造路径）
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


def manager(*branch_ids: BranchId, limit: int = 2) -> AgentManager:
    return AgentManager(
        run_id=new_run_id(), branch_order=tuple(branch_ids), total_attempt_limit=limit
    )


def decide(branch_id: BranchId, action: DispatchAction) -> DispatchDecision:
    return DispatchDecision(
        branch_id=branch_id, action=action, reason=f"test-{action.value}"
    )


def start_and_commit(subject: AgentManager, branch_id: BranchId) -> None:
    """走完两阶段：取 START 许可 → 用显式成功事实提交 RUNNING。"""
    subject.materialize(
        decisions=(decide(branch_id, DispatchAction.START),),
        bundles={branch_id: bundle(f"goal-{branch_id}")},
    )
    subject.commit_started(
        branch_id=branch_id,
        attempt=1,
        handle="h-1",
        started_at="2026-09-19T00:00:00Z",
    )


def terminated_branch(subject: AgentManager, branch_id: BranchId) -> None:
    """把某分支推进到**已终止**（RUNNING → COMPLETED）。"""
    start_and_commit(subject, branch_id)
    subject.record_outcome(
        branch_id=branch_id, attempt=1, state=BranchState.COMPLETED, ended_at="T-end"
    )


def close(subject: AgentManager, state: TerminalState = TerminalState.SUCCESS, **over) -> None:
    kwargs = {
        "terminal_state": state,
        "no_pending_approval": True,
        "no_pending_recovery": True,
        "ended_at": "T-run-end",
    }
    kwargs.update(over)
    subject.close_run(**kwargs)  # type: ignore[arg-type]


# ==========================================================================
# 结构锁 LT-1 – LT-6
# ==========================================================================


class TestLT1SingleTerminalWriter:
    def test_exactly_one_runtimerminal_construction_site(self) -> None:
        # AST 级：注释/docstring 里的提及**不计入**（子串计数会是 2，属假读数）
        assert call_target_counts(SOURCE)["RunTerminal"] == 1

    def test_no_other_module_constructs_a_run_terminal(self) -> None:
        offenders = sorted(
            str(path.relative_to(SRC))
            for path in SRC.rglob("*.py")
            if path.name != "agent_manager.py"
            and call_target_counts(path.read_text(encoding="utf-8")).get("RunTerminal", 0)
        )
        assert offenders == []

    def test_construction_happens_inside_close_run(self) -> None:
        # 构造点必须在 `close_run` 的**方法体内**（AST 级，不靠字符串切片）
        assert method_call_target_counts(SOURCE, "AgentManager", "close_run")[
            "RunTerminal"
        ] == 1

    def test_probe_second_construction_site_goes_red(self) -> None:
        mutated = SOURCE + (
            "\n\ndef _extra_terminal(run_id: str) -> RunTerminal:\n"
            "    return RunTerminal(run_id=run_id, state=TerminalState.SUCCESS)\n"
        )
        assert call_target_counts(mutated)["RunTerminal"] == 2


class TestLT2CloseRunIsFullyExplicit:
    def test_signature(self) -> None:
        assert (
            signature_shape(SOURCE, "AgentManager", "close_run")
            == EXPECTED_CLOSE_RUN_SHAPE
        )

    def test_probe_default_value_goes_red(self) -> None:
        mutated = SOURCE.replace(
            "        no_pending_recovery: bool,\n        ended_at: str,\n    ) -> None:\n"
            '        """Run 级收口',
            "        no_pending_recovery: bool = True,\n        ended_at: str,\n"
            "    ) -> None:\n"
            '        """Run 级收口',
        )
        assert mutated != SOURCE
        assert (
            signature_shape(mutated, "AgentManager", "close_run")
            != EXPECTED_CLOSE_RUN_SHAPE
        )

    @pytest.mark.parametrize(
        "banned", ["success", "ok", "all_branch_done", "default"]
    )
    def test_probe_boolean_shortcut_signature_goes_red(self, banned: str) -> None:
        mutated = SOURCE.replace(
            "        terminal_state: TerminalState,",
            f"        {banned}: bool,",
        )
        assert mutated != SOURCE
        assert (
            signature_shape(mutated, "AgentManager", "close_run")
            != EXPECTED_CLOSE_RUN_SHAPE
        )

    def test_six_explicit_failure_points_are_frozen(self) -> None:
        """`close_run` 的六个显式失败点（含 **C1 的两条**）—— 结构锁，不只靠行为测试。

        ⚠️ 这条是本轮在 mutation 计划评审时**就地补强**的（并入 LT-2 / LT-5，**未新增编号**）：
        没有它，"删掉 C1② 守卫"这类静默降级**只有行为测试**能抓到。
        """
        assert (
            raise_guards(SOURCE, "AgentManager", "close_run") == EXPECTED_CLOSE_RUN_GUARDS
        )

    def test_probe_dropped_convergence_guard_goes_red(self) -> None:
        mutated = SOURCE.replace(
            "        if not (no_pending_approval and no_pending_recovery):\n"
            "            raise AgentManagerError(\n"
            '                "控制流尚未收敛，不得进入 Run 终态"\n'
            "                f\"（no_pending_approval={no_pending_approval} · \"\n"
            "                f\"no_pending_recovery={no_pending_recovery} —— "
            '4.5 条件② / A18.2）"\n'
            "            )\n",
            "",
        )
        assert mutated != SOURCE
        assert (
            raise_guards(mutated, "AgentManager", "close_run")
            != EXPECTED_CLOSE_RUN_GUARDS
        )


class TestLT3NoClockAndNoStorage:
    def test_no_clock_calls(self) -> None:
        assert clock_call_hits(SOURCE) == []

    def test_ended_at_is_not_stored(self) -> None:
        # `ended_at` 只被校验，**没有**任何赋值目标（Run 级契约无时间字段 —— §95.2 的候选）
        targets = attribute_assignment_targets(SOURCE) + annotated_attribute_targets(SOURCE)
        assert [t for t in targets if "ended_at" in t] == []

    def test_module_does_not_import_time(self) -> None:
        assert {"time", "datetime", "os"} & set(imported_names(SOURCE)) == set()

    def test_probe_clock_use_goes_red(self) -> None:
        mutated = SOURCE.replace(
            "        self._terminal = RunTerminal(run_id=self._run_id, state=terminal_state)",
            "        import datetime\n"
            "        ended_at = datetime.datetime.now().isoformat()\n"
            "        self._terminal = RunTerminal("
            "run_id=self._run_id, state=terminal_state)",
        )
        assert mutated != SOURCE
        assert clock_call_hits(mutated) != []


class TestLT4AttemptOrderingUntouched:
    def test_attempt_write_counts_unchanged(self) -> None:
        counts = call_target_counts(SOURCE)
        assert counts["self._replace"] == EXPECTED_REPLACE_CALLS
        assert counts["self._records.append"] == EXPECTED_RECORDS_APPEND_CALLS

    def test_close_run_does_not_touch_records(self) -> None:
        # close_run 的方法体里不得出现记录写入（Phase A 不碰 attempt lifecycle）
        counts = method_call_target_counts(SOURCE, "AgentManager", "close_run")
        assert counts.get("self._replace", 0) == 0
        assert counts.get("self._records.append", 0) == 0
        assert counts.get("self._records.pop", 0) == 0

    def test_probe_attempt_write_inside_close_run_goes_red(self) -> None:
        mutated = SOURCE.replace(
            "        self._terminal = RunTerminal(run_id=self._run_id, state=terminal_state)",
            "        self._records.append(self._records[0])\n"
            "        self._terminal = RunTerminal("
            "run_id=self._run_id, state=terminal_state)",
        )
        assert mutated != SOURCE
        assert call_target_counts(mutated)["self._records.append"] == 5


class TestLT5NoPendingAfterTerminal:
    def test_single_write_points(self) -> None:
        # 声明在 __init__（AnnAssign），**再赋值只在 close_run**（Assign）⇒ 各 1 处
        assert attribute_assignment_targets(SOURCE).count("self._terminal") == 1
        assert attribute_assignment_targets(SOURCE).count("self._control_flow_converged") == 1
        assert annotated_attribute_targets(SOURCE).count("self._terminal") == 1
        assert annotated_attribute_targets(SOURCE).count("self._control_flow_converged") == 1

    def test_probe_second_write_point_goes_red(self) -> None:
        mutated = SOURCE.replace(
            "    def _latest(self, branch_id: BranchId) -> AgentRun | None:",
            "    def _reopen(self) -> None:\n"
            "        self._terminal = None\n\n"
            "    def _latest(self, branch_id: BranchId) -> AgentRun | None:",
        )
        assert mutated != SOURCE
        assert attribute_assignment_targets(mutated).count("self._terminal") == 2


class TestLT6NoSelfComputedTerminalState:
    def test_no_terminal_state_member_is_named(self) -> None:
        # "状态 → 终态"的任何映射/条件都必须**点名某个 TerminalState 成员** ⇒ 这里必须为空
        members = [
            chain for chain in attribute_chains(SOURCE) if chain.startswith("TerminalState.")
        ]
        assert members == []

    def test_close_run_only_type_checks_the_enum(self) -> None:
        # close_run 内对 TerminalState 的唯一用法 = `isinstance` 类型校验（正证据）
        counts = method_call_target_counts(SOURCE, "AgentManager", "close_run")
        assert counts.get("isinstance", 0) >= 1
        method_text = method_sources(SOURCE, "AgentManager")["close_run"]
        assert "TerminalState." not in method_text
        assert "isinstance(terminal_state, TerminalState)" in method_text

    def test_probe_self_computed_state_goes_red(self) -> None:
        mutated = SOURCE.replace(
            "        self._control_flow_converged = no_pending_approval and no_pending_recovery",
            "        terminal_state = (\n"
            "            TerminalState.SUCCESS\n"
            "            if all(r.is_terminal for r in self._records)\n"
            "            else TerminalState.TERMINAL_FAILURE\n"
            "        )\n"
            "        self._control_flow_converged = "
            "no_pending_approval and no_pending_recovery",
        )
        assert mutated != SOURCE
        assert [
            c for c in attribute_chains(mutated) if c.startswith("TerminalState.")
        ] != []


class TestPA7FrozenRegionsUnchanged:
    @pytest.mark.parametrize("name", sorted(FROZEN_METHOD_HASHES))
    def test_frozen_methods_byte_identical(self, name: str) -> None:
        hashes = method_source_hashes(SOURCE, "AgentManager")
        assert hashes[name] == FROZEN_METHOD_HASHES[name], (
            f"{name} 属**冻结区域**（A13/A18 已冻结的实现），Phase A 授权边界**不得修改**；"
            "确需修改必须先在架构层裁定 + 更新本表 + 记录"
        )

    @pytest.mark.parametrize("name", sorted(UPDATED_METHOD_HASHES))
    def test_declared_updates_match(self, name: str) -> None:
        assert method_source_hashes(SOURCE, "AgentManager")[name] == UPDATED_METHOD_HASHES[name]

    def test_declared_updates_are_the_declared_ones(self) -> None:
        # 把"声明的改动"变成可读断言（哈希不是黑箱）
        methods = method_source_hashes(SOURCE, "AgentManager")
        assert set(methods) == set(FROZEN_METHOD_HASHES) | set(UPDATED_METHOD_HASHES) | {
            "close_run"
        }
        init_body = SOURCE.split("    def __init__(")[1].split("\n    @property")[0]
        assert "self._terminal: RunTerminal | None = None" in init_body
        assert "self._control_flow_converged: bool = False" in init_body
        routing = SOURCE.split("    def routing_run(")[1].split("\n    @property")[0]
        assert "terminal=self._terminal" in routing
        assert "control_flow_converged=self._control_flow_converged" in routing

    def test_probe_frozen_region_change_goes_red(self) -> None:
        mutated = SOURCE.replace(
            "        remaining_slots = _available_slots",  # 属 materialize（未改）
            "        remaining_slots = _available_slots",
        )
        # 直接改一个冻结方法的最小语义：`record_outcome` 的既有终态集合
        mutated = SOURCE.replace(
            "        if state not in _OUTCOME_STATES:",
            "        if state not in _OUTCOME_STATES and False:",
        )
        assert mutated != SOURCE
        assert (
            method_source_hashes(mutated, "AgentManager")["record_outcome"]
            != FROZEN_METHOD_HASHES["record_outcome"]
        )

    def test_no_new_exception_type_and_imports_unchanged_plus_two(self) -> None:
        assert exception_subclass_names(SOURCE) == ("AgentManagerError",)
        assert forbidden_import_hits(
            SOURCE, ("adapters", "execution", "scheduler", "validation", "recovery")
        ) == set()
        tokens = set(imported_module_tokens(SOURCE))
        # 新增的两个类型来自 contracts（**没有**新增模块依赖）
        assert {"RunTerminal", "TerminalState"} <= set(imported_names(SOURCE))
        assert tokens <= {
            "__future__",
            "annotations",
            "typing",
            "Mapping",
            "agent_start_requests",
            "contracts",
            "common",
            "contracts.common",
            "dispatch_decisions",
            "runtime_control_facts",
        }


# ==========================================================================
# 行为测试 B1 – B9（TEST-STATE-002 的四项在此落地）
# ==========================================================================


class TestB1TerminalComesFromCloseRun:
    """TEST-STATE-002 ①：terminal 必须来自 `close_run`。"""

    def test_before_close_there_is_no_terminal(self) -> None:
        subject = manager()
        assert subject.routing_run.terminal is None
        assert subject.routing_run.control_flow_converged is False
        assert subject.routing_run.is_converged is False

    def test_after_close_terminal_is_the_given_state(self) -> None:
        subject = manager()
        close(subject, TerminalState.USER_CANCELLED)
        run = subject.routing_run
        assert isinstance(run.terminal, RunTerminal)
        assert run.terminal.state is TerminalState.USER_CANCELLED
        assert run.terminal.run_id == run.run_id
        assert run.control_flow_converged is True
        assert run.is_converged is True


class TestB2ConvergenceFactsAreRequired:
    """TEST-STATE-002 ②：缺任一 fact ⇒ 不得写 terminal。"""

    @pytest.mark.parametrize(
        ("approval", "recovery"),
        [(True, False), (False, True), (False, False)],
    )
    def test_missing_fact_refused(self, approval: bool, recovery: bool) -> None:
        subject = manager()
        with pytest.raises(AgentManagerError) as excinfo:
            close(
                subject,
                no_pending_approval=approval,
                no_pending_recovery=recovery,
            )
        assert "4.5 条件②" in str(excinfo.value)
        assert subject.routing_run.terminal is None

    def test_both_facts_allow_the_write(self) -> None:
        subject = manager()
        close(subject)
        assert subject.routing_run.control_flow_converged is True


class TestB3UnfinishedBranchRefused:
    """TEST-STATE-002 ③：attempt / 分支顺序不可越级。"""

    def test_unfinished_branch_refused(self) -> None:
        branch_id = new_branch_id()
        subject = manager(branch_id)
        start_and_commit(subject, branch_id)  # RUNNING（未终止）
        with pytest.raises(AgentManagerError) as excinfo:
            close(subject)
        assert "4.5 条件①" in str(excinfo.value)
        assert subject.routing_run.terminal is None

    def test_terminated_branch_allows_the_write(self) -> None:
        branch_id = new_branch_id()
        subject = manager(branch_id)
        terminated_branch(subject, branch_id)
        close(subject)
        run = subject.routing_run
        assert run.terminal is not None and run.is_converged is True
        assert len(run.branches) == 1 and run.branches[0].state is BranchState.COMPLETED

    def test_zero_branch_run_is_legal(self) -> None:
        # C3：`DIRECT` / `STAGED`（0 child）⇒ 条件①真空成立
        subject = manager()
        close(subject)
        assert subject.routing_run.terminal is not None


class TestB4NoPendingAfterTerminal:
    """TEST-STATE-002 ④：terminal 之后不允许存在 pending（含重复收口）。"""

    def test_double_close_refused(self) -> None:
        subject = manager()
        close(subject)
        with pytest.raises(AgentManagerError) as excinfo:
            close(subject)
        assert "不得再次 close_run" in str(excinfo.value)

    def test_terminal_state_is_not_overwritten(self) -> None:
        subject = manager()
        close(subject, TerminalState.SUCCESS)
        with pytest.raises(AgentManagerError):
            close(subject, TerminalState.TERMINAL_FAILURE)
        assert subject.routing_run.terminal.state is TerminalState.SUCCESS


class TestB5InputStrictness:
    @pytest.mark.parametrize("bad", ["success", 1, None, BranchState.COMPLETED])
    def test_terminal_state_must_be_enum(self, bad: object) -> None:
        subject = manager()
        with pytest.raises(AgentManagerError):
            close(subject, bad)  # type: ignore[arg-type]

    @pytest.mark.parametrize("bad", [1, 0, None, "true"])
    @pytest.mark.parametrize("field", ["no_pending_approval", "no_pending_recovery"])
    def test_facts_must_be_strict_bool(self, field: str, bad: object) -> None:
        subject = manager()
        with pytest.raises(AgentManagerError):
            close(subject, **{field: bad})

    @pytest.mark.parametrize("bad", ["", "   ", 1, None])
    def test_ended_at_must_be_non_empty_str(self, bad: object) -> None:
        subject = manager()
        with pytest.raises(AgentManagerError):
            close(subject, ended_at=bad)


class TestB6StateUnchangedByPhaseA:
    def test_attempt_stays_one(self) -> None:
        subject = manager()
        close(subject)
        assert subject.routing_run.attempt == 1
        assert subject.routing_run.total_attempt_limit == 2

    def test_routing_run_is_still_an_immutable_snapshot(self) -> None:
        subject = manager()
        before = subject.routing_run
        close(subject)
        after = subject.routing_run
        assert before is not after and before.terminal is None
        assert after.terminal is not None

    def test_branch_level_writes_still_work(self) -> None:
        branch_id = new_branch_id()
        subject = manager(branch_id)
        terminated_branch(subject, branch_id)
        assert subject.active_subagent_count == 0


class TestB7NoClockNoStoredTimestamp:
    def test_run_has_no_timestamp_field(self) -> None:
        subject = manager()
        close(subject)
        run = subject.routing_run
        assert not hasattr(run, "ended_at")
        assert tuple(run.__dataclass_fields__) == (
            "run_id",
            "branches",
            "terminal",
            "attempt",
            "total_attempt_limit",
            "control_flow_converged",
        )


class TestB8TerminalIsAFactNotADecision:
    def test_close_run_returns_none(self) -> None:
        subject = manager()
        assert close(subject) is None

    def test_no_state_to_terminal_mapping_exists(self) -> None:
        # 六值中任意一个都能被写入（`[10]` 不做取舍）；真正的取舍在调用方
        for state in TerminalState:
            subject = manager()
            close(subject, state)
            assert subject.routing_run.terminal.state is state


class TestB9AgentRunContractUntouched:
    def test_agent_run_fields_unchanged(self) -> None:
        assert tuple(AgentRun.__dataclass_fields__) == (
            "run_id",
            "branch_id",
            "agent_id",
            "attempt",
            "state",
            "started_at",
            "ended_at",
            "handle",
        )

    def test_run_terminal_fields_unchanged(self) -> None:
        assert tuple(RunTerminal.__dataclass_fields__) == ("run_id", "state")


class TestImportsAndModuleSurface:
    def test_module_level_containers_are_the_known_two(self) -> None:
        names = sorted(
            name
            for name in referenced_names(SOURCE)
            if name.startswith("_") and name.isupper()
        )
        assert names == ["_LIVE_STATES", "_OUTCOME_STATES", "_STOPPABLE_STATES"]

    def test_no_io(self) -> None:
        assert open_calls(SOURCE) == []
