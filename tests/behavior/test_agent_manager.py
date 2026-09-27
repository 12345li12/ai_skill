"""``[10] Agent Manager`` —— 结构锁 L1–L15 + 行为锁 B1–B29（Step 5B.6.4.2-impl）。

架构依据：`docs/architecture.md` 2 节 ``[10] Agent Manager``（**Amendment A13**，FROZEN）。
范围、锁清单与 probe 计划：`tests/behavior/README_step5b.md` §44。

⚠️ 敏感性 probe 一律在**源码字符串**上完成，**不覆写** ``src/`` 里的真实文件。

⚠️ **禁止子串扫描**：``BranchState.REJECTED`` 在 ``agent_manager.py`` 的 docstring 里被
**故意提及**（说明禁令），因此 L5 只能走 **AST**（见 ``test_substring_scan_would_false_positive``）。

⚠️ **本轮的真实边界**：``WAITING_INPUT`` **没有入口** —— 批准的函数面里没有"追加 / 等待输入"
操作（那是热切换 append 路径，未在本轮范围）。因此 A13.4 的"``WAITING_INPUT`` 必须计入"
由**模块常量锁**（``test_L10_waiting_input_is_in_live_states``）承载，端到端路径见 README §45 的 deferred。
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from adaptive_router.agent_manager import AgentManager, AgentManagerError
from adaptive_router.agent_start_requests import AgentStartRequest
from adaptive_router.contracts import (
    BranchId,
    BranchState,
    ContextBundle,
    new_branch_id,
    new_run_id,
)
from adaptive_router.dispatch_decisions import DispatchAction, DispatchDecision
from adaptive_router.runtime_control_facts import RuntimeControlFacts

from _agent_manager_locks import (
    calls_to,
    class_method_node,
    clock_call_hits,
    dataclass_field_names,
    enum_member_references,
    forbidden_import_hits,
    imported_module_tokens,
    keyword_only_defaults,
    manager_data_attribute_reads,
    manager_self_attribute_reads,
    mapping_get_calls,
    module_constant_expression,
    open_calls,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
SRC = REPO_ROOT / "src/adaptive_router"
MANAGER_PATH = SRC / "agent_manager.py"
MANAGER_SOURCE = MANAGER_PATH.read_text(encoding="utf-8")
START_REQUEST_SOURCE = (SRC / "agent_start_requests.py").read_text(encoding="utf-8")
CONTROL_FACTS_SOURCE = (SRC / "runtime_control_facts.py").read_text(encoding="utf-8")

ENUM_BASES = ("BranchState", "DispatchAction")

#: L1 —— `agent_manager.py` 的**输入数据属性读取 allowlist**（实测 11 项，README §45）。
ALLOWED_ATTRIBUTE_READS = frozenset(
    {
        "branch_id",
        "attempt",
        "state",
        "agent_id",
        "started_at",
        "ended_at",
        "handle",
        "is_terminal",
        "action",
        "reason",
        "stop_branches_requested",
        "value",
    }
)

#: L2b —— 允许出现的 `self.<名字>` 私有状态（**A13 的四个** + **A18 Phase A 声明的两个**
#: + **`5B.6.52.3` P2 的一个**）。
#:
#: ⚠️ **Amendment A18 / Phase A 的登记式扩展（见 README §96.3 ③ 与 §97.7）**：
#: `close_run` 必须记住 Run 级终态与收敛位 ⇒ 新增 `_terminal` / `_control_flow_converged`
#: **两个名字**（这两个名字由 §96.3 ③ 明确冻结并经复审批准）。
#: ⚠️ 本集合仍是**精确集合断言** —— 任何**其它**新增 self 状态依旧会让 L2b 变红（不放松守卫）。
ALLOWED_SELF_READS = frozenset(
    {
        "_run_id",
        "_branch_order",
        "_total_attempt_limit",
        "_records",
        # A18 Phase A（frozen artifact extension，已授权）：
        "_terminal",
        "_control_flow_converged",
        # `5B.6.52.3`（P2 · Run 级 attempt 计数）：见 README §270 的 Lock Amendment 登记
        "_run_attempt",
    }
)

#: L8 —— 导入白名单：**对共用检测器的精确集合断言**。
#:
#: ⚠️ 这里刻意**不**自己解析 ``ImportFrom.module`` —— 那会构成第二套朴素判定
#: （``from . import X`` 的 ``module is None`` 会被绕过），
#: `test_import_detectors.TestNoReDivergence` 会判红（本轮真实发生过一次）。
#: 于是改成"冻结共用检测器报告的全部 token"：任何新增 import（模块名或符号名）
#: 都会改变这个集合 ⇒ 断言变红。集合来自实测，含 ``typing`` 的符号 ``Mapping``
#: 与 ``__future__`` 的 ``annotations``。
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

#: L8 —— 禁用清单（成本层是**观察者**；Adapter / P1 / policy 均不得 import）。
FORBIDDEN_IMPORTS = (
    "cost",
    "adapters",
    "adapter",
    "execution",
    "policy",
    "planner",
    "model_router",
    "router",
    "session",
    "entrypoint",
    "project_context",
    "scheduler",
    "pathlib",
    "os",
    "subprocess",
    "time",
    "datetime",
)

#: L7 —— `commit_started` 的精确 keyword-only 形状（4 项、全部无默认值）。
EXPECTED_COMMIT_KWONLY = {
    "branch_id": False,
    "attempt": False,
    "handle": False,
    "started_at": False,
}


def source_of(node: ast.AST) -> str:
    return ast.unparse(node)


# --------------------------------------------------------------------------
# 测试替身
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
        run_id=new_run_id(),
        branch_order=tuple(branch_ids),
        total_attempt_limit=limit,
    )


def decide(branch_id: BranchId, action: DispatchAction) -> DispatchDecision:
    return DispatchDecision(branch_id=branch_id, action=action, reason=f"test-{action.value}")


def bundles_for(*branch_ids: BranchId) -> dict[BranchId, ContextBundle]:
    return {branch_id: bundle(f"goal-{branch_id}") for branch_id in branch_ids}


def start_and_commit(
    subject: AgentManager, branch_id: BranchId, *, attempt: int = 1, handle: str = "h-1"
) -> None:
    """走完两阶段：先取 START 许可，再用**显式成功事实**提交 RUNNING。"""
    subject.materialize(
        decisions=(decide(branch_id, DispatchAction.START),),
        bundles=bundles_for(branch_id),
    )
    subject.commit_started(
        branch_id=branch_id,
        attempt=attempt,
        handle=handle,
        started_at="2026-09-18T00:00:00Z",
    )


# ==========================================================================
# 结构锁（静态 · 真实源码）
# ==========================================================================


def _assert_attribute_allowlist(source: str) -> None:
    assert manager_data_attribute_reads(source, enum_bases=ENUM_BASES) == ALLOWED_ATTRIBUTE_READS


class TestL1AttributeReads:
    def test_real_source(self) -> None:
        _assert_attribute_allowlist(MANAGER_SOURCE)

    @pytest.mark.parametrize(
        "needle, replacement",
        [
            ("latest.state.value", "latest.goal.value"),           # 读 BranchSpec 的 goal
            ("latest.is_terminal", "latest.read_only"),            # 读只读标记
            ("decision.reason", "decision.mode"),                  # 读执行模式
            ("facts.stop_branches_requested", "facts.plan.mode"),  # 穿透到 Plan
        ],
    )
    def test_probe_extra_read_goes_red(self, needle: str, replacement: str) -> None:
        mutated = MANAGER_SOURCE.replace(needle, replacement)
        assert mutated != MANAGER_SOURCE, f"探针失效：源码里找不到 {needle!r}"
        with pytest.raises(AssertionError):
            _assert_attribute_allowlist(mutated)

    def test_probe_alias_read_goes_red(self) -> None:
        """借局部变量读字段同样要被抓到（``spec = latest`` 后 ``spec.goal``）。"""
        mutated = MANAGER_SOURCE + "\n\n\ndef _peek(spec):\n    return spec.goal\n"
        with pytest.raises(AssertionError):
            _assert_attribute_allowlist(mutated)


class TestL2SingleConstructionSite:
    def test_real_source_one_site_inside_helper(self) -> None:
        sites = calls_to(MANAGER_SOURCE, ("AgentRun",))
        assert len(sites) == 1
        fn = next(
            node
            for node in ast.parse(MANAGER_SOURCE).body
            if isinstance(node, ast.FunctionDef) and node.name == "_agent_run"
        )
        assert fn.lineno <= sites[0][1] <= (fn.end_lineno or fn.lineno)

    def test_probe_second_site_goes_red(self) -> None:
        mutated = MANAGER_SOURCE + "\n\n\ndef _sneak(**kw):\n    return AgentRun(**kw)\n"
        assert len(calls_to(mutated, ("AgentRun",))) == 2

    def test_self_reads_are_only_known_state(self) -> None:
        assert manager_self_attribute_reads(MANAGER_SOURCE) == ALLOWED_SELF_READS

    def test_probe_extra_self_state_goes_red(self) -> None:
        mutated = MANAGER_SOURCE + "\n\n\ndef _peek(self):\n    return self._cache\n"
        assert manager_self_attribute_reads(mutated) != ALLOWED_SELF_READS


class TestL3L4NoRunForVetoOrStart:
    def test_real_source_L3_L4_absence(self) -> None:
        """``_licence_start`` 与 ``_drop_never_dispatched`` 体内**不得**出现 ``_agent_run``。"""
        for name in ("_licence_start", "_drop_never_dispatched"):
            body = source_of(class_method_node(MANAGER_SOURCE, "AgentManager", name))
            assert "_agent_run(" not in body, f"{name} 不得构造 AgentRun"

    def test_probe_start_constructing_run_goes_red(self) -> None:
        mutated = MANAGER_SOURCE.replace(
            "        latest = self._latest(branch_id)\n"
            "        if latest is None:\n"
            "            attempt = 1",
            "        self._records.append(_agent_run(run_id=self._run_id,"
            " branch_id=branch_id, agent_id=new_agent_id(), attempt=1,"
            " state=BranchState.WAITING))\n"
            "        latest = self._latest(branch_id)\n"
            "        if latest is None:\n"
            "            attempt = 1",
        )
        assert mutated != MANAGER_SOURCE, "探针失效：替换未命中"
        body = source_of(class_method_node(mutated, "AgentManager", "_licence_start"))
        assert "_agent_run(" in body


class TestL5NoRejectedState:
    def test_real_source_ast(self) -> None:
        assert "REJECTED" not in enum_member_references(MANAGER_SOURCE, "BranchState")

    def test_substring_scan_would_false_positive(self) -> None:
        """**证明 L5 不能写成子串扫描**：docstring 里故意提到该状态名 ≥ 2 次。"""
        assert MANAGER_SOURCE.count("BranchState.REJECTED") >= 2
        assert "REJECTED" not in enum_member_references(MANAGER_SOURCE, "BranchState")

    def test_probe_attribute_form_goes_red(self) -> None:
        mutated = MANAGER_SOURCE + "\n\n\ndef _sneak():\n    return BranchState.REJECTED\n"
        assert "REJECTED" in enum_member_references(mutated, "BranchState")

    def test_probe_getattr_form_goes_red(self) -> None:
        mutated = (
            MANAGER_SOURCE + '\n\n\ndef _sneak():\n    return getattr(BranchState, "REJECTED")\n'
        )
        assert "REJECTED" in enum_member_references(mutated, "BranchState")


class TestL6NoClock:
    def test_real_source(self) -> None:
        assert forbidden_import_hits(MANAGER_SOURCE, ("time", "datetime")) == set()
        assert clock_call_hits(MANAGER_SOURCE) == []

    def test_probe_import_time_goes_red(self) -> None:
        assert forbidden_import_hits("import time\n", ("time", "datetime")) == {"time"}

    def test_probe_clock_call_goes_red(self) -> None:
        assert clock_call_hits("x = datetime.now()\n") == [("now", 1)]
        assert clock_call_hits("x = clock.monotonic()\n") == [("monotonic", 1)]


class TestL7CommitSignature:
    def test_real_source(self) -> None:
        assert (
            keyword_only_defaults(MANAGER_SOURCE, "AgentManager", "commit_started")
            == EXPECTED_COMMIT_KWONLY
        )

    def test_probe_default_on_handle_goes_red(self) -> None:
        mutated = MANAGER_SOURCE.replace(
            '        handle: str,\n        started_at: str,\n    ) -> None:\n        """**唯一**把',
            '        handle: str = "",\n        started_at: str,\n    ) -> None:\n        """**唯一**把',
        )
        assert mutated != MANAGER_SOURCE, "探针失效：替换未命中"
        assert (
            keyword_only_defaults(mutated, "AgentManager", "commit_started")
            != EXPECTED_COMMIT_KWONLY
        )

    def test_probe_extra_parameter_goes_red(self) -> None:
        mutated = MANAGER_SOURCE.replace(
            '        handle: str,\n        started_at: str,\n    ) -> None:\n        """**唯一**把',
            '        handle: str,\n        started_at: str,\n        secret: str,\n    ) -> None:\n        """**唯一**把',
        )
        assert mutated != MANAGER_SOURCE, "探针失效：替换未命中"
        assert (
            keyword_only_defaults(mutated, "AgentManager", "commit_started")
            != EXPECTED_COMMIT_KWONLY
        )


