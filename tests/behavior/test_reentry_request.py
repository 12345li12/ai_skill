"""``C2b-2b-2 再入载荷``（`ModifyRerouteRequest → ReentryRequest`）—— 结构锁 **LRE-1 – LRE-9** + 行为 **B1 – B7**。

架构依据：`docs/architecture.md` **Approval Gate 节点行**（MODIFY 出口逐字：
回 `[02]` Policy → 重建 AuthorizationContext → 重算 → Judgment → 必要时再次 Gate；
**"永远不能直接执行旧 RoutingPlan"**）· **§4.6**（**R4** = 用户干预**本身即授权**；不新增 R7）·
**Authorization Context Builder 节点行**（"用户当前授权"是输入；不新增节点编号）·
**`INV-03`** · **`INV-16`** · **A13.5**（显式事实注入先例）。
范围、冻结、授权与实现记录见 `tests/behavior/README_step5b.md` **§132–§136**。

**本模块要守住的六条主张：**

  1. **入口形状不变**：本边不 import `router`、不构造 `AdaptiveRouter`（`__call__` 逐字节不变）；
  2. **不重建 facts**：`PolicyFacts(` = 0，且 `request` 按**身份**透传；
  3. **不执行 Policy**：`Policy(` / `resolve` = 0（全仓 `resolve` 调用点仍恰好 1）；
  4. **attempt 只读入**：不是赋值目标 · 不引用运行态 · 不 import `agent_manager`；
  5. **task / context identity-preserving**：`is` 关系，不 copy / 不 normalize；
  6. **载荷恰好四字段**：`request` / `task` / `context` / `attempt`（无第二 facts、无 `rationale`）。

⚠️ **禁止子串扫描**：本文件 docstring 里**故意提及** `Policy` / `resolve` / `attempt` / `close_run`
等词 ⇒ 所有锁一律 **AST 级**（`referenced_names` 只看 AST，提及不算引用）。
⚠️ 敏感性 probe 一律在**源码字符串**上完成（``_mutate``），**不覆写** ``src/`` 里的真实文件；
真实源码的一次性变异证据见 README §136.5。
"""

from __future__ import annotations

import dataclasses
import hashlib
import pathlib

import pytest

from adaptive_router.contracts import ProjectContext
from adaptive_router.policy import PolicyFacts
from adaptive_router.recovery_modify_wiring import (
    ModifyRerouteRequest,
    RecoveryModifyWiringError,
)
from adaptive_router.reentry_request import (
    ReentryRequest,
    ReentryWiringError,
    build_reentry_request,
)

