"""``C2b-2b-1 MODIFY 重入请求载体``（`GateOutcome → ModifyRerouteRequest`）—— 结构锁 **LRM-1 – LRM-9** + 行为 **B1 – B6**。

架构依据：`docs/architecture.md` **Approval Gate 节点行**（MODIFY 出口逐字：
回 `[02]` Policy → 重建 AuthorizationContext → 从最早受影响节点重算 → Judgment → 必要时再次 Gate；
**"永远不能直接执行旧 RoutingPlan"**）· **§4.6**（**R4** = 用户干预**本身即授权**；
MODIFY 属 R4，**不新增 R7**）· **Authorization Context Builder 节点行**（"用户当前授权"是输入；
聚合在 Judgment 之前；不新增节点编号）· **`INV-02`** / **`INV-03`** · **A13.5**（显式事实注入先例）。
范围、冻结、授权与实现记录见 `tests/behavior/README_step5b.md` **§126–§130**。

**本模块要守住的五条主张：**

  1. **唯一载体**：`ModifyRerouteRequest(` 只有一处构造点，两实参逐字透传；
  2. **carrier 不是 producer**：**不重建** `PolicyFacts`（身份级证据：`request.policy_facts is 入参`）；
  3. **carrier 不是解释器**：对 `modification` **零取值**（连 `.strip()` / 空检查都不做）；
  4. **carrier 不是 Policy 执行入口**：不构造 `Policy`、不调用 `resolve`；
  5. **三类非法输入显式失败**：挂起（`decision is None`）· 非 `GateDecision` · 非 `MODIFY`（含 APPROVE）。

⚠️ **禁止子串扫描**：本文件 docstring 里**故意提及** `modification` / `Policy` / `resolve` /
`close_run` 等词 ⇒ 所有锁一律 **AST 级**（`referenced_names` 只看 AST，提及不算引用）。
⚠️ 敏感性 probe 一律在**源码字符串**上完成（``_mutate``），**不覆写** ``src/`` 里的真实文件；
真实源码的一次性变异证据见 README §130.5。
"""

from __future__ import annotations

import dataclasses
import hashlib
import pathlib

import pytest

from adaptive_router.approval_gate_wiring import (
    GateDecision,
    GateDecisionKind,
    GateOutcome,
)
from adaptive_router.policy import PolicyFacts
from adaptive_router.recovery_modify_wiring import (
    REROUTE_RATIONALE,
    ModifyRerouteRequest,
    RecoveryModifyWiringError,
    build_modify_reroute_request,
)