class TestL8Imports:
    def test_real_source_exact_token_set(self) -> None:
        tokens = set(imported_module_tokens(MANAGER_SOURCE))
        assert tokens == FROZEN_IMPORT_TOKENS, (
            f"新增：{tokens - FROZEN_IMPORT_TOKENS}；消失：{FROZEN_IMPORT_TOKENS - tokens}"
        )

    def test_real_source_forbidden_empty(self) -> None:
        assert forbidden_import_hits(MANAGER_SOURCE, FORBIDDEN_IMPORTS) == set()

    def test_probe_cost_import_goes_red(self) -> None:
        mutated = MANAGER_SOURCE + "\n\nfrom . import cost  # noqa\n"
        assert forbidden_import_hits(mutated, FORBIDDEN_IMPORTS) == {"cost"}
        assert set(imported_module_tokens(mutated)) != FROZEN_IMPORT_TOKENS

    def test_probe_adapter_import_goes_red(self) -> None:
        mutated = MANAGER_SOURCE + "\n\nfrom . import adapters  # noqa\n"
        assert "adapters" in forbidden_import_hits(mutated, FORBIDDEN_IMPORTS)
        assert set(imported_module_tokens(mutated)) != FROZEN_IMPORT_TOKENS

    def test_probe_policy_import_goes_red(self) -> None:
        mutated = MANAGER_SOURCE + "\n\nfrom .policy import EffectivePolicy  # noqa\n"
        assert "policy" in forbidden_import_hits(mutated, FORBIDDEN_IMPORTS)
        assert set(imported_module_tokens(mutated)) != FROZEN_IMPORT_TOKENS


