"""``C2b-1 重过门请求载体``（`RedecisionRequest → RegateRequest`）—— 结构锁 **LRG-1 – LRG-9** + 行为 **B1 – B7**。

架构依据：`docs/architecture.md` **§4.4**（回流流：重决策 → 重新估算 → **Approval Judgment** → 条件性 Gate）·
**§4.6**（R1 / R2 / R3 的授权**路径不跳过**；**Gate 条件性**）· **`INV-16`**（回流加码必须重过 Gate）·
**L43 / `INV-10`**（→ C2b-2）· **A6**（constraints 缺失 ⇒ 显式失败）· **A7 / A8**（判定链与 disclosure）·
**A17**（`[14]` 只路由不执行）· **A18.2**（严格 bool 先例）· **A18.5**（V1 无 Approval Gate）。
范围、冻结、授权与实现记录见 `tests/behavior/README_step5b.md` **§114–§118**。

**本模块要守住的五条主张：**

  1. **唯一载体**：`RegateRequest(` 只有一处构造点，六实参**逐字**透传；
  2. **carrier 不是 decision maker**：不产判定（不 import `approval_judgment` / `risk`）；
  3. **不实现 Gate / 不假装批准**：DTO **没有**"批准结果"字段，`needs_approval` 一个都不出现；
  4. **不重估 / 不重跑 / 不写运行态**：不 import `cost*` / `planner` / `model_router` / `router` /
     `agent_manager` / `scheduler` / `execution`；
  5. **加码事实只透传、不伪造**：`escalates=False` 是**合法输入**，但**内部覆盖**必然变红
     （绑定集合锁 + 行为 B2 双重防线）。

⚠️ **禁止子串扫描**：本文件 docstring 里**故意提及** `needs_approval` / `ApprovalJudge` /
`CostEstimate` / `materialize` 等词 ⇒ 所有锁一律 **AST 级**。
⚠️ 敏感性 probe 一律在**源码字符串**上完成（``_mutate``），**不覆写** ``src/`` 里的真实文件；
真实源码的一次性变异证据见 README §118.5。
"""

from __future__ import annotations

import dataclasses
import hashlib
import pathlib

import pytest

from adaptive_router.contracts import RecoveryTarget, new_branch_id
from adaptive_router.recovery_regate_wiring import (
    RecoveryRegateWiringError,
    RegateRequest,
    build_regate_request,
)
from adaptive_router.recovery_route_wiring import (
    RedecisionRequest,
    build_redecision_request,
)
from adaptive_router.recovery_wiring import route_adapter_failure