from _recovery_modify_locks import (
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
WIRING_PATH = SRC / "recovery_modify_wiring.py"
WIRING = WIRING_PATH.read_text(encoding="utf-8")

#: LRM-2 —— `ModifyRerouteRequest(` 的**两个实参逐字形态**。
EXPECTED_REQUEST_SHAPES = {
    "policy_facts": ("_require_policy_facts(policy_facts)",),
    "rationale": ("REROUTE_RATIONALE",),
}

#: §128.3 —— 请求**恰好两个字段**（多一个都不许）。
EXPECTED_REQUEST_FIELDS = ("policy_facts", "rationale")

#: LRM-2 —— 模块内允许的绑定：`__all__` · 固定措辞常量。
#:
#: ⚠️ **函数体内零局部绑定**是本条锁的核心：任何 `x = ...` 式**内部覆盖**都会让集合变化 ⇒ 红。
#: ⚠️ 枚举成员（`APPROVE` / `DENY` / `MODIFY`）属 `GateDecisionKind` 的**类体**（C2b-2a 的载体），
#: 不在本模块 ⇒ 它们**不出现**在这里（与 `_approval_gate_locks.py` 的同类常量不同 —— 那一轮的枚举
#: 定义在本模块内）。本模块**不定义任何枚举**。
EXPECTED_BINDINGS = {"__all__", "REROUTE_RATIONALE"}

#: 结构 —— 模块级函数清单（恰好四个，不得增删）。
EXPECTED_FUNCTIONS = (
    "_require_gate_outcome",
    "_require_modify_decision",
    "_require_policy_facts",
    "build_modify_reroute_request",
)

#: LRM-9 —— 依赖白名单。相对 import 的**路径段**也会进入 token 集合
#: （`from .policy import PolicyFacts` ⇒ `policy` + `PolicyFacts`），因此期望值把它们**显式列出**。
FUTURE_TOKENS = {"__future__", "annotations"}
ALLOWED_STDLIB = {"dataclasses", "dataclass"}
EXPECTED_IMPORT_MODULES = ALLOWED_STDLIB | {"approval_gate_wiring", "policy"}
EXPECTED_IMPORTED_NAMES = {"GateDecision", "GateDecisionKind", "GateOutcome", "PolicyFacts", "dataclass"}

#: LRM-6 —— **零取值**。字符串方法一旦出现即证明"文本被读过"。
STRING_METHODS = (
    "strip",
    "split",
    "lower",
    "upper",
    "startswith",
    "endswith",
    "replace",
    "find",
    "match",
    "search",
    "fullmatch",
)

#: LRM-7 / LRM-8 —— 本边一个都不许出现。
POLICY_EXECUTION_CALLS = ("Policy", "build_disclosure", "ApprovalJudge", "judge")
FORBIDDEN_IMPORT_TOKENS = (
    "router",
    "planner",
    "model_router",
    "cost",
    "risk",
    "approval_judgment",
    "approval_disclosure",
    "routing_plan",
    "recovery",
    "agent_manager",
    "scheduler",
    "execution",
    "terminal_wiring",
    "session",
    "entrypoint",
    "adapters",
)
CONTROL_CALLS = (
    "materialize",
    "commit_started",
    "record_outcome",
    "close_run",
    "apply_runtime_control",
    "schedule",
)
RUNTIME_NAMES = ("no_pending_approval", "control_flow_converged", "APPROVAL_DENIED", "attempt")

#: LRM 保护的受保护 artifact（十四件；基线见 README §129.11 / §130.2）。
PROTECTED_ARTIFACTS = {
    "src/adaptive_router/approval_gate_wiring.py": (
        "CEB2015F825763DF934FCCBF42DF8D94031B592F9BB4712F03A69409FA04CDAF"
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
    "src/adaptive_router/policy.py": (
        "14C5BEDD562E3F99C308F3CCFDA1FA717A08CB3FDA6970FF771B1F4C62ADFA08"
    ),
    "src/adaptive_router/router.py": (
        "B64F82C52C6FBFA1DC2D8EC5165F3328D0004BBEBA2424B518C02AC3FE1B07D3"
    ),
    "src/adaptive_router/session.py": (
        "A8483929115C1F197DB20E286550EDC558A392BFDC8603B4D387220F4D78C695"
    ),
    "src/adaptive_router/entrypoint.py": (
        "43B635D937D228421B6902CF5B157B79A2476FA6DDB1BCDE7FDB5B65DC00361C"
    ),
    "src/adaptive_router/agent_manager.py": (
        "154B171E0F2A7D568FCD61318869EEA05DA0D9A4140640A6A89860D13D0D01C5"
    ),
    "src/adaptive_router/recovery_route_wiring.py": (
        "B63EFD125DD1039D91F8FE839941D8BAF308C26BF748CA44B1FA79E625622EC3"
    ),
    "src/adaptive_router/recovery_regate_wiring.py": (
        "3CDFB4C1AEB5BDA70769F2CC5B8D14EB465437BBA378FF47E4FBFA67748B14C7"
    ),
    "src/adaptive_router/terminal_wiring.py": (
        "2B317FEABD72313465CDBE86659E58ED35CBDC24EE7CBD694AEAB357C92C0691"
    ),
    "src/adaptive_router/cost/ledger_wiring.py": (
        "6F0F3DBCA6970F8873115AB672434A49D6227F017A8BCD55F7B6075B431F0B7F"
    ),
    "tests/behavior/_recovery_regate_locks.py": (
        "F260F8514119F14A34C31758EE34590012E27E2ABCD3E4232A3211430262897D"
    ),
    "tests/behavior/test_recovery_route_wiring.py": (
        "5F0461EAB26AD496126D31B231E226AB5978D802D237B7594521DEC7AC47774F"
    ),
    "tests/behavior/test_recovery_regate_wiring.py": (
        "CFBB478D74FFEF6936E96FC3EB8169BFADE358E6DF39F0A625574B28C88E9826"
    ),
    "tests/behavior/test_approval_gate_wiring.py": (
        "FBD7CE0A3E55233A910D34872005F74C5A27F5D239543DE66FA75A20DE324B67"
    ),
    "src/adaptive_router/contracts/decision.py": (
        "CD65DCC9DA19557B158980FC9B2CA70DF3E90BF583FBCC41DD4BD96A09FED93E"
    ),
    "src/adaptive_router/contracts/common.py": (
        "AD9BE8FBA26B6F908CC8F3C197D49CB52D64AF833D14272CECC8FE173D9C4168"
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
# 测试替身：**已重建的** PolicyFacts（边界外事实）+ C2b-2a 的 GateOutcome
# --------------------------------------------------------------------------


def policy_facts(*, read_only: bool = True) -> PolicyFacts:
    """**边界外重建后的**新授权事实（本边只透传；不构造、不修改、不合并）。"""
    return PolicyFacts(
        session_authorized=True,
        system_denied=(),
        developer_denied=(),
        safety_denied=(),
        platform_denied=(),
        user_denied=(),
        project_denied=(),
        user_granted_scopes=("read",),
        user_commands=(),
        user_read_only_override=read_only,
        project_model_preference=None,
        project_approval_preference=None,
        protocol_default_mode="observe",
    )


def gate_outcome(decision: GateDecision | None) -> GateOutcome:
    """C2b-2a 的恒定载体（四项清单 + 透传位 + 决策位）。"""
    return GateOutcome(
        needs_approval=True,
        proposed_action="写入 docs/ 下的两个文件",
        impact_scope="仅限本仓库 docs/",
        main_risk="文档与实现不一致",
        recovery_method="从 git 恢复上一版本文档",
        decision=decision,
    )


MODIFY_DECISION = GateDecision(
    kind=GateDecisionKind.MODIFY, modification="可以，但只允许读、不允许写"
)


# ==========================================================================
# LRM-1 —— 唯一载体
# ==========================================================================


class TestLRM1SingleCarrier:
    def test_request_is_constructed_exactly_once(self) -> None:
        assert len(calls_to(WIRING, ("ModifyRerouteRequest",))) == 1

    def test_the_only_construction_site_is_the_entry(self) -> None:
        assert set(call_keyword_shapes_by_caller(WIRING, "ModifyRerouteRequest")) == {
            "build_modify_reroute_request"
        }

    def test_probe_second_construction_site_goes_red(self) -> None:
        variant = _mutate(
            "    _require_modify_decision(_require_gate_outcome(gate_outcome))",
            "    ModifyRerouteRequest(policy_facts=policy_facts, rationale=REROUTE_RATIONALE)\n"
            "    _require_modify_decision(_require_gate_outcome(gate_outcome))",
        )
        assert len(calls_to(variant, ("ModifyRerouteRequest",))) == 2


# ==========================================================================
# LRM-2 —— 只搬运 / 只组装（形态 + 零局部绑定）
# ==========================================================================


class TestLRM2PassthroughOnly:
    def test_the_two_arguments_are_passthrough_and_fixed_wording(self) -> None:
        shapes = call_keyword_shapes_by_caller(WIRING, "ModifyRerouteRequest")
        assert shapes == {"build_modify_reroute_request": EXPECTED_REQUEST_SHAPES}

    def test_no_star_star_escape_hatch(self) -> None:
        shapes = call_keyword_shapes_by_caller(WIRING, "ModifyRerouteRequest")
        assert "**" not in shapes["build_modify_reroute_request"]

    def test_the_module_has_no_unexpected_bindings(self) -> None:
        bound = set(assignment_value_shapes(WIRING)) | set(annotated_name_targets(WIRING))
        assert bound == EXPECTED_BINDINGS

    def test_probe_internal_override_of_facts_goes_red(self) -> None:
        variant = _mutate(
            "    _require_modify_decision(_require_gate_outcome(gate_outcome))",
            "    policy_facts = PolicyFacts(**{})\n"
            "    _require_modify_decision(_require_gate_outcome(gate_outcome))",
        )
        bound = set(assignment_value_shapes(variant)) | set(
            annotated_name_targets(variant)
        )
        assert bound != EXPECTED_BINDINGS

    def test_probe_rationale_rewritten_from_text_goes_red(self) -> None:
        variant = _mutate(
            "        rationale=REROUTE_RATIONALE,",
            "        rationale=gate_outcome.decision.modification,",
        )
        assert call_keyword_shapes_by_caller(variant, "ModifyRerouteRequest") != {
            "build_modify_reroute_request": EXPECTED_REQUEST_SHAPES
        }


# ==========================================================================
# LRM-3 —— MODIFY 专用入口 + 不重判 kind
# ==========================================================================


class TestLRM3ModifyOnlyEntry:
    def test_kind_is_never_reassigned(self) -> None:
        assert "kind" not in assignment_value_shapes(WIRING)

    def test_no_gate_dto_is_constructed_here(self) -> None:
        assert calls_to(WIRING, ("GateDecision", "GateOutcome")) == []

    def test_the_modify_guard_is_present(self) -> None:
        assert (
            "gate_outcome.decision.kind is not GateDecisionKind.MODIFY",
            "RecoveryModifyWiringError",
        ) in function_raise_guards(WIRING, "_require_modify_decision")
        assert (
            "gate_outcome.decision is None",
            "RecoveryModifyWiringError",
        ) in function_raise_guards(WIRING, "_require_modify_decision")
        assert (
            "not isinstance(gate_outcome.decision, GateDecision)",
            "RecoveryModifyWiringError",
        ) in function_raise_guards(WIRING, "_require_modify_decision")

    def test_probe_dropped_modify_guard_goes_red(self) -> None:
        variant = _mutate(
            "    if gate_outcome.decision.kind is not GateDecisionKind.MODIFY:",
            "    if False:",
        )
        assert (
            "gate_outcome.decision.kind is not GateDecisionKind.MODIFY",
            "RecoveryModifyWiringError",
        ) not in function_raise_guards(variant, "_require_modify_decision")


# ==========================================================================
# LRM-4 —— 身份守卫
# ==========================================================================


class TestLRM4IdentityGuards:
    def test_outcome_guard_present(self) -> None:
        assert (
            "not isinstance(gate_outcome, GateOutcome)",
            "RecoveryModifyWiringError",
        ) in function_raise_guards(WIRING, "_require_gate_outcome")

    def test_facts_guard_present(self) -> None:
        assert (
            "not isinstance(policy_facts, PolicyFacts)",
            "RecoveryModifyWiringError",
        ) in function_raise_guards(WIRING, "_require_policy_facts")

    def test_probe_dropped_outcome_guard_goes_red(self) -> None:
        variant = _mutate(
            "    if not isinstance(gate_outcome, GateOutcome):",
            "    if False:",
        )
        assert (
            "not isinstance(gate_outcome, GateOutcome)",
            "RecoveryModifyWiringError",
        ) not in function_raise_guards(variant, "_require_gate_outcome")


# ==========================================================================
# LRM-5 —— 不重建 PolicyFacts（结构 + 身份级证据）
# ==========================================================================


class TestLRM5NoFactsRebuild:
    def test_no_policy_facts_is_ever_constructed(self) -> None:
        assert calls_to(WIRING, ("PolicyFacts",)) == []

    def test_no_dataclasses_replace_or_field_mutation(self) -> None:
        assert calls_to(WIRING, ("replace", "fields", "asdict", "astuple")) == []

    def test_no_facts_field_name_appears_in_assignment_shape(self) -> None:
        shapes = assignment_value_shapes(WIRING)
        for field in ("user_read_only_override", "user_commands", "session_authorized"):
            assert field not in shapes, field

    def test_probe_rebuilt_facts_goes_red(self) -> None:
        variant = _mutate(
            "    _require_modify_decision(_require_gate_outcome(gate_outcome))",
            "    policy_facts = PolicyFacts(**{})\n"
            "    _require_modify_decision(_require_gate_outcome(gate_outcome))",
        )
        assert calls_to(variant, ("PolicyFacts",)) != []


# ==========================================================================
# LRM-6 —— 零取值（`modification access = 0`）
# ==========================================================================


class TestLRM6ZeroRead:
    def test_modification_is_never_referenced(self) -> None:
        assert "modification" not in referenced_names(WIRING)

    def test_no_attribute_is_named_modification(self) -> None:
        assert all(
            "modification" not in token
            for token in call_keyword_shapes_by_caller(WIRING, "ModifyRerouteRequest")
            .get("build_modify_reroute_request", {})
            .keys()
        )

    def test_no_string_method_is_called(self) -> None:
        assert calls_to(WIRING, STRING_METHODS) == []

    def test_no_regex_module_is_imported(self) -> None:
        assert "re" not in imported_module_tokens(WIRING)

    def test_probe_reading_the_text_goes_red(self) -> None:
        variant = _mutate(
            "    if gate_outcome.decision is None:",
            "    _read = gate_outcome.decision.modification.strip()\n"
            "    if gate_outcome.decision is None:",
        )
        assert "modification" in referenced_names(variant)
        assert calls_to(variant, ("strip",)) != []


# ==========================================================================
# LRM-7 —— 不执行 Policy / 不重算
# ==========================================================================


class TestLRM7NoPolicyExecution:
    def test_policy_is_never_constructed(self) -> None:
        assert calls_to(WIRING, ("Policy",)) == []

    def test_no_resolve_call(self) -> None:
        assert calls_to(WIRING, ("resolve",)) == []

    def test_no_downstream_decision_entry_is_touched(self) -> None:
        assert calls_to(WIRING, POLICY_EXECUTION_CALLS) == []
        names = referenced_names(WIRING)
        for token in ("ApprovalJudge", "build_disclosure"):
            assert token not in names, token

    def test_probe_policy_execution_goes_red(self) -> None:
        variant = _mutate(
            "    _require_modify_decision(_require_gate_outcome(gate_outcome))",
            "    Policy().resolve(policy_facts)\n"
            "    _require_modify_decision(_require_gate_outcome(gate_outcome))",
        )
        assert calls_to(variant, ("Policy",)) != []
        assert calls_to(variant, ("resolve",)) != []


# ==========================================================================
# LRM-8 —— 不写运行态
# ==========================================================================


class TestLRM8NoRuntimeWrite:
    def test_no_control_method_is_called(self) -> None:
        assert calls_to(WIRING, CONTROL_CALLS) == []

    def test_no_runtime_name_appears(self) -> None:
        names = referenced_names(WIRING)
        for name in RUNTIME_NAMES:
            assert name not in names, name

    def test_no_runtime_module_is_imported(self) -> None:
        assert forbidden_import_hits(WIRING, FORBIDDEN_IMPORT_TOKENS) == set()

    def test_probe_runtime_write_goes_red(self) -> None:
        variant = _mutate(
            "    if gate_outcome.decision is None:",
            "    _ = attempt + 1\n    if gate_outcome.decision is None:",
        )
        assert "attempt" in referenced_names(variant)


# ==========================================================================
# LRM-9 —— 无时钟 / 无状态 / 无 IO · 白名单精确
# ==========================================================================


class TestLRM9WhitelistAndShape:
    def test_no_clock_no_state_no_io(self) -> None:
        assert clock_call_hits(WIRING) == []
        assert module_level_container_assignments(WIRING) == ()
        assert annotated_assign_names(WIRING) == ()
        assert attribute_assignment_targets(WIRING) == ()
        assert open_calls(WIRING) == []
        assert none_coalescing_shapes(WIRING) == ()

    def test_import_whitelist_is_exact(self) -> None:
        tokens = imported_module_tokens(WIRING) - FUTURE_TOKENS
        assert tokens == EXPECTED_IMPORT_MODULES
        assert imported_names(WIRING) - FUTURE_TOKENS == EXPECTED_IMPORTED_NAMES

    def test_function_inventory_signatures_and_return(self) -> None:
        assert module_level_function_names(WIRING) == EXPECTED_FUNCTIONS
        assert function_signature_shape(WIRING, "build_modify_reroute_request") == {
            "positional": [],
            "kwonly": ["gate_outcome", "policy_facts"],
            "defaults": [False, False],
            "vararg": None,
            "kwarg": None,
        }
        assert (
            function_return_annotation(WIRING, "build_modify_reroute_request")
            == "ModifyRerouteRequest"
        )
        assert len(function_return_shapes(WIRING, "build_modify_reroute_request")) == 1

    def test_probe_extra_import_goes_red(self) -> None:
        variant = _mutate(
            "from .policy import PolicyFacts",
            "import os\nfrom .policy import PolicyFacts",
        )
        assert imported_module_tokens(variant) - FUTURE_TOKENS != EXPECTED_IMPORT_MODULES


# ==========================================================================
# LRM 保护的受保护 artifact（十四件）
# ==========================================================================




# ==========================================================================
# 行为测试 B1 – B6
# ==========================================================================


class TestBuildModifyRerouteRequest:
    def test_b1_facts_are_carried_by_identity(self) -> None:
        """**身份级证据**：请求里的 facts **就是**入参那个对象（不是重建的同值对象）。"""
        facts = policy_facts()
        request = build_modify_reroute_request(
            gate_outcome=gate_outcome(MODIFY_DECISION), policy_facts=facts
        )
        assert request.policy_facts is facts

    def test_b2_pending_is_an_explicit_failure(self) -> None:
        with pytest.raises(RecoveryModifyWiringError):
            build_modify_reroute_request(
                gate_outcome=gate_outcome(None), policy_facts=policy_facts()
            )

    def test_b3_approve_and_deny_are_explicit_failures(self) -> None:
        for kind in (GateDecisionKind.APPROVE, GateDecisionKind.DENY):
            with pytest.raises(RecoveryModifyWiringError):
                build_modify_reroute_request(
                    gate_outcome=gate_outcome(GateDecision(kind=kind, modification=None)),
                    policy_facts=policy_facts(),
                )

    def test_b4_wrong_types_are_explicit_failures(self) -> None:
        for bad in ("nope", None, 1, object()):
            with pytest.raises(RecoveryModifyWiringError):
                build_modify_reroute_request(
                    gate_outcome=bad, policy_facts=policy_facts()  # type: ignore[arg-type]
                )
            with pytest.raises(RecoveryModifyWiringError):
                build_modify_reroute_request(
                    gate_outcome=gate_outcome(MODIFY_DECISION),
                    policy_facts=bad,  # type: ignore[arg-type]
                )

    def test_b5_the_text_has_zero_effect_on_the_output(self) -> None:
        """**对文本零反应**：任意 `modification` 文本 ⇒ 除 facts 身份外输出形状完全相同。"""
        facts = policy_facts()
        first = build_modify_reroute_request(
            gate_outcome=gate_outcome(
                GateDecision(kind=GateDecisionKind.MODIFY, modification="可以，但只允许读")
            ),
            policy_facts=facts,
        )
        second = build_modify_reroute_request(
            gate_outcome=gate_outcome(
                GateDecision(kind=GateDecisionKind.MODIFY, modification="写数据库并推送")
            ),
            policy_facts=facts,
        )
        assert first.rationale == second.rationale == REROUTE_RATIONALE
        assert first.policy_facts is second.policy_facts is facts

    def test_b6_the_dto_carries_exactly_two_fields(self) -> None:
        request = build_modify_reroute_request(
            gate_outcome=gate_outcome(MODIFY_DECISION), policy_facts=policy_facts()
        )
        names = tuple(field.name for field in dataclasses.fields(ModifyRerouteRequest))
        assert names == EXPECTED_REQUEST_FIELDS
        assert "modification" not in request.rationale
        assert "只允许读" not in request.rationale
        with pytest.raises(dataclasses.FrozenInstanceError):
            request.rationale = "x"  # type: ignore[misc]