class TestL13L14NoSilentDefaultsNoIO:
    def test_real_source(self) -> None:
        assert mapping_get_calls(MANAGER_SOURCE) == []
        assert open_calls(MANAGER_SOURCE) == []

    def test_probe_mapping_get_goes_red(self) -> None:
        assert mapping_get_calls("x = bundles.get(b, None)\n") == [1]

    def test_probe_open_goes_red(self) -> None:
        assert open_calls('x = open("f")\n') == [1]


class TestL9L10L12StateSets:
    """把 A13.4 / A13.5 / 6.4 的状态集合变成**可断言的字面事实**。"""

    def test_L9_L10_live_states(self) -> None:
        assert module_constant_expression(MANAGER_SOURCE, "_LIVE_STATES") == (
            "(BranchState.RUNNING, BranchState.WAITING_INPUT)"
        )

    def test_L10_waiting_input_is_in_live_states(self) -> None:
        assert "BranchState.WAITING_INPUT" in module_constant_expression(
            MANAGER_SOURCE, "_LIVE_STATES"
        )

    def test_L12_stoppable_states(self) -> None:
        assert module_constant_expression(MANAGER_SOURCE, "_STOPPABLE_STATES") == (
            "(BranchState.WAITING, BranchState.RUNNING, BranchState.WAITING_INPUT)"
        )

    def test_outcome_states_are_exactly_four_without_rejected(self) -> None:
        expression = module_constant_expression(MANAGER_SOURCE, "_OUTCOME_STATES")
        assert expression == (
            "(BranchState.COMPLETED, BranchState.FAILED,"
            " BranchState.ADOPTED, BranchState.OVERRIDDEN)"
        )
        assert "REJECTED" not in expression
        assert "STOPPED" not in expression

    def test_probe_dropping_waiting_input_goes_red(self) -> None:
        mutated = MANAGER_SOURCE.replace(
            "_LIVE_STATES = (BranchState.RUNNING, BranchState.WAITING_INPUT)",
            "_LIVE_STATES = (BranchState.RUNNING,)",
        )
        assert mutated != MANAGER_SOURCE, "探针失效：替换未命中"
        assert module_constant_expression(mutated, "_LIVE_STATES") != (
            "(BranchState.RUNNING, BranchState.WAITING_INPUT)"
        )


