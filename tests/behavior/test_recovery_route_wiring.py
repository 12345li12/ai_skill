"""``C2a 重决策请求载体``（`[14] → RedecisionRequest`）—— 结构锁 **LRT-1 – LRT-9** + 行为 **B1 – B7**。

架构依据：`docs/architecture.md` 2 节 ``[14] Recovery Router`` 节点行（**只产生决策，由对应节点
重新做正式决策**）· **A17.2**（九类 → 五目标满射表）· **A17.3**（V1 可达 / 不可达类别）·
**A17.4**（`carry_forward` 边界）· **A17.6 / A11.4b**（分支级身份 · 同一次配对事件）·
**`INV-09`**（`[14]` 是唯一合法回流控制器）· **§4.6 / `INV-16`**（授权链 —— 属 C2b）· L49。
范围、冻结、授权与实现记录见 `tests/behavior/README_step5b.md` **§108–§112**。

**本模块要守住的四条主张：**

  1. **唯一载体**：`RedecisionRequest(` 只有一处构造点，且五个实参**逐字**透传自 `RecoveryOutcome`；
  2. **carrier 不是 decision maker**：不构造 `RecoveryDecision` / `RecoveryOutcome`、
     **不 import `recovery.py`**（结构上无法调用 `decide_recovery`）；
  3. **`None` 是合法终态**：`decision is None` ⇒ 返回 `None`（不造假请求、不抛异常）；
  4. **只到请求为止**：不执行任何节点、不写运行态、不接授权链（越界即 LRT-4/5/6/7 变红）。

⚠️ **禁止子串扫描**：本文件 docstring 里**故意提及** `RecoveryDecision` / `decide_recovery` /
`Planner` / `CostSummary` 等词 ⇒ 所有锁一律 **AST 级**。
⚠️ 敏感性 probe 一律在**源码字符串**上完成（``_mutate``），**不覆写** ``src/`` 里的真实文件；
真实源码的一次性变异证据见 README §112.5。
"""

from __future__ import annotations

import dataclasses
import hashlib
import pathlib

import pytest

from adaptive_router.contracts import (
    BranchId,
    Confidence,
    FailureClass,
    RecoveryDecision,
    RecoveryTarget,
    new_branch_id,
)
from adaptive_router.recovery import FAILURE_CLASS_TO_TARGET, V1_REACHABLE
from adaptive_router.recovery_route_wiring import (
    REDECISION_TARGETS,
    RecoveryRouteWiringError,
    RedecisionRequest,
    build_redecision_request,
)
from adaptive_router.recovery_wiring import RecoveryOutcome, route_adapter_failure