from _reentry_locks import (
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
WIRING_PATH = SRC / "reentry_request.py"
WIRING = WIRING_PATH.read_text(encoding="utf-8")
ROUTER_PATH = SRC / "router.py"
ROUTER = ROUTER_PATH.read_text(encoding="utf-8")

#: LRE-6 —— `ReentryRequest(` 的**四个实参逐字形态**。
EXPECTED_REQUEST_SHAPES = {
    "request": ("_require_modify_request(request)",),
    "task": ("task",),
    "context": ("_require_task_and_context(task, context)",),
    "attempt": ("_require_attempt(attempt)",),
}

#: §134.2 —— 载荷**恰好四个字段**（多一个都不许）。
EXPECTED_REQUEST_FIELDS = ("request", "task", "context", "attempt")

#: LRE-6 —— 模块内允许的绑定：只有 `__all__`（**函数体内零局部绑定**）。
EXPECTED_BINDINGS = {"__all__"}

#: 结构 —— 模块级函数清单（恰好四个，不得增删；§135.3 已批准）。
EXPECTED_FUNCTIONS = (
    "_require_modify_request",
    "_require_task_and_context",
    "_require_attempt",
    "build_reentry_request",
)

#: LRE-9 —— 依赖白名单。
FUTURE_TOKENS = {"__future__", "annotations"}
ALLOWED_STDLIB = {"dataclasses", "dataclass"}
EXPECTED_IMPORT_MODULES = ALLOWED_STDLIB | {"contracts", "recovery_modify_wiring"}
EXPECTED_IMPORTED_NAMES = {"ProjectContext", "ModifyRerouteRequest", "dataclass"}

#: LRE-5 / LRE-7 —— 本边一个都不许出现。
FORBIDDEN_IMPORT_TOKENS = (
    "router",
    "planner",
    "model_router",
    "cost",
    "risk",
    "approval_judgment",
    "approval_disclosure",
    "approval_gate_wiring",
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
RUNTIME_NAMES = ("RoutingRun", "AgentRun", "BranchState", "no_pending_approval", "ApprovalJudge")
#: LRE-4 —— 本边绝不构造 / 调用 Policy（只允许 `PolicyFacts` 作为**类型**出现）。
POLICY_EXECUTION_CALLS = ("Policy", "resolve", "build_disclosure", "judge")

#: LRE-1 —— 冻结入口形状（**只读**断言既有源码，不修改它）。
EXPECTED_CALL_PARAMETERS = ["self", "task", "context"]

#: LRE-7 —— 受保护 artifact（**十九件**：十二交付物 + 三个冻结节点 + 契约 + 架构 + 入口形状锁）。
PROTECTED_ARTIFACTS = {
    "src/adaptive_router/recovery_modify_wiring.py": (
        "D56E81F45858161060941A51E10C0D828D6A8E59441E0025882037E3AEEECCEB"
    ),
    "src/adaptive_router/approval_gate_wiring.py": (
        "CEB2015F825763DF934FCCBF42DF8D94031B592F9BB4712F03A69409FA04CDAF"
    ),
    "src/adaptive_router/recovery_regate_wiring.py": (
        "3CDFB4C1AEB5BDA70769F2CC5B8D14EB465437BBA378FF47E4FBFA67748B14C7"
    ),
    "src/adaptive_router/recovery_route_wiring.py": (
        "B63EFD125DD1039D91F8FE839941D8BAF308C26BF748CA44B1FA79E625622EC3"
    ),
    "src/adaptive_router/terminal_wiring.py": (
        "2B317FEABD72313465CDBE86659E58ED35CBDC24EE7CBD694AEAB357C92C0691"
    ),
    "src/adaptive_router/cost/ledger_wiring.py": (
        "6F0F3DBCA6970F8873115AB672434A49D6227F017A8BCD55F7B6075B431F0B7F"
    ),
    "src/adaptive_router/router.py": (
        "B64F82C52C6FBFA1DC2D8EC5165F3328D0004BBEBA2424B518C02AC3FE1B07D3"
    ),
    "src/adaptive_router/policy.py": (
        "14C5BEDD562E3F99C308F3CCFDA1FA717A08CB3FDA6970FF771B1F4C62ADFA08"
    ),
    "src/adaptive_router/agent_manager.py": (
        "154B171E0F2A7D568FCD61318869EEA05DA0D9A4140640A6A89860D13D0D01C5"
    ),
    "src/adaptive_router/approval_judgment.py": (
        "E38435FD3744D0DE5215C5075A1C3413F960705BA61D906E1584544201C4AAD6"
    ),
    "src/adaptive_router/contracts/decision.py": (
        "CD65DCC9DA19557B158980FC9B2CA70DF3E90BF583FBCC41DD4BD96A09FED93E"
    ),
    "src/adaptive_router/contracts/common.py": (
        "AD9BE8FBA26B6F908CC8F3C197D49CB52D64AF833D14272CECC8FE173D9C4168"
    ),
    "tests/behavior/_recovery_modify_locks.py": (
        "E6A0495005CA86287435A15005044A4B0C3FDA6C9F6DF4B002105100461FBCF0"
    ),
    "tests/behavior/test_recovery_modify_wiring.py": (
        "FFA5278A4F7515749619ACF4D870E90FCC800690283C57B0BF7CE3EE6F9324EB"
    ),
    "tests/behavior/test_router.py": (
        "27BFAA27D11D72C4579479E7AD764C317DFA260E6FC40F140D54F8EEF31C4F28"
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
# 测试替身：C2b-2b-1 的请求（含**完整最终态** PolicyFacts）+ 同源 task / context
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


def modify_request() -> ModifyRerouteRequest:
    """C2b-2b-1 的恒定载体（唯一 producer ⇒ 其 `policy_facts` 是唯一事实来源）。"""
    return ModifyRerouteRequest(
        policy_facts=policy_facts(),
        rationale="本次授权事实来自 GateOutcome（Approval Gate 的 MODIFY 出口，属 R4）"
        "⇒ 从 [02] Policy 重新进入决策主链",
    )


def project_context() -> ProjectContext:
    """**同源**项目上下文（identity-preserving 的另一端）。

    ⚠️ 字段与既有夹具（`test_evaluator.py:111-129`）同口径：`ProjectContext` 的 14 个必填字段
    全部显式给出 ⇒ 本边**不重建**它，只把它**按身份**透传。
    """
    return ProjectContext(
        root="/repo",
        present=True,
        boundary_dirs=("src",),
        writable=(),
        denied=(),
        readme_refs=("README.md",),
        agents_md_present=False,
        agents_md_digest=None,
        test_commands=(),
        high_risk_files=(),
        model_preference=None,
        approval_preference=None,
        source_path=None,
        read_at="2026-09-20T00:00:00Z",
    )


TASK = object()


def build(*, request=None, task=TASK, context=None, attempt=2) -> ReentryRequest:
    """便利构造：默认给出**合法**输入（同源 task / context + attempt=2）。"""
    return build_reentry_request(
        request=modify_request() if request is None else request,
        task=task,
        context=project_context() if context is None else context,
        attempt=attempt,
    )


# ==========================================================================
# LRE-1 —— 入口形状不变（只读断言既有源码）
# ==========================================================================


class TestLRE1FrozenEntryShape:
    def test_the_frozen_call_parameters_are_unchanged(self) -> None:
        import ast

        tree = ast.parse(ROUTER)
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == "AdaptiveRouter":
                for child in node.body:
                    if isinstance(child, ast.FunctionDef) and child.name == "__call__":
                        params = [a.arg for a in (*child.args.posonlyargs, *child.args.args)]
                        assert params == EXPECTED_CALL_PARAMETERS
                        return
        raise AssertionError("router.py 里找不到 AdaptiveRouter.__call__")

    def test_this_edge_never_touches_the_router(self) -> None:
        assert forbidden_import_hits(WIRING, ("router", "entrypoint", "session")) == set()
        assert calls_to(WIRING, ("AdaptiveRouter", "RouterFacts", "RouterOutcome")) == []

    def test_probe_adding_an_entry_parameter_goes_red(self) -> None:
        import ast

        variant_source = ROUTER.replace(
            "    def __call__(self, task: Any, context: ProjectContext) -> RouterOutcome:",
            "    def __call__(self, task: Any, context: ProjectContext, attempt: int = 1)"
            " -> RouterOutcome:",
        )
        assert variant_source != ROUTER
        tree = ast.parse(variant_source)
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == "AdaptiveRouter":
                for child in node.body:
                    if isinstance(child, ast.FunctionDef) and child.name == "__call__":
                        params = [a.arg for a in (*child.args.posonlyargs, *child.args.args)]
                        assert params != EXPECTED_CALL_PARAMETERS
                        return
        raise AssertionError("变异未生效")


# ==========================================================================
# LRE-2 / LRE-3 —— facts 来源与身份
# ==========================================================================


class TestLRE2And3FactsSourceAndIdentity:
    def test_no_policy_facts_is_ever_constructed(self) -> None:
        assert calls_to(WIRING, ("PolicyFacts",)) == []

    def test_no_dataclasses_replace_or_field_mutation(self) -> None:
        assert calls_to(WIRING, ("replace", "fields", "asdict", "astuple")) == []

    def test_no_facts_field_name_appears_in_assignment_shape(self) -> None:
        shapes = assignment_value_shapes(WIRING)
        for field in ("user_read_only_override", "user_commands", "session_authorized"):
            assert field not in shapes, field

    def test_the_request_is_passed_through_by_identity(self) -> None:
        """**身份级证明**（LRE-3）：载荷里的 request **就是**入参那个对象。"""
        request = modify_request()
        out = build(request=request)
        assert out.request is request

    def test_probe_rebuilt_facts_goes_red(self) -> None:
        variant = _mutate(
            "    return ReentryRequest(",
            "    PolicyFacts(**{})\n    return ReentryRequest(",
        )
        assert calls_to(variant, ("PolicyFacts",)) != []


# ==========================================================================
# LRE-4 —— Policy 入口唯一性
# ==========================================================================


class TestLRE4PolicyEntryUniqueness:
    def test_policy_is_never_constructed_or_resolved(self) -> None:
        assert calls_to(WIRING, POLICY_EXECUTION_CALLS) == []

    def test_there_is_still_exactly_one_resolve_call_site_in_src(self) -> None:
        hits: list[str] = []
        for path in SRC.rglob("*.py"):
            if ".resolve(" in path.read_text(encoding="utf-8"):
                hits.append(path.name)
        assert hits == ["router.py"]

    def test_probe_calling_resolve_goes_red(self) -> None:
        variant = _mutate(
            "    return ReentryRequest(",
            "    Policy().resolve(request.policy_facts)\n    return ReentryRequest(",
        )
        assert calls_to(variant, ("Policy",)) != []
        assert calls_to(variant, ("resolve",)) != []


# ==========================================================================
# LRE-5 —— attempt 边界（只读入，不写入）
# ==========================================================================


class TestLRE5AttemptBoundary:
    def test_attempt_is_never_an_assignment_target(self) -> None:
        assert "attempt" not in assignment_value_shapes(WIRING)
        assert "attempt" not in annotated_name_targets(WIRING)

    def test_no_runtime_name_or_control_call_appears(self) -> None:
        names = referenced_names(WIRING)
        for name in RUNTIME_NAMES:
            assert name not in names, name
        assert calls_to(WIRING, CONTROL_CALLS) == []

    def test_the_two_attempt_guards_are_present(self) -> None:
        assert (
            "type(attempt) is not int or attempt < 2",
            "ReentryWiringError",
        ) in function_raise_guards(WIRING, "_require_attempt")

    def test_probe_attempt_becomes_an_assignment_target(self) -> None:
        variant = _mutate(
            "    return ReentryRequest(",
            "    attempt = attempt\n    return ReentryRequest(",
        )
        assert "attempt" in assignment_value_shapes(variant)

    def test_probe_runtime_derivation_goes_red(self) -> None:
        variant = _mutate(
            "    return ReentryRequest(",
            "    from .agent_manager import AgentManager\n    _ = AgentManager\n    return ReentryRequest(",
        )
        assert forbidden_import_hits(variant, ("agent_manager",)) != set()


# ==========================================================================
# LRE-6 —— 只搬运 / 零局部绑定
# ==========================================================================


class TestLRE6PassthroughOnly:
    def test_request_is_constructed_exactly_once(self) -> None:
        assert len(calls_to(WIRING, ("ReentryRequest",))) == 1

    def test_the_only_construction_site_is_the_entry(self) -> None:
        assert set(call_keyword_shapes_by_caller(WIRING, "ReentryRequest")) == {
            "build_reentry_request"
        }

    def test_the_four_arguments_are_passthrough_and_guarded(self) -> None:
        shapes = call_keyword_shapes_by_caller(WIRING, "ReentryRequest")
        assert shapes == {"build_reentry_request": EXPECTED_REQUEST_SHAPES}

    def test_no_star_star_escape_hatch(self) -> None:
        shapes = call_keyword_shapes_by_caller(WIRING, "ReentryRequest")
        assert "**" not in shapes["build_reentry_request"]

    def test_the_module_has_no_local_bindings(self) -> None:
        bound = set(assignment_value_shapes(WIRING)) | set(annotated_name_targets(WIRING))
        assert bound == EXPECTED_BINDINGS

    def test_probe_second_construction_site_goes_red(self) -> None:
        variant = _mutate(
            "    return ReentryRequest(",
            "    ReentryRequest(request=request, task=task, context=context, attempt=attempt)\n"
            "    return ReentryRequest(",
        )
        assert len(calls_to(variant, ("ReentryRequest",))) == 2

    def test_probe_intermediate_binding_goes_red(self) -> None:
        variant = _mutate(
            "    return ReentryRequest(",
            "    normalized_task = task\n    return ReentryRequest(",
        )
        bound = set(assignment_value_shapes(variant)) | set(
            annotated_name_targets(variant)
        )
        assert bound != EXPECTED_BINDINGS


# ==========================================================================
# LRE-7 —— 受保护 artifact（十九件）
# ==========================================================================




# ==========================================================================
# LRE-8 / LRE-9 —— 无时钟无状态 / 依赖白名单
# ==========================================================================


class TestLRE8NoClockNoState:
    def test_no_clock_no_state_no_io(self) -> None:
        assert clock_call_hits(WIRING) == []
        assert module_level_container_assignments(WIRING) == ()
        assert annotated_assign_names(WIRING) == ()
        assert attribute_assignment_targets(WIRING) == ()
        assert open_calls(WIRING) == []
        assert none_coalescing_shapes(WIRING) == ()


class TestLRE9WhitelistAndShape:
    def test_import_whitelist_is_exact(self) -> None:
        tokens = imported_module_tokens(WIRING) - FUTURE_TOKENS
        assert tokens == EXPECTED_IMPORT_MODULES
        assert imported_names(WIRING) - FUTURE_TOKENS == EXPECTED_IMPORTED_NAMES

    def test_no_forbidden_import(self) -> None:
        assert forbidden_import_hits(WIRING, FORBIDDEN_IMPORT_TOKENS) == set()

    def test_function_inventory_signatures_and_return(self) -> None:
        assert module_level_function_names(WIRING) == EXPECTED_FUNCTIONS
        assert function_signature_shape(WIRING, "build_reentry_request") == {
            "positional": [],
            "kwonly": ["request", "task", "context", "attempt"],
            "defaults": [False, False, False, False],
            "vararg": None,
            "kwarg": None,
        }
        assert (
            function_return_annotation(WIRING, "build_reentry_request") == "ReentryRequest"
        )
        assert len(function_return_shapes(WIRING, "build_reentry_request")) == 1

    def test_probe_extra_import_goes_red(self) -> None:
        variant = _mutate(
            "from .contracts import ProjectContext",
            "import os\nfrom .contracts import ProjectContext",
        )
        assert imported_module_tokens(variant) - FUTURE_TOKENS != EXPECTED_IMPORT_MODULES


# ==========================================================================
# 行为测试 B1 – B7
# ==========================================================================


class TestBuildReentryRequest:
    def test_b1_the_request_is_carried_by_identity(self) -> None:
        """**身份级证明**：载荷里的 request **就是**入参 ⇒ 其 `policy_facts` 同一对象。"""
        request = modify_request()
        out = build(request=request)
        assert out.request is request
        assert out.request.policy_facts is request.policy_facts

    def test_b2_attempt_one_is_an_explicit_failure(self) -> None:
        with pytest.raises(ReentryWiringError):
            build(attempt=1)

    def test_b3_non_int_or_bool_attempts_are_explicit_failures(self) -> None:
        for bad in (True, False, 2.0, "2", None):
            with pytest.raises(ReentryWiringError):
                build(attempt=bad)  # type: ignore[arg-type]

    def test_b4_wrong_types_are_explicit_failures(self) -> None:
        with pytest.raises(ReentryWiringError):
            build(request="nope")  # type: ignore[arg-type]
        with pytest.raises(ReentryWiringError):
            build(request=object())  # type: ignore[arg-type]
        with pytest.raises(ReentryWiringError):
            build(context="nope")  # type: ignore[arg-type]
        with pytest.raises(ReentryWiringError):
            build(task=None)

    def test_b5_task_and_context_are_identity_preserving(self) -> None:
        task = object()
        context = project_context()
        out = build(task=task, context=context)
        assert out.task is task
        assert out.context is context

    def test_b6_the_dto_carries_exactly_four_fields(self) -> None:
        out = build()
        names = tuple(field.name for field in dataclasses.fields(ReentryRequest))
        assert names == EXPECTED_REQUEST_FIELDS
        assert not hasattr(out, "policy_facts")
        assert not hasattr(out, "rationale")
        with pytest.raises(dataclasses.FrozenInstanceError):
            out.attempt = 3  # type: ignore[misc]

    def test_b7_there_is_still_exactly_one_resolve_call_site_in_src(self) -> None:
        hits: list[str] = []
        for path in SRC.rglob("*.py"):
            if ".resolve(" in path.read_text(encoding="utf-8"):
                hits.append(path.name)
        assert hits == ["router.py"]

    def test_a_valid_reentry_payload_reports_the_second_attempt(self) -> None:
        out = build(attempt=2)
        assert out.attempt == 2

    def test_the_wiring_error_is_not_a_contract_type(self) -> None:
        assert issubclass(ReentryWiringError, RuntimeError)
        assert not issubclass(RecoveryModifyWiringError, ReentryWiringError)