# ==========================================================================
# DTO 形状
# ==========================================================================


class TestDTOSurface:
    def test_agent_start_request_fields(self) -> None:
        assert dataclass_field_names(START_REQUEST_SOURCE, "AgentStartRequest") == (
            "branch_id",
            "attempt",
            "bundle",
        )

    def test_runtime_control_facts_fields(self) -> None:
        assert dataclass_field_names(CONTROL_FACTS_SOURCE, "RuntimeControlFacts") == (
            "stop_branches_requested",
        )

    def test_dispatch_action_still_four(self) -> None:
        assert [member.value for member in DispatchAction] == ["start", "queue", "reject", "reuse"]

    @pytest.mark.parametrize("bad", [True, False, 0, -1])
    def test_start_request_rejects_non_int_attempt(self, bad: object) -> None:
        with pytest.raises((TypeError, ValueError)):
            AgentStartRequest(
                branch_id=new_branch_id(),
                attempt=bad,  # type: ignore[arg-type]
                bundle=bundle(),
            )

    def test_start_request_rejects_non_bundle(self) -> None:
        with pytest.raises(TypeError):
            AgentStartRequest(
                branch_id=new_branch_id(),
                attempt=1,
                bundle="nope",  # type: ignore[arg-type]
            )

    def test_start_request_rejects_bad_branch_id(self) -> None:
        with pytest.raises(ValueError):
            AgentStartRequest(branch_id="nope", attempt=1, bundle=bundle())

    @pytest.mark.parametrize("bad", [1, 0, "yes", None])
    def test_control_facts_rejects_non_bool(self, bad: object) -> None:
        with pytest.raises(TypeError):
            RuntimeControlFacts(stop_branches_requested=bad)  # type: ignore[arg-type]

    def test_control_facts_accepts_bool(self) -> None:
        assert RuntimeControlFacts(stop_branches_requested=True).stop_branches_requested is True


# ==========================================================================
# 行为锁（B 系列）
# ==========================================================================


class TestB1B5Materialization:
    def test_B1_start_writes_no_record_and_returns_request(self) -> None:
        br = new_branch_id()
        subject = manager(br)
        starts = subject.materialize(
            decisions=(decide(br, DispatchAction.START),), bundles=bundles_for(br)
        )
        assert subject.routing_run.branches == ()  # A13.2：START 不写记录
        assert len(starts) == 1
        assert starts[0].branch_id == br
        assert starts[0].attempt == 1
        assert starts[0].bundle.goal == f"goal-{br}"

    def test_B2_in_flight_start_is_not_pending(self) -> None:
        """**重复派发防线**：已发许可但未提交的 child 必须**不在** pending 里。"""
        br = new_branch_id()
        subject = manager(br)
        subject.materialize(
            decisions=(decide(br, DispatchAction.START),), bundles=bundles_for(br)
        )
        assert subject.pending_admission_branch_ids == ()
        assert subject.active_subagent_count == 0

    def test_B3_queue_writes_waiting_and_is_pending(self) -> None:
        br = new_branch_id()
        subject = manager(br)
        starts = subject.materialize(
            decisions=(decide(br, DispatchAction.QUEUE),), bundles=bundles_for(br)
        )
        assert starts == ()
        records = subject.routing_run.branches
        assert len(records) == 1
        assert records[0].state is BranchState.WAITING
        assert records[0].attempt == 1
        assert subject.pending_admission_branch_ids == (br,)
        assert subject.active_subagent_count == 0  # WAITING **不**占槽

    def test_B4_reject_writes_no_record(self) -> None:
        br = new_branch_id()
        subject = manager(br)
        subject.materialize(
            decisions=(decide(br, DispatchAction.REJECT),), bundles=bundles_for(br)
        )
        assert subject.routing_run.branches == ()
        assert subject.pending_admission_branch_ids == ()

    def test_B5_reuse_writes_no_record(self) -> None:
        br = new_branch_id()
        subject = manager(br)
        subject.materialize(
            decisions=(decide(br, DispatchAction.REUSE),), bundles=bundles_for(br)
        )
        assert subject.routing_run.branches == ()