from _recovery_route_locks import (
    annotated_assign_names,
    attribute_assignment_targets,
    attribute_value_shapes,
    call_keyword_shapes,
    call_keyword_shapes_by_caller,
    calls_to,
    clock_call_hits,
    forbidden_import_hits,
    function_raise_guards,
    function_return_annotation,
    function_return_shapes,
    function_signature_shape,
    imported_module_tokens,
    imported_names,
    module_level_container_assignments,
    module_level_function_names,
    open_calls,
    referenced_names,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
SRC = REPO_ROOT / "src/adaptive_router"
WIRING_PATH = SRC / "recovery_route_wiring.py"
WIRING = WIRING_PATH.read_text(encoding="utf-8")

#: LRT-2 —— `RedecisionRequest(` 的**五个实参逐字形态**（身份 + 决策内容，全部透传）。
EXPECTED_REQUEST_SHAPES = {
    "branch_id": ("outcome.branch_id",),
    "attempt": ("outcome.attempt",),
    "target": ("outcome.decision.route_to",),
    "minimal_correction": ("outcome.decision.minimal_correction",),
    "carry_forward": ("outcome.decision.carry_forward",),
}

#: LRT-4 —— 白名单常量**逐元素**（A17.3 的三个可达目标；`CONTEXT` / `SCHEDULER` 不得加入）。
EXPECTED_TARGET_SHAPES = (
    "RecoveryTarget.PLANNER",
    "RecoveryTarget.MODEL_ROUTER",
    "RecoveryTarget.CAPABILITY",
)

#: LRT-4 —— 两条 return 形态**恰好**是"无请求"与"有请求"（无第三条出口）。
#: ⚠️ `function_return_shapes` 用 `ast.walk`（BFS）⇒ 顺序**不保证**源码顺序，故按**集合**断言。
EXPECTED_REQUEST_RETURN = (
    "RedecisionRequest(branch_id=outcome.branch_id, attempt=outcome.attempt, "
    "target=outcome.decision.route_to, "
    "minimal_correction=outcome.decision.minimal_correction, "
    "carry_forward=outcome.decision.carry_forward)"
)
EXPECTED_RETURN_SHAPES = {"None", EXPECTED_REQUEST_RETURN}

#: LRT-9 / 结构 —— 两个模块级函数（不得增删）。
EXPECTED_FUNCTIONS = ("_require_outcome", "build_redecision_request")

#: LRT-5 / LRT-9 —— 依赖白名单（精确集合）。
#: ⚠️ `dataclasses` / `dataclass` 是唯一允许的 stdlib token（`RedecisionRequest` 是 frozen dataclass；
#: 与 C1 的 §107.5 偏差 1 同一先例，已批准）。
FUTURE_TOKENS = {"__future__", "annotations"}
ALLOWED_STDLIB = {"dataclasses", "dataclass"}
EXPECTED_IMPORT_MODULES = {"contracts", "recovery_wiring"}
EXPECTED_IMPORTED_NAMES = {"BranchId", "RecoveryTarget", "RecoveryOutcome", "dataclass"}

#: LRT-5 —— 五个重决策节点（既不 import、也不调用）。
NODE_MODULES = ("capabilities", "planner", "model_router", "context_builder", "scheduler")
NODE_CALLS = ("Planner", "ModelRouter", "CapabilityRegistry", "ContextBuilder", "Scheduler")

#: LRT-6 —— `[10]` 的控制方法（载体一个都不许调用）。
CONTROL_CALLS = (
    "materialize",
    "commit_started",
    "record_outcome",
    "close_run",
    "apply_runtime_control",
    "schedule",
)

#: LRT-7 —— 授权链与成本侧（重过门 / 重估属 C2b；C1 的成本边与本边互不 import）。
APPROVAL_COST_TOKENS = (
    "approval_judgment",
    "approval_constraints",
    "approval_disclosure",
    "policy",
    "cost",
    "cost.ledger_wiring",
)

#: LRT-8 —— 受保护 artifact（基线见 README §110.3 / §112.5）。
PROTECTED_ARTIFACTS = {
    "src/adaptive_router/recovery.py": (
        "3AACE179ECF6F32119E62C2FB84DE4C26D89DF220D801F393AF72E8EA96B756A"
    ),
    "src/adaptive_router/recovery_wiring.py": (
        "B56266000A5186B7DC1EE22B3CA3B4F100839F7F47DCAC1C7FBFFE90553C2767"
    ),
    "src/adaptive_router/recovery_context.py": (
        "C25B72AD62533177CE4DC17DFF55314C2A8703C1E45F50F7C667E88CB8AF4F2C"
    ),
    "src/adaptive_router/contracts/decision.py": (
        "CD65DCC9DA19557B158980FC9B2CA70DF3E90BF583FBCC41DD4BD96A09FED93E"
    ),
    "src/adaptive_router/capabilities.py": (
        "F068D2688DEF2BC23687093D331D9705E432C2A299CA98DF95C63CABE23CFDEB"
    ),
    "src/adaptive_router/planner.py": (
        "1966217F16039E05EB5D604BB1156D72011C35429798BA136F9904C61E2085C0"
    ),
    "src/adaptive_router/model_router.py": (
        "2E73C7B8F1A4D512AF77AA424B9468F8251F63FD50E8207D401DAAFFC5D81D48"
    ),
    "src/adaptive_router/context_builder.py": (
        "3EB22714EEF3579BEB895E909BAEE3860D836B9E071836A641C5141240FBE0A7"
    ),
    "src/adaptive_router/scheduler.py": (
        "17F00C5E4831F46F7855D6DE61DF2E4E05C8CE19E66A36B94C585F2F093B8F40"
    ),
    "docs/architecture.md": (
        "CB3FCE709C2548EF3C7676886CC921176E5B9C82AA10CDF506F6A18438FA1BB5"
    ),
}


def _mutate(old: str, new: str) -> str:
    """**合成变异源码**（只改字符串，不动 ``src/``）；锚点未命中即失败（防 probe 假绿）。"""
    variant = WIRING.replace(old, new)
    assert variant != WIRING, f"probe 的锚点未命中，变异未生效：{old!r}"
    return variant


# --------------------------------------------------------------------------
# 测试替身：`[14]` 的两条真实入口之一（adapter failure 侧，duck-typed 三字段）
# --------------------------------------------------------------------------


class _Kind:
    """`failure.failure_kind.value` 的最小形态（`recovery_wiring` 只认 `.value`）。"""

    def __init__(self, value: str) -> None:
        self.value = value


@dataclasses.dataclass
class _Failure:
    failure_kind: _Kind
    message: str
    timestamp: str


def failure(kind: str) -> _Failure:
    return _Failure(
        failure_kind=_Kind(kind),
        message=f"{kind} happened",
        timestamp="2026-09-19T00:00:00Z",
    )


def outcome_for(
    kind: str, *, attempt: int = 1, limit: int = 2, branch_id: BranchId | None = None
) -> RecoveryOutcome:
    """经**真实入口** `recovery_wiring.route_adapter_failure` 产出载体（不手造决策）。"""
    return route_adapter_failure(
        branch_id=branch_id if branch_id is not None else new_branch_id(),
        attempt=attempt,
        total_attempt_limit=limit,
        failure=failure(kind),
    )


def _unreachable(target: RecoveryTarget) -> RecoveryOutcome:
    """把真载体里的决策换成**V1 不可达目标**（**只在测试侧**制造非法状态，证明守卫会拒）。"""
    base = outcome_for("timeout")
    assert base.decision is not None
    illegal = RecoveryDecision(
        failure_class=FailureClass.INPUT,
        route_to=target,
        attempt=base.decision.attempt,
        total_attempt_limit=base.decision.total_attempt_limit,
        carry_forward=base.decision.carry_forward,
        minimal_correction=base.decision.minimal_correction,
        confidence=base.decision.confidence,
    )
    return dataclasses.replace(base, decision=illegal)


# ==========================================================================
# LRT-1 —— 唯一载体
# ==========================================================================


class TestLRT1SingleCarrier:
    def test_request_is_constructed_exactly_once(self) -> None:
        assert len(calls_to(WIRING, ("RedecisionRequest",))) == 1

    def test_the_only_construction_site_is_the_builder(self) -> None:
        assert set(call_keyword_shapes_by_caller(WIRING, "RedecisionRequest")) == {
            "build_redecision_request"
        }

    def test_probe_second_construction_site_goes_red(self) -> None:
        variant = _mutate(
            "    if outcome.decision is None:",
            "    RedecisionRequest(\n"
            "        branch_id=outcome.branch_id, attempt=outcome.attempt,\n"
            "        target=RecoveryTarget.PLANNER, minimal_correction=\"x\",\n"
            "        carry_forward=(),\n    )\n"
            "    if outcome.decision is None:",
        )
        assert len(calls_to(variant, ("RedecisionRequest",))) == 2


# ==========================================================================
# LRT-2 —— 只透传，不重算
# ==========================================================================


class TestLRT2PassthroughOnly:
    def test_the_five_arguments_are_the_decision_fields(self) -> None:
        shapes = call_keyword_shapes_by_caller(WIRING, "RedecisionRequest")
        assert shapes == {"build_redecision_request": EXPECTED_REQUEST_SHAPES}

    def test_no_star_star_escape_hatch(self) -> None:
        shapes = call_keyword_shapes_by_caller(WIRING, "RedecisionRequest")
        assert "**" not in shapes["build_redecision_request"]

    def test_probe_hardcoded_target_goes_red(self) -> None:
        variant = _mutate(
            "        target=outcome.decision.route_to,",
            "        target=RecoveryTarget.PLANNER,",
        )
        assert call_keyword_shapes_by_caller(variant, "RedecisionRequest") != {
            "build_redecision_request": EXPECTED_REQUEST_SHAPES
        }

    def test_probe_literal_correction_goes_red(self) -> None:
        variant = _mutate(
            "        minimal_correction=outcome.decision.minimal_correction,",
            '        minimal_correction="重试",',
        )
        assert call_keyword_shapes_by_caller(variant, "RedecisionRequest") != {
            "build_redecision_request": EXPECTED_REQUEST_SHAPES
        }


# ==========================================================================
# LRT-3 —— 不产生决策
# ==========================================================================


class TestLRT3NoDecisionProduced:
    def test_no_decision_or_carrier_is_constructed(self) -> None:
        assert calls_to(
            WIRING, ("RecoveryDecision", "RecoveryOutcome", "decide_recovery")
        ) == []

    def test_recovery_module_is_not_imported(self) -> None:
        assert "recovery" not in imported_module_tokens(WIRING)

    def test_decision_function_is_not_even_referenced(self) -> None:
        assert "decide_recovery" not in referenced_names(WIRING)

    def test_probe_decision_import_goes_red(self) -> None:
        variant = _mutate(
            "from .recovery_wiring import RecoveryOutcome",
            "from .recovery_wiring import RecoveryOutcome\n"
            "from .recovery import decide_recovery",
        )
        assert "recovery" in imported_module_tokens(variant)
        assert "decide_recovery" in referenced_names(variant)


# ==========================================================================
# LRT-4 —— V1 可达守卫 + 无请求语义
# ==========================================================================


class TestLRT4V1GuardAndNoRequest:
    def test_the_guard_is_the_v1_target_set(self) -> None:
        assert (
            "outcome.decision.route_to not in REDECISION_TARGETS",
            "RecoveryRouteWiringError",
        ) in function_raise_guards(WIRING, "build_redecision_request")

    def test_the_target_set_is_exactly_the_three_reachable_targets(self) -> None:
        assert attribute_value_shapes(WIRING, "REDECISION_TARGETS") == (
            EXPECTED_TARGET_SHAPES
        )

    def test_there_are_exactly_two_returns(self) -> None:
        shapes = function_return_shapes(WIRING, "build_redecision_request")
        assert len(shapes) == 2
        assert set(shapes) == EXPECTED_RETURN_SHAPES

    def test_probe_dropped_guard_goes_red(self) -> None:
        variant = _mutate(
            "    if outcome.decision.route_to not in REDECISION_TARGETS:",
            "    if False:",
        )
        assert (
            "outcome.decision.route_to not in REDECISION_TARGETS",
            "RecoveryRouteWiringError",
        ) not in function_raise_guards(variant, "build_redecision_request")

    def test_probe_widened_target_set_goes_red(self) -> None:
        variant = _mutate(
            "    RecoveryTarget.CAPABILITY,\n)",
            "    RecoveryTarget.CAPABILITY,\n    RecoveryTarget.CONTEXT,\n)",
        )
        assert attribute_value_shapes(variant, "REDECISION_TARGETS") != (
            EXPECTED_TARGET_SHAPES
        )


# ==========================================================================
# LRT-5 —— 不执行节点（含 import 白名单）
# ==========================================================================


class TestLRT5NoNodeExecution:
    def test_no_node_is_imported_or_called(self) -> None:
        assert calls_to(WIRING, NODE_CALLS) == []
        assert forbidden_import_hits(WIRING, NODE_MODULES) == set()

    def test_import_whitelist_is_exact(self) -> None:
        tokens = imported_module_tokens(WIRING) - FUTURE_TOKENS
        assert tokens == EXPECTED_IMPORT_MODULES | ALLOWED_STDLIB

    def test_probe_node_import_goes_red(self) -> None:
        variant = _mutate(
            "from .contracts import BranchId, RecoveryTarget",
            "from .contracts import BranchId, RecoveryTarget\n"
            "from .planner import Planner",
        )
        assert forbidden_import_hits(variant, NODE_MODULES) != set()
        assert imported_module_tokens(variant) - FUTURE_TOKENS != (
            EXPECTED_IMPORT_MODULES | ALLOWED_STDLIB
        )


# ==========================================================================
# LRT-6 —— 不写运行态
# ==========================================================================


class TestLRT6NoRuntimeWrite:
    def test_no_control_method_is_called(self) -> None:
        assert calls_to(WIRING, CONTROL_CALLS) == []

    def test_no_runtime_import(self) -> None:
        assert forbidden_import_hits(
            WIRING, ("agent_manager", "scheduler", "execution", "adapters")
        ) == set()

    def test_probe_control_call_goes_red(self) -> None:
        variant = _mutate(
            "from .contracts import BranchId, RecoveryTarget",
            "from .agent_manager import AgentManager\n"
            "from .contracts import BranchId, RecoveryTarget",
        )
        variant = variant.replace(
            "    if outcome.decision is None:",
            "    AgentManager(\n"
            "        run_id=\"run_x\", branch_order=(), total_attempt_limit=1\n"
            "    ).close_run(\n"
            "        terminal_state=None, no_pending_approval=True,\n"
            "        no_pending_recovery=True, ended_at=\"MR\",\n"
            "    )\n"
            "    if outcome.decision is None:",
            1,
        )
        assert calls_to(variant, ("close_run",)) != []
        assert forbidden_import_hits(variant, ("agent_manager",)) != set()


# ==========================================================================
# LRT-7 —— 不接授权链 / 不碰成本侧
# ==========================================================================


class TestLRT7NoApprovalOrCost:
    def test_no_approval_or_cost_import(self) -> None:
        assert forbidden_import_hits(WIRING, APPROVAL_COST_TOKENS) == set()

    def test_cost_summary_is_not_referenced(self) -> None:
        assert "CostSummary" not in referenced_names(WIRING)

    def test_probe_approval_import_goes_red(self) -> None:
        variant = _mutate(
            "from .contracts import BranchId, RecoveryTarget",
            "from .approval_judgment import judge_approval\n"
            "from .contracts import BranchId, RecoveryTarget",
        )
        assert forbidden_import_hits(variant, APPROVAL_COST_TOKENS) != set()


# ==========================================================================
# LRT-8 —— 受保护 artifact
# ==========================================================================


# ==========================================================================
# LRT-9 —— 无时钟 / 无状态
# ==========================================================================


class TestLRT9NoClockNoState:
    def test_no_clock_calls(self) -> None:
        assert clock_call_hits(WIRING) == []

    def test_no_module_state_and_no_io(self) -> None:
        assert module_level_container_assignments(WIRING) == ()
        assert annotated_assign_names(WIRING) == ()
        assert attribute_assignment_targets(WIRING) == ()
        assert open_calls(WIRING) == []

    def test_function_inventory_and_signatures_are_kw_only(self) -> None:
        assert module_level_function_names(WIRING) == EXPECTED_FUNCTIONS
        assert function_signature_shape(WIRING, "build_redecision_request") == {
            "positional": [],
            "kwonly": ["outcome"],
            "defaults": [False],
            "vararg": None,
            "kwarg": None,
        }
        assert function_signature_shape(WIRING, "_require_outcome")["kwarg"] is None
        assert (
            function_return_annotation(WIRING, "build_redecision_request")
            == "RedecisionRequest | None"
        )

    def test_probe_clock_goes_red(self) -> None:
        variant = _mutate(
            "from dataclasses import dataclass",
            "import time\nfrom dataclasses import dataclass",
        )
        variant = variant.replace(
            "    if outcome.decision is None:",
            "    if time.time() and outcome.decision is None:",
            1,
        )
        assert clock_call_hits(variant) != []

    def test_probe_module_level_list_goes_red(self) -> None:
        variant = _mutate(
            "REDECISION_TARGETS = (",
            "REDECISION_NAMES = [\n    \"planner\",\n]\nREDECISION_TARGETS = (",
        )
        assert module_level_container_assignments(variant) != ()


# ==========================================================================
# 行为测试 B1 – B7
# ==========================================================================


class TestBuildRedecisionRequest:
    def test_b1_a_reachable_target_yields_the_request(self) -> None:
        outcome = outcome_for("timeout")
        assert outcome.decision is not None
        request = build_redecision_request(outcome=outcome)
        assert request is not None
        assert request.target is RecoveryTarget.MODEL_ROUTER
        assert request.minimal_correction == outcome.decision.minimal_correction
        assert request.carry_forward == outcome.decision.carry_forward

    def test_b2_identity_comes_from_the_pairing_event(self) -> None:
        branch_id = new_branch_id()
        outcome = outcome_for("crash", attempt=2, limit=3, branch_id=branch_id)
        request = build_redecision_request(outcome=outcome)
        assert request is not None
        assert request.branch_id == branch_id
        assert request.attempt == 2  # 分支级 attempt（A17.6），不是恒定 1

    def test_b3_no_decision_means_no_request(self) -> None:
        outcome = outcome_for("timeout", attempt=1, limit=1)
        assert outcome.decision is None
        assert outcome.should_recover is False
        assert build_redecision_request(outcome=outcome) is None

    def test_b4_unreachable_targets_are_explicit_failures(self) -> None:
        for target in (RecoveryTarget.CONTEXT, RecoveryTarget.SCHEDULER):
            illegal = _unreachable(target)
            with pytest.raises(RecoveryRouteWiringError):
                build_redecision_request(outcome=illegal)

    def test_b5_every_v1_reachable_target_is_producible(self) -> None:
        produced = {}
        for kind in ("refused", "timeout", "crash", "network"):
            request = build_redecision_request(outcome=outcome_for(kind))
            assert request is not None
            produced[kind] = request.target
        assert produced == {
            "refused": RecoveryTarget.PLANNER,
            "timeout": RecoveryTarget.MODEL_ROUTER,
            "crash": RecoveryTarget.CAPABILITY,
            "network": RecoveryTarget.CAPABILITY,
        }

    def test_b6_non_outcome_is_an_explicit_failure(self) -> None:
        for bad in ("nope", None, 1):
            with pytest.raises(RecoveryRouteWiringError):
                build_redecision_request(outcome=bad)  # type: ignore[arg-type]

    def test_b7_target_set_and_request_are_immutable(self) -> None:
        assert isinstance(REDECISION_TARGETS, tuple)
        assert set(REDECISION_TARGETS) == {
            RecoveryTarget.PLANNER,
            RecoveryTarget.MODEL_ROUTER,
            RecoveryTarget.CAPABILITY,
        }
        request = build_redecision_request(outcome=outcome_for("refused"))
        assert request is not None
        with pytest.raises(dataclasses.FrozenInstanceError):
            request.target = RecoveryTarget.PLANNER  # type: ignore[misc]

    def test_b7b_c2a_target_set_agrees_with_recovery_v1_reachable(self) -> None:
        """跨边不变量：`[14]` 的 `V1_REACHABLE` 经 A17.2 **恰好**落到 C2a 的白名单。"""
        assert {FAILURE_CLASS_TO_TARGET[item] for item in V1_REACHABLE} == set(
            REDECISION_TARGETS
        )