from _recovery_regate_locks import (
    annotated_assign_names,
    annotated_name_targets,
    assignment_value_shapes,
    attribute_assignment_targets,
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
    none_coalescing_shapes,
    open_calls,
    referenced_names,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
SRC = REPO_ROOT / "src/adaptive_router"
WIRING_PATH = SRC / "recovery_regate_wiring.py"
WIRING = WIRING_PATH.read_text(encoding="utf-8")

#: LRG-2 —— `RegateRequest(` 的**六个实参逐字形态**（五字段透传 + 加码事实）。
EXPECTED_REQUEST_SHAPES = {
    "branch_id": ("request.branch_id",),
    "attempt": ("request.attempt",),
    "target": ("request.target",),
    "minimal_correction": ("request.minimal_correction",),
    "carry_forward": ("request.carry_forward",),
    "escalates": ("escalates",),
}

#: LRG-4 —— DTO 的**字段名集合精确等于六个**（防"加一个 approved / needs_approval 字段"）。
EXPECTED_DTO_FIELDS = (
    "branch_id",
    "attempt",
    "target",
    "minimal_correction",
    "carry_forward",
    "escalates",
)

#: LRG-2 —— 模块内**允许出现**的绑定：只有 `__all__`（函数体内**不得有任何局部绑定**）。
EXPECTED_BINDINGS = {"__all__"}

#: 结构 —— 三个模块级函数（不得增删）。
EXPECTED_FUNCTIONS = ("_require_request", "_require_escalates", "build_regate_request")

#: LRG-9 —— 依赖白名单（精确集合；`dataclasses` / `dataclass` 与 C1 / C2a 同一先例）。
FUTURE_TOKENS = {"__future__", "annotations"}
ALLOWED_STDLIB = {"dataclasses", "dataclass"}
EXPECTED_IMPORT_MODULES = {"contracts", "recovery_route_wiring"}
EXPECTED_IMPORTED_NAMES = {
    "BranchId",
    "RecoveryTarget",
    "RedecisionRequest",
    "dataclass",
}

#: LRG-3 / LRG-4 —— 判定面与 Gate 面（本模块一个都不许出现）。
JUDGMENT_TOKENS = ("approval_judgment", "approval_constraints", "approval_disclosure", "risk")
GATE_NAMES = ("ApprovalGate", "GateResult", "needs_approval", "approved", "ApprovalJudge")

#: LRG-5 / LRG-6 —— 成本 / 决策链 / 运行态。
COST_CHAIN_TOKENS = ("cost", "planner", "model_router", "router", "execution", "scheduler", "agent_manager")
COST_CHAIN_CALLS = ("CostEstimate", "Plan", "ModelSelection", "RoutingPlan")
CONTROL_CALLS = (
    "materialize",
    "commit_started",
    "record_outcome",
    "close_run",
    "apply_runtime_control",
    "schedule",
)

#: LRG-7 —— 十三件受保护 artifact（基线见 README §117.4 / §118.6）。
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
    "src/adaptive_router/recovery_route_wiring.py": (
        "B63EFD125DD1039D91F8FE839941D8BAF308C26BF748CA44B1FA79E625622EC3"
    ),
    "tests/behavior/_recovery_route_locks.py": (
        "F22A5A49B0216BEE6FEDBC258C27E63A57A74425C5C70FB84B69196F77D1F023"
    ),
    "tests/behavior/test_recovery_route_wiring.py": (
        "5F0461EAB26AD496126D31B231E226AB5978D802D237B7594521DEC7AC47774F"
    ),
    "src/adaptive_router/approval_judgment.py": (
        "E38435FD3744D0DE5215C5075A1C3413F960705BA61D906E1584544201C4AAD6"
    ),
    "src/adaptive_router/approval_constraints.py": (
        "4031F4968D22B1C5541DE2E3A99E0B800CC7EED0552D06FBD73467DAB9CE8DBD"
    ),
    "src/adaptive_router/approval_disclosure.py": (
        "C2C4052A6B63C971F63CF3E37D220218A7D8E401D731BDAB13CBEAAF9D2DAE00"
    ),
    "src/adaptive_router/router.py": (
        "B64F82C52C6FBFA1DC2D8EC5165F3328D0004BBEBA2424B518C02AC3FE1B07D3"
    ),
    "src/adaptive_router/cost/wiring.py": (
        "1292AB3892E661F43F4CE5B763A38BBCADE2DBCE28C2E884C5D5CE4DA4693E51"
    ),
    "src/adaptive_router/contracts/decision.py": (
        "CD65DCC9DA19557B158980FC9B2CA70DF3E90BF583FBCC41DD4BD96A09FED93E"
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
# 测试替身：经**真实入口**造一个 C2a 的 `RedecisionRequest`
# --------------------------------------------------------------------------


class _Kind:
    def __init__(self, value: str) -> None:
        self.value = value


@dataclasses.dataclass
class _Failure:
    failure_kind: _Kind
    message: str
    timestamp: str


def c2a_request(kind: str = "timeout", *, attempt: int = 1, limit: int = 2) -> RedecisionRequest:
    """从 `[14]` 的真实载体出发，经 C2a 得到重决策请求（**不手造**）。"""
    outcome = route_adapter_failure(
        branch_id=new_branch_id(),
        attempt=attempt,
        total_attempt_limit=limit,
        failure=_Failure(
            failure_kind=_Kind(kind),
            message=f"{kind} happened",
            timestamp="2026-09-19T00:00:00Z",
        ),
    )
    request = build_redecision_request(outcome=outcome)
    assert request is not None
    return request


# ==========================================================================
# LRG-1 —— 唯一载体
# ==========================================================================


class TestLRG1SingleCarrier:
    def test_request_is_constructed_exactly_once(self) -> None:
        assert len(calls_to(WIRING, ("RegateRequest",))) == 1

    def test_the_only_construction_site_is_the_builder(self) -> None:
        assert set(call_keyword_shapes_by_caller(WIRING, "RegateRequest")) == {
            "build_regate_request"
        }

    def test_probe_second_construction_site_goes_red(self) -> None:
        variant = _mutate(
            "    _require_request(request)",
            "    RegateRequest(\n"
            "        branch_id=request.branch_id, attempt=request.attempt,\n"
            "        target=request.target, minimal_correction=\"x\",\n"
            "        carry_forward=(), escalates=escalates,\n    )\n"
            "    _require_request(request)",
        )
        assert len(calls_to(variant, ("RegateRequest",))) == 2


# ==========================================================================
# LRG-2 —— 只透传 / 只组装（形态 + 无重绑定）
# ==========================================================================


class TestLRG2PassthroughOnly:
    def test_the_six_arguments_are_the_request_fields_and_the_fact(self) -> None:
        shapes = call_keyword_shapes_by_caller(WIRING, "RegateRequest")
        assert shapes == {"build_regate_request": EXPECTED_REQUEST_SHAPES}

    def test_no_star_star_escape_hatch(self) -> None:
        shapes = call_keyword_shapes_by_caller(WIRING, "RegateRequest")
        assert "**" not in shapes["build_regate_request"]

    def test_the_module_has_no_local_bindings_beyond_dunder_all(self) -> None:
        """**就地收紧**（§117.5）：函数体内不得有任何局部绑定 ⇒ 内部覆盖 `escalates` 必然变红。"""
        bound = set(assignment_value_shapes(WIRING)) | set(
            annotated_name_targets(WIRING)
        )
        assert bound == EXPECTED_BINDINGS

    def test_probe_internal_override_goes_red(self) -> None:
        variant = _mutate(
            "    _require_escalates(escalates)",
            "    escalates = False\n    _require_escalates(escalates)",
        )
        bound = set(assignment_value_shapes(variant)) | set(
            annotated_name_targets(variant)
        )
        assert bound != EXPECTED_BINDINGS

    def test_probe_inverted_fact_goes_red(self) -> None:
        variant = _mutate("        escalates=escalates,", "        escalates=not escalates,")
        assert call_keyword_shapes_by_caller(variant, "RegateRequest") != {
            "build_regate_request": EXPECTED_REQUEST_SHAPES
        }


# ==========================================================================
# LRG-3 —— 不产判定
# ==========================================================================


class TestLRG3NoJudgment:
    def test_no_judgment_is_constructed(self) -> None:
        assert calls_to(WIRING, ("ApprovalJudgment", "ApprovalJudge")) == []
        assert "ApprovalJudge" not in referenced_names(WIRING)

    def test_judgment_modules_are_not_imported(self) -> None:
        assert forbidden_import_hits(WIRING, JUDGMENT_TOKENS) == set()

    def test_probe_judgment_import_goes_red(self) -> None:
        variant = _mutate(
            "from .recovery_route_wiring import RedecisionRequest",
            "from .recovery_route_wiring import RedecisionRequest\n"
            "from .approval_judgment import ApprovalJudge",
        )
        variant = variant.replace(
            "    _require_request(request)",
            "    ApprovalJudge\n    _require_request(request)",
            1,
        )
        assert forbidden_import_hits(variant, JUDGMENT_TOKENS) != set()
        assert "ApprovalJudge" in referenced_names(variant)


# ==========================================================================
# LRG-4 —— 不实现 Gate / 不假装批准
# ==========================================================================


class TestLRG4NoGateNoFakeApproval:
    def test_no_gate_or_approval_semantics_appear(self) -> None:
        names = referenced_names(WIRING)
        for name in GATE_NAMES:
            assert name not in names, name
        assert calls_to(WIRING, GATE_NAMES) == []

    def test_the_dto_fields_are_exactly_the_six_facts(self) -> None:
        assert tuple(f.name for f in dataclasses.fields(RegateRequest)) == (
            EXPECTED_DTO_FIELDS
        )

    def test_probe_needs_approval_field_goes_red(self) -> None:
        variant = _mutate(
            "    escalates: bool",
            "    escalates: bool\n    needs_approval: bool",
        )
        assert "needs_approval" in referenced_names(variant)


# ==========================================================================
# LRG-5 —— 不重估成本 / 不重跑决策链
# ==========================================================================


class TestLRG5NoReestimatingNoRerun:
    def test_no_cost_or_decision_chain_calls(self) -> None:
        assert calls_to(WIRING, COST_CHAIN_CALLS) == []

    def test_no_cost_or_decision_chain_import(self) -> None:
        assert forbidden_import_hits(WIRING, COST_CHAIN_TOKENS) == set()

    def test_probe_cost_import_goes_red(self) -> None:
        variant = _mutate(
            "from .recovery_route_wiring import RedecisionRequest",
            "from .recovery_route_wiring import RedecisionRequest\n"
            "from .cost.wiring import CostEstimatorWiring",
        )
        assert forbidden_import_hits(variant, COST_CHAIN_TOKENS) != set()


# ==========================================================================
# LRG-6 —— 不写运行态
# ==========================================================================


class TestLRG6NoRuntimeWrite:
    def test_no_control_method_is_called(self) -> None:
        assert calls_to(WIRING, CONTROL_CALLS) == []

    def test_no_runtime_import(self) -> None:
        assert forbidden_import_hits(
            WIRING, ("agent_manager", "scheduler", "execution", "adapters")
        ) == set()

    def test_probe_control_call_goes_red(self) -> None:
        variant = _mutate(
            "from .recovery_route_wiring import RedecisionRequest",
            "from .agent_manager import AgentManager\n"
            "from .recovery_route_wiring import RedecisionRequest",
        )
        variant = variant.replace(
            "    _require_request(request)",
            "    AgentManager(\n"
            '        run_id="run_mg5", branch_order=(), total_attempt_limit=1\n'
            "    ).materialize(decisions=(), bundles={})\n"
            "    _require_request(request)",
            1,
        )
        assert calls_to(variant, ("materialize",)) != []
        assert forbidden_import_hits(variant, ("agent_manager",)) != set()


# ==========================================================================
# LRG-7 —— 受保护 artifact（十三件）
# ==========================================================================


# ==========================================================================
# LRG-8 / LRG-9 —— 无时钟无状态 / 依赖白名单
# ==========================================================================


class TestLRG8NoClockNoState:
    def test_no_clock_no_state_no_io(self) -> None:
        assert clock_call_hits(WIRING) == []
        assert module_level_container_assignments(WIRING) == ()
        assert annotated_assign_names(WIRING) == ()
        assert attribute_assignment_targets(WIRING) == ()
        assert open_calls(WIRING) == []
        assert none_coalescing_shapes(WIRING) == ()

    def test_probe_clock_goes_red(self) -> None:
        variant = _mutate(
            "from dataclasses import dataclass",
            "import time\nfrom dataclasses import dataclass",
        )
        variant = variant.replace(
            "    _require_request(request)",
            "    time.time() and _require_request(request)",
            1,
        )
        assert clock_call_hits(variant) != []


class TestLRG9ImportWhitelist:
    def test_import_whitelist_is_exact(self) -> None:
        tokens = imported_module_tokens(WIRING) - FUTURE_TOKENS
        assert tokens == EXPECTED_IMPORT_MODULES | ALLOWED_STDLIB

    def test_imported_names_are_exact(self) -> None:
        assert imported_names(WIRING) - FUTURE_TOKENS == EXPECTED_IMPORTED_NAMES

    def test_function_inventory_signatures_and_returns(self) -> None:
        assert module_level_function_names(WIRING) == EXPECTED_FUNCTIONS
        assert function_signature_shape(WIRING, "build_regate_request") == {
            "positional": [],
            "kwonly": ["request", "escalates"],
            "defaults": [False, False],
            "vararg": None,
            "kwarg": None,
        }
        assert function_return_annotation(WIRING, "build_regate_request") == "RegateRequest"
        assert ("not isinstance(request, RedecisionRequest)", "RecoveryRegateWiringError") in (
            function_raise_guards(WIRING, "_require_request")
        )
        assert (
            "type(value) is not bool",
            "RecoveryRegateWiringError",
        ) in function_raise_guards(WIRING, "_require_escalates")
        assert len(function_return_shapes(WIRING, "build_regate_request")) == 1

    def test_probe_extra_import_goes_red(self) -> None:
        variant = _mutate(
            "from .contracts import BranchId, RecoveryTarget",
            "from .contracts import BranchId, RecoveryTarget\nimport os",
        )
        assert imported_module_tokens(variant) - FUTURE_TOKENS != (
            EXPECTED_IMPORT_MODULES | ALLOWED_STDLIB
        )


# ==========================================================================
# 行为测试 B1 – B7
# ==========================================================================


class TestBuildRegateRequest:
    def test_b1_the_five_fields_are_passed_through(self) -> None:
        request = c2a_request("crash", attempt=2, limit=3)
        regate = build_regate_request(request=request, escalates=True)
        assert regate.branch_id == request.branch_id
        assert regate.attempt == request.attempt == 2
        assert regate.target is request.target
        assert regate.minimal_correction == request.minimal_correction
        assert regate.carry_forward == request.carry_forward

    def test_b2_the_escalation_fact_is_passed_through_both_ways(self) -> None:
        """加码事实**双向**透传：`True` 与 `False` 都必须**原样**到达（不得被改写）。"""
        request = c2a_request()
        for value in (True, False):
            regate = build_regate_request(request=request, escalates=value)
            assert regate.escalates is value

    def test_b3_escalates_must_be_a_strict_bool(self) -> None:
        request = c2a_request()
        for bad in (1, 0, "true", "True", None, [], object()):
            with pytest.raises(RecoveryRegateWiringError):
                build_regate_request(request=request, escalates=bad)  # type: ignore[arg-type]

    def test_b4_non_request_is_an_explicit_failure(self) -> None:
        for bad in ("nope", None, 1, object()):
            with pytest.raises(RecoveryRegateWiringError):
                build_regate_request(request=bad, escalates=True)  # type: ignore[arg-type]

    def test_b5_non_escalating_still_enters_the_chain(self) -> None:
        """§4.6：授权**路径不跳过** ⇒ 非加码**仍然产出请求**（不是 `None`、不抛异常）。"""
        regate = build_regate_request(request=c2a_request(), escalates=False)
        assert isinstance(regate, RegateRequest)
        assert regate.escalates is False

    def test_b6_the_dto_is_frozen_and_carries_no_approval_result(self) -> None:
        regate = build_regate_request(request=c2a_request(), escalates=True)
        with pytest.raises(dataclasses.FrozenInstanceError):
            regate.escalates = False  # type: ignore[misc]
        names = {f.name for f in dataclasses.fields(regate)}
        assert names == set(EXPECTED_DTO_FIELDS)
        assert not names & {"approved", "needs_approval", "gate", "granted", "judgment"}

    def test_b7_the_regate_request_is_a_distinct_type(self) -> None:
        request = c2a_request()
        regate = build_regate_request(request=request, escalates=True)
        assert type(regate) is not type(request)
        assert not isinstance(regate, RedecisionRequest)
        assert not isinstance(request, RegateRequest)
        assert regate.target is RecoveryTarget.MODEL_ROUTER  # timeout → MODEL_ROUTER（A17.1/A17.2）