class TestB6B7VetoOnPlaceholder:
    def test_B6_reject_drops_never_dispatched_waiting(self) -> None:
        """先 QUEUE 后 REJECT ⇒ 占位被移除（否则 pending 永不收敛）。"""
        br = new_branch_id()
        subject = manager(br)
        subject.materialize(
            decisions=(decide(br, DispatchAction.QUEUE),), bundles=bundles_for(br)
        )
        assert subject.pending_admission_branch_ids == (br,)
        subject.materialize(
            decisions=(decide(br, DispatchAction.REJECT),), bundles=bundles_for(br)
        )
        assert subject.routing_run.branches == ()
        assert subject.pending_admission_branch_ids == ()

    def test_B6b_reuse_drops_never_dispatched_waiting(self) -> None:
        br = new_branch_id()
        subject = manager(br)
        subject.materialize(
            decisions=(decide(br, DispatchAction.QUEUE),), bundles=bundles_for(br)
        )
        subject.materialize(
            decisions=(decide(br, DispatchAction.REUSE),), bundles=bundles_for(br)
        )
        assert subject.routing_run.branches == ()

    def test_B7_reject_cannot_erase_a_running_agent(self) -> None:
        br = new_branch_id()
        subject = manager(br)
        start_and_commit(subject, br)
        with pytest.raises(AgentManagerError, match="不得抹掉"):
            subject.materialize(
                decisions=(decide(br, DispatchAction.REJECT),), bundles=bundles_for(br)
            )
        assert subject.routing_run.branches[0].state is BranchState.RUNNING

    def test_B7b_queue_cannot_be_reapplied_to_a_running_branch(self) -> None:
        br = new_branch_id()
        subject = manager(br)
        start_and_commit(subject, br)
        with pytest.raises(AgentManagerError, match="不得再物化 QUEUE"):
            subject.materialize(
                decisions=(decide(br, DispatchAction.QUEUE),), bundles=bundles_for(br)
            )


class TestB8B11CommitStarted:
    def test_B8_first_commit_creates_running_with_handle(self) -> None:
        br = new_branch_id()
        subject = manager(br)
        start_and_commit(subject, br, handle="platform-42")
        record = subject.routing_run.branches[0]
        assert record.state is BranchState.RUNNING
        assert record.handle == "platform-42"
        assert record.started_at == "2026-09-18T00:00:00Z"
        assert record.ended_at is None
        assert subject.active_subagent_count == 1

    def test_B9_queue_promotion_keeps_same_attempt_and_agent_id(self) -> None:
        """A13.2：原为 WAITING ⇒ **更新同一 attempt**，且 ``agent_id`` 不得重新 mint。"""
        br = new_branch_id()
        subject = manager(br)
        subject.materialize(
            decisions=(decide(br, DispatchAction.QUEUE),), bundles=bundles_for(br)
        )
        before = subject.routing_run.branches[0]
        subject.commit_started(
            branch_id=br, attempt=1, handle="h", started_at="2026-09-18T00:00:00Z"
        )
        after = subject.routing_run.branches
        assert len(after) == 1  # **没有**新建 attempt
        assert after[0].attempt == before.attempt
        assert after[0].agent_id == before.agent_id  # L15：内部身份不变
        assert after[0].state is BranchState.RUNNING

    def test_B9b_start_licence_for_queued_child_uses_same_attempt(self) -> None:
        """QUEUE 过的 child 再拿 START ⇒ 许可的 attempt 仍是 **1**（提升，不是新 attempt）。"""
        br = new_branch_id()
        subject = manager(br)
        subject.materialize(
            decisions=(decide(br, DispatchAction.QUEUE),), bundles=bundles_for(br)
        )
        starts = subject.materialize(
            decisions=(decide(br, DispatchAction.START),), bundles=bundles_for(br)
        )
        assert [item.attempt for item in starts] == [1]

    def test_B10_commit_requires_waiting_or_absent(self) -> None:
        br = new_branch_id()
        subject = manager(br)
        start_and_commit(subject, br)
        with pytest.raises(AgentManagerError, match="只有 WAITING"):
            subject.commit_started(
                branch_id=br, attempt=1, handle="h2", started_at="2026-09-18T00:00:01Z"
            )

    def test_B11_commit_attempt_must_match_latest(self) -> None:
        br = new_branch_id()
        subject = manager(br)
        subject.materialize(
            decisions=(decide(br, DispatchAction.QUEUE),), bundles=bundles_for(br)
        )
        with pytest.raises(AgentManagerError, match="最新 attempt 是 1"):
            subject.commit_started(
                branch_id=br, attempt=2, handle="h", started_at="2026-09-18T00:00:00Z"
            )

    def test_B11b_first_attempt_must_be_one(self) -> None:
        br = new_branch_id()
        subject = manager(br)
        with pytest.raises(AgentManagerError, match="不得从 attempt"):
            subject.commit_started(
                branch_id=br, attempt=3, handle="h", started_at="2026-09-18T00:00:00Z"
            )

    @pytest.mark.parametrize("bad", ["", "   ", None, 1])
    def test_B11c_handle_must_be_explicit(self, bad: object) -> None:
        br = new_branch_id()
        subject = manager(br)
        with pytest.raises(AgentManagerError):
            subject.commit_started(
                branch_id=br,
                attempt=1,
                handle=bad,  # type: ignore[arg-type]
                started_at="t",
            )

    @pytest.mark.parametrize("bad", ["", None, 7])
    def test_B11d_started_at_must_be_explicit(self, bad: object) -> None:
        br = new_branch_id()
        subject = manager(br)
        with pytest.raises(AgentManagerError):
            subject.commit_started(
                branch_id=br,
                attempt=1,
                handle="h",
                started_at=bad,  # type: ignore[arg-type]
            )


class TestB12B14RuntimeView:
    def test_B12_running_counts_as_active(self) -> None:
        br = new_branch_id()
        subject = manager(br)
        start_and_commit(subject, br)
        assert subject.active_subagent_count == 1

    def test_B13_only_latest_attempt_counts(self) -> None:
        """L9：attempt1(FAILED) + attempt2(RUNNING) ⇒ 计数 **1**，不是 2。"""
        br = new_branch_id()
        subject = manager(br)
        start_and_commit(subject, br, attempt=1)
        subject.record_outcome(
            branch_id=br, attempt=1, state=BranchState.FAILED, ended_at="2026-09-18T01:00:00Z"
        )
        start_and_commit(subject, br, attempt=2, handle="h-2")
        assert len(subject.routing_run.branches) == 2  # attempt history 保留两条
        assert subject.active_subagent_count == 1

    def test_B14_pending_follows_branch_order(self) -> None:
        first, second, third = new_branch_id(), new_branch_id(), new_branch_id()
        subject = manager(first, second, third)
        # 故意**逆序**裁决：输出必须仍按注入的 Plan.branches 顺序
        subject.materialize(
            decisions=(
                decide(third, DispatchAction.QUEUE),
                decide(first, DispatchAction.QUEUE),
            ),
            bundles=bundles_for(first, second, third),
        )
        assert subject.pending_admission_branch_ids == (first, third)

    def test_B14b_pending_excludes_terminal_and_live(self) -> None:
        queued, running, done = new_branch_id(), new_branch_id(), new_branch_id()
        subject = manager(queued, running, done)
        subject.materialize(
            decisions=(decide(queued, DispatchAction.QUEUE),),
            bundles=bundles_for(queued, running, done),
        )
        start_and_commit(subject, running)
        start_and_commit(subject, done)
        subject.record_outcome(branch_id=done, attempt=1, state=BranchState.FAILED, ended_at="t")
        assert subject.pending_admission_branch_ids == (queued,)


class TestB15B20OutcomeAndAttemptDepth:
    def test_B15_outcome_frees_capacity(self) -> None:
        br = new_branch_id()
        subject = manager(br)
        start_and_commit(subject, br)
        assert subject.active_subagent_count == 1
        subject.record_outcome(
            branch_id=br, attempt=1, state=BranchState.COMPLETED, ended_at="2026-09-18T03:00:00Z"
        )
        assert subject.active_subagent_count == 0  # **容量释放**（A13.6）
        record = subject.routing_run.branches[0]
        assert record.state is BranchState.COMPLETED
        assert record.handle == "h-1"  # 句柄与身份保留
        assert record.ended_at == "2026-09-18T03:00:00Z"
        assert record.agent_id  # 不丢身份

    @pytest.mark.parametrize(
        "bad", [BranchState.REJECTED, BranchState.STOPPED, BranchState.WAITING]
    )
    def test_B16_outcome_rejects_non_outcome_states(self, bad: BranchState) -> None:
        br = new_branch_id()
        subject = manager(br)
        start_and_commit(subject, br)
        with pytest.raises(AgentManagerError, match="四个终态"):
            subject.record_outcome(
                branch_id=br, attempt=1, state=bad, ended_at="2026-09-18T03:00:00Z"
            )

    def test_B17_outcome_requires_live_attempt(self) -> None:
        br = new_branch_id()
        subject = manager(br)
        subject.materialize(
            decisions=(decide(br, DispatchAction.QUEUE),), bundles=bundles_for(br)
        )
        with pytest.raises(AgentManagerError, match="只有 RUNNING / WAITING_INPUT"):
            subject.record_outcome(
                branch_id=br, attempt=1, state=BranchState.COMPLETED, ended_at="t"
            )

    def test_B17b_outcome_on_unknown_branch_fails(self) -> None:
        br, stranger = new_branch_id(), new_branch_id()
        subject = manager(br)
        with pytest.raises(AgentManagerError, match="没有任何 attempt 记录"):
            subject.record_outcome(
                branch_id=stranger, attempt=1, state=BranchState.FAILED, ended_at="t"
            )

    def test_B18_retry_opens_next_attempt(self) -> None:
        br = new_branch_id()
        subject = manager(br, limit=2)
        start_and_commit(subject, br, attempt=1)
        subject.record_outcome(
            branch_id=br, attempt=1, state=BranchState.FAILED, ended_at="2026-09-18T04:00:00Z"
        )
        starts = subject.materialize(
            decisions=(decide(br, DispatchAction.START),), bundles=bundles_for(br)
        )
        assert [item.attempt for item in starts] == [2]

    def test_B19_attempt_limit_is_enforced(self) -> None:
        """A13.7：``total_attempt_limit = 2`` ⇒ attempt 2 终止后**不得**再开 attempt 3。"""
        br = new_branch_id()
        subject = manager(br, limit=2)
        start_and_commit(subject, br, attempt=1)
        subject.record_outcome(branch_id=br, attempt=1, state=BranchState.FAILED, ended_at="t1")
        start_and_commit(subject, br, attempt=2, handle="h-2")
        subject.record_outcome(branch_id=br, attempt=2, state=BranchState.FAILED, ended_at="t2")
        with pytest.raises(AgentManagerError, match="已达 attempt 上限"):
            subject.materialize(
                decisions=(decide(br, DispatchAction.START),), bundles=bundles_for(br)
            )

    def test_B20_no_concurrent_dispatch_of_same_child(self) -> None:
        br = new_branch_id()
        subject = manager(br)
        start_and_commit(subject, br)
        with pytest.raises(AgentManagerError, match="不得再次派发"):
            subject.materialize(
                decisions=(decide(br, DispatchAction.START),), bundles=bundles_for(br)
            )


class TestB21B23RuntimeControl:
    def test_B21_false_produces_no_change(self) -> None:
        """L12：``False`` ⇒ **0 变更**，且**不得**读成"禁止停止"。"""
        br = new_branch_id()
        subject = manager(br)
        start_and_commit(subject, br)
        stopped = subject.apply_runtime_control(
            facts=RuntimeControlFacts(stop_branches_requested=False), ended_at="t"
        )
        assert stopped == ()
        assert subject.routing_run.branches[0].state is BranchState.RUNNING

    def test_B22_stop_all_covers_lifecycle_only(self) -> None:
        running, queued, vetoed, done = (
            new_branch_id(),
            new_branch_id(),
            new_branch_id(),
            new_branch_id(),
        )
        subject = manager(running, queued, vetoed, done)
        start_and_commit(subject, running)
        start_and_commit(subject, done)
        subject.record_outcome(
            branch_id=done, attempt=1, state=BranchState.COMPLETED, ended_at="t"
        )
        subject.materialize(
            decisions=(
                decide(queued, DispatchAction.QUEUE),
                decide(vetoed, DispatchAction.REJECT),
            ),
            bundles=bundles_for(running, queued, vetoed, done),
        )
        stopped = subject.apply_runtime_control(
            facts=RuntimeControlFacts(stop_branches_requested=True),
            ended_at="2026-09-18T05:00:00Z",
        )
        assert stopped == (running, queued)  # 按 branch_order；REJECT 无记录可写
        assert subject.routing_run.branch_by_id(running).state is BranchState.STOPPED
        assert subject.routing_run.branch_by_id(queued).state is BranchState.STOPPED
        assert subject.routing_run.branch_by_id(done).state is BranchState.COMPLETED  # 终态不动

    def test_B23_stop_all_drops_active_count(self) -> None:
        first, second = new_branch_id(), new_branch_id()
        subject = manager(first, second)
        start_and_commit(subject, first)
        start_and_commit(subject, second)
        assert subject.active_subagent_count == 2
        subject.apply_runtime_control(
            facts=RuntimeControlFacts(stop_branches_requested=True), ended_at="t"
        )
        assert subject.active_subagent_count == 0

    def test_B23b_stop_keeps_attempt_history(self) -> None:
        br = new_branch_id()
        subject = manager(br)
        start_and_commit(subject, br)
        subject.apply_runtime_control(
            facts=RuntimeControlFacts(stop_branches_requested=True), ended_at="t-stop"
        )
        records = subject.routing_run.branches
        assert len(records) == 1
        assert records[0].state is BranchState.STOPPED
        assert records[0].handle == "h-1"  # 句柄不丢
        assert records[0].ended_at == "t-stop"

    def test_B23c_stop_is_idempotent(self) -> None:
        br = new_branch_id()
        subject = manager(br)
        start_and_commit(subject, br)
        facts = RuntimeControlFacts(stop_branches_requested=True)
        assert subject.apply_runtime_control(facts=facts, ended_at="t") == (br,)
        assert subject.apply_runtime_control(facts=facts, ended_at="t2") == ()


class TestB24B29Boundaries:
    def test_B24_materialize_rejects_out_of_plan_branch(self) -> None:
        known, stranger = new_branch_id(), new_branch_id()
        subject = manager(known)
        with pytest.raises(AgentManagerError, match="越界"):
            subject.materialize(
                decisions=(decide(stranger, DispatchAction.QUEUE),),
                bundles=bundles_for(known, stranger),
            )

    def test_B24b_materialize_rejects_duplicate_decision(self) -> None:
        br = new_branch_id()
        subject = manager(br)
        with pytest.raises(AgentManagerError, match="重复裁决"):
            subject.materialize(
                decisions=(
                    decide(br, DispatchAction.QUEUE),
                    decide(br, DispatchAction.REJECT),
                ),
                bundles=bundles_for(br),
            )

    def test_B24c_materialize_rejects_non_tuple(self) -> None:
        br = new_branch_id()
        subject = manager(br)
        with pytest.raises(AgentManagerError, match="必须是 tuple"):
            subject.materialize(
                decisions=[decide(br, DispatchAction.QUEUE)],  # type: ignore[arg-type]
                bundles=bundles_for(br),
            )

    def test_B25_output_order_follows_branch_order(self) -> None:
        first, second = new_branch_id(), new_branch_id()
        subject = manager(first, second)
        starts = subject.materialize(
            decisions=(
                decide(second, DispatchAction.START),
                decide(first, DispatchAction.START),
            ),
            bundles=bundles_for(first, second),
        )
        assert [item.branch_id for item in starts] == [first, second]

    def test_B26_start_requires_prepared_bundle(self) -> None:
        br = new_branch_id()
        subject = manager(br)
        with pytest.raises(AgentManagerError, match="prepared ContextBundle"):
            subject.materialize(decisions=(decide(br, DispatchAction.START),), bundles={})

    def test_B27_branches_is_attempt_history(self) -> None:
        br = new_branch_id()
        subject = manager(br)
        start_and_commit(subject, br, attempt=1)
        subject.record_outcome(branch_id=br, attempt=1, state=BranchState.FAILED, ended_at="t1")
        start_and_commit(subject, br, attempt=2, handle="h-2")
        subject.record_outcome(
            branch_id=br, attempt=2, state=BranchState.COMPLETED, ended_at="t2"
        )
        history = subject.routing_run.branches
        assert [(item.attempt, item.state) for item in history] == [
            (1, BranchState.FAILED),
            (2, BranchState.COMPLETED),
        ]
        # A13.3：branch_by_id 取**最新** attempt（不是第一个匹配）
        assert subject.routing_run.branch_by_id(br).attempt == 2
        # A13.3：每 branch **至多一条**终止记录，且取最新 attempt 的终态（成功，不是历史失败）
        terminals = subject.routing_run.branch_terminals
        assert len(terminals) == 1
        assert terminals[0].branch_id == br

    def test_B28_routing_run_is_contract_valid(self) -> None:
        br = new_branch_id()
        subject = manager(br)
        start_and_commit(subject, br)
        run = subject.routing_run
        assert run.all_branches_terminated is False
        assert run.is_converged is False
        assert run.terminal is None

    def test_B29_empty_branch_order_is_normal(self) -> None:
        """``DIRECT`` / ``STAGED_SINGLE_AGENT`` ⇒ 0 child 是**正常输入**。"""
        subject = manager()
        assert subject.routing_run.branches == ()
        assert subject.active_subagent_count == 0
        assert subject.pending_admission_branch_ids == ()
        assert subject.materialize(decisions=(), bundles={}) == ()
        assert len(subject.routing_run.branch_terminals) == 0

    def test_B29b_branch_order_must_be_unique_tuple(self) -> None:
        br = new_branch_id()
        with pytest.raises(AgentManagerError, match="重复 branch_id"):
            AgentManager(run_id=new_run_id(), branch_order=(br, br), total_attempt_limit=2)
        with pytest.raises(AgentManagerError, match="必须是 tuple"):
            AgentManager(
                run_id=new_run_id(),
                branch_order=[br],  # type: ignore[arg-type]
                total_attempt_limit=2,
            )

    def test_B29c_attempt_limit_must_be_positive(self) -> None:
        with pytest.raises(AgentManagerError, match="total_attempt_limit"):
            AgentManager(run_id=new_run_id(), branch_order=(), total_attempt_limit=0)
        with pytest.raises(AgentManagerError, match="total_attempt_limit"):
            AgentManager(
                run_id=new_run_id(),
                branch_order=(),
                total_attempt_limit=True,  # type: ignore[arg-type]
            )

    def test_B29d_runtime_control_facts_type_is_checked(self) -> None:
        br = new_branch_id()
        subject = manager(br)
        with pytest.raises(AgentManagerError, match="facts 必须是 RuntimeControlFacts"):
            subject.apply_runtime_control(facts=True, ended_at="t")  # type: ignore[arg-type]
