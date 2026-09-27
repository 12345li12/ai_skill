"""``[14] Recovery`` —— 结构锁 **L14-1 – L14-15** + 行为测试 B1 – B16。

架构依据：`docs/architecture.md` 2 节 ``[14] Recovery Router``（**Amendment A17**，FROZEN）：
A17.1（`failure_kind → FailureClass`）· A17.2（`FailureClass → RecoveryTarget`）·
A17.3（V1 可达 / 不可达）· A17.4（`carry_forward` 边界 + 分类位）· A17.5（`RecoveryContext` 三分归属）·
A17.6（身份链与 `attempt` 层级）· A17.7（收敛事实，`INV-20`）· A17.9（九项裁决登记）。
范围、锁清单与 probe 计划见 `tests/behavior/README_step5b.md` §83。

**本模块要守住的三条主张：**

  1. **表即唯一事实来源**：两张映射表（+ 措辞表 + 可达集）是全部规则；决策函数里**没有**散落的 `if/elif`；
  2. **`[14]` 只产决策**：不写运行态、不推进 Run 级 attempt、不碰收敛事实（`INV-20` 在 `[14]` 侧的机械形式 = 名称出现 = 0）；
  3. **`None` 只有一个出口**：仅"达到 `total_attempt_limit`"返回 `None`；其余路径一律**显式失败**。

⚠️ **禁止子串扫描**：本文件 docstring 里**故意提及** `control_flow_converged` / `CapabilityMismatch` /
`BranchState` 等词（说明边界）⇒ 所有锁一律 **AST 级**。
⚠️ 敏感性 probe 一律在**源码字符串**上完成，**不覆写** `src/` 里的真实文件；
真实源码的一次性变异证据见 README §84.5。
"""

from __future__ import annotations

import pathlib

import pytest

from adaptive_router.adapters import AdapterFailureKind
from adaptive_router.contracts import (
    Confidence,
    ContractError,
    FailureClass,
    RecoveryDecision,
    RecoveryTarget,
    ValidationRecord,
    new_branch_id,
)
from adaptive_router.recovery import (
    CARRY_FORWARD_TEXT,
    DECISION_CONFIDENCE,
    FAILURE_CLASS_TO_CORRECTION,
    FAILURE_CLASS_TO_TARGET,
    FAILURE_KIND_TO_CLASS,
    V1_REACHABLE,
    RecoveryRouterError,
    decide_recovery,
)
from adaptive_router.recovery_context import CarryForwardKind, RecoveryContext

from _recovery_locks import (
    annotated_assign_names,
    assigned_literal_items,
    calls_to,
    class_def_names,
    clock_call_hits,
    comparison_sites,
    dataclass_field_names,
    declared_public_names,
    exception_subclass_names,
    forbidden_import_hits,
    function_raise_guards,
    function_return_annotation,
    function_return_shapes,
    function_signature_shape,
    handled_exception_names,
    imported_module_tokens,
    mapping_get_calls,
    module_level_container_assignments,
    module_level_function_names,
    open_calls,
    raise_guards,
    referenced_names,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
SRC = REPO_ROOT / "src/adaptive_router"
RECOVERY_SOURCE = (SRC / "recovery.py").read_text(encoding="utf-8")
CONTEXT_SOURCE = (SRC / "recovery_context.py").read_text(encoding="utf-8")

#: L14-1 —— 两个模块的 import token **精确集合**（实测，见 README §84.3）。
ALLOWED_TOKENS_RECOVERY = frozenset(
    {"__future__", "annotations", "contracts", "recovery_context"}
)
ALLOWED_TOKENS_CONTEXT = frozenset(
    {
        "__future__",
        "annotations",
        "enum",
        "Enum",
        "dataclasses",
        "dataclass",
        "contracts",
        "common",
        "contracts.common",
    }
)

#: L14-1 —— 禁用清单（每条都有冻结依据，见 §83.3）。
FORBIDDEN_IMPORTS = (
    "adapters",
    "agent_manager",
    "scheduler",
    "execution",
    "execution_results",
    "result_handoff",
    "validation",
    "validation_judgment",
    "validation_wiring",
    "reusable_results",
    "cost",
    "policy",
    "router",
    "entrypoint",
    "capabilities",
    "project_context",
    "session",
    "context_builder",
)

#: L14-4 —— **A17.1**（`docs/architecture.md` 的 A17.1 表逐行转录；表 == 契约）。
EXPECTED_KIND_TO_CLASS = (
    "'crash': FailureClass.EXTERNAL_SERVICE",
    "'timeout': FailureClass.MODEL",
    "'refused': FailureClass.PERMISSION",
    "'network': FailureClass.EXTERNAL_SERVICE",
)

#: L14-4 —— **A17.2**（九行满射表逐行转录）。
EXPECTED_CLASS_TO_TARGET = (
    "FailureClass.INPUT: RecoveryTarget.CONTEXT",
    "FailureClass.PERMISSION: RecoveryTarget.PLANNER",
    "FailureClass.TOOL: RecoveryTarget.CAPABILITY",
    "FailureClass.MODEL: RecoveryTarget.MODEL_ROUTER",
    "FailureClass.EFFORT: RecoveryTarget.MODEL_ROUTER",
    "FailureClass.DECOMPOSITION: RecoveryTarget.PLANNER",
    "FailureClass.DEPENDENCY: RecoveryTarget.SCHEDULER",
    "FailureClass.EXTERNAL_SERVICE: RecoveryTarget.CAPABILITY",
    "FailureClass.COMPLETION_CRITERIA: RecoveryTarget.PLANNER",
)

#: L14-4 / L14-8 —— **A17.3**（V1 可达四类）。
EXPECTED_REACHABLE = (
    "FailureClass.COMPLETION_CRITERIA",
    "FailureClass.MODEL",
    "FailureClass.PERMISSION",
    "FailureClass.EXTERNAL_SERVICE",
)

#: L14-4 —— 最小修正的**逐类固定措辞**（A17：不得按 `message` 自由生成）。
EXPECTED_CLASS_TO_CORRECTION = (
    "FailureClass.INPUT: '补齐或修正输入事实后重跑'",
    "FailureClass.PERMISSION: '改走可授权的最小动作后重跑'",
    "FailureClass.TOOL: '重查能力后按当前能力重新派发'",
    "FailureClass.MODEL: '调整模型档位后重试'",
    "FailureClass.EFFORT: '调整推理强度后重试'",
    "FailureClass.DECOMPOSITION: '重新拆分任务后重跑'",
    "FailureClass.DEPENDENCY: '重排调度以解开依赖后重跑'",
    "FailureClass.EXTERNAL_SERVICE: '待外部服务恢复后重查环境再跑'",
    "FailureClass.COMPLETION_CRITERIA: '对照未满足的完成标准重新规划'",
)

#: L14-4 —— 六类可复用资产的文本前缀（A17.4 / L49 原文六类）。
EXPECTED_CARRY_FORWARD_TEXT = (
    "CarryForwardKind.VALIDATION_STATE: '验证状态'",
    "CarryForwardKind.EXCLUDED_DIRECTIONS: '已排除方向'",
    "CarryForwardKind.FACTS: '复用事实'",
    "CarryForwardKind.SUMMARY: '摘要'",
    "CarryForwardKind.LOGS: '日志'",
    "CarryForwardKind.MODIFICATIONS: '修改'",
)

#: L14-15 —— 模块级**带注解赋值**的白名单（恰好这六个；实测）。
EXPECTED_MODULE_ASSIGNMENTS = (
    "FAILURE_KIND_TO_CLASS",
    "FAILURE_CLASS_TO_TARGET",
    "V1_REACHABLE",
    "FAILURE_CLASS_TO_CORRECTION",
    "CARRY_FORWARD_TEXT",
    "DECISION_CONFIDENCE",
)

#: L14-10 / L14-11 / L14-12 —— 不得出现的符号（运行态 / 收敛 / 适配层失败语义）。
FORBIDDEN_SYMBOLS = (
    "BranchState",
    "RoutingRun",
    "AgentManager",
    "AgentStartRequest",
    "commit_started",
    "record_outcome",
    "apply_runtime_control",
    "control_flow_converged",
    "is_converged",
    "no_pending_approval",
    "no_pending_recovery",
    "CapabilityMismatch",
    "AdapterFailure",
    "AdapterFailureKind",
    "AdapterStartReceipt",
)

#: L14-14 —— `RecoveryContext` 的七个存储字段（第八个 `failed_criteria` 是**派生属性**）。
EXPECTED_CONTEXT_FIELDS = (
    "branch_id",
    "attempt",
    "total_attempt_limit",
    "validation",
    "failure_kind",
    "message",
    "timestamp",
)

#: L14-14 —— **不得**出现在输入 DTO 里的**计算字段**（A17.5：不得由上游注入）。
COMPUTED_FIELD_NAMES = (
    "failure_class",
    "recovery_target",
    "minimal_correction",
    "carry_forward",
    "confidence",
)


# --------------------------------------------------------------------------
# 测试替身
# --------------------------------------------------------------------------


def failed_validation(*, branch_id: str | None = None) -> ValidationRecord:
    return ValidationRecord(
        branch_id=branch_id if branch_id is not None else new_branch_id(),
        passed=False,
        evidence=("pytest 未通过",),
        failed_criteria=("2) 测试通过",),
        detail="一条未满足",
    )


def validation_path(**overrides: object) -> RecoveryContext:
    base: dict[str, object] = {
        "branch_id": new_branch_id(),
        "attempt": 1,
        "total_attempt_limit": 2,
        "validation": None,
        "failure_kind": None,
        "message": None,
        "timestamp": None,
    }
    base.update(overrides)
    if base["validation"] is None and "validation" not in overrides:
        base["validation"] = failed_validation(branch_id=str(base["branch_id"]))
    return RecoveryContext(**base)  # type: ignore[arg-type]


def kind_path(kind: str = "timeout", **overrides: object) -> RecoveryContext:
    base: dict[str, object] = {
        "branch_id": new_branch_id(),
        "attempt": 1,
        "total_attempt_limit": 2,
        "validation": None,
        "failure_kind": kind,
        "message": "平台失败",
        "timestamp": "2026-09-19T00:00:00Z",
    }
    base.update(overrides)
    return RecoveryContext(**base)  # type: ignore[arg-type]


# ==========================================================================
# 结构锁 L14-1 – L14-15
# ==========================================================================


class TestL141ImportBoundary:
    def test_recovery_token_set(self) -> None:
        tokens = set(imported_module_tokens(RECOVERY_SOURCE))
        assert tokens == set(ALLOWED_TOKENS_RECOVERY), (
            f"新增：{sorted(tokens - set(ALLOWED_TOKENS_RECOVERY))}；"
            f"消失：{sorted(set(ALLOWED_TOKENS_RECOVERY) - tokens)}"
        )

    def test_context_token_set(self) -> None:
        tokens = set(imported_module_tokens(CONTEXT_SOURCE))
        assert tokens == set(ALLOWED_TOKENS_CONTEXT), (
            f"新增：{sorted(tokens - set(ALLOWED_TOKENS_CONTEXT))}；"
            f"消失：{sorted(set(ALLOWED_TOKENS_CONTEXT) - tokens)}"
        )

    @pytest.mark.parametrize("source_name", ["recovery", "context"])
    def test_forbidden_imports_empty(self, source_name: str) -> None:
        source = RECOVERY_SOURCE if source_name == "recovery" else CONTEXT_SOURCE
        assert forbidden_import_hits(source, FORBIDDEN_IMPORTS) == set()

    @pytest.mark.parametrize(
        "needle",
        [
            "\nfrom .adapters import AdapterFailure  # noqa\n",
            "\nfrom .adapters.contract import AdapterFailureKind  # noqa\n",
            "\nfrom .agent_manager import AgentManager  # noqa\n",
            "\nfrom .validation import Validator  # noqa\n",
            "\nfrom . import cost  # noqa\n",
        ],
    )
    def test_probe_forbidden_import_goes_red(self, needle: str) -> None:
        assert forbidden_import_hits(RECOVERY_SOURCE + needle, FORBIDDEN_IMPORTS) != set()


class TestL142EntryShape:
    def test_module_level_functions(self) -> None:
        assert module_level_function_names(RECOVERY_SOURCE) == (
            "_class_for",
            "_target_for",
            "_carry_forward",
            "decide_recovery",
        )

    def test_entry_signature(self) -> None:
        # ⚠️ kw-only：与全仓惯例一致（`validate_spec(*, …)` / `schedule(*, …)` / `run_pass(*, …)`）。
        assert function_signature_shape(RECOVERY_SOURCE, "decide_recovery") == {
            "positional": [],
            "kwonly": ["context"],
            "defaults": [False],
            "vararg": None,
            "kwarg": None,
        }

    def test_probe_default_context_goes_red(self) -> None:
        mutated = RECOVERY_SOURCE.replace(
            "def decide_recovery(*, context: RecoveryContext) -> RecoveryDecision | None:",
            "def decide_recovery(*, context: RecoveryContext = None)"
            " -> RecoveryDecision | None:",
        )
        assert mutated != RECOVERY_SOURCE
        shape = function_signature_shape(mutated, "decide_recovery")
        assert shape["defaults"] != [False]


class TestL143ExplicitFailures:
    def test_raise_guards(self) -> None:
        assert function_raise_guards(RECOVERY_SOURCE, "decide_recovery") == (
            ("not isinstance(context, RecoveryContext)", "RecoveryRouterError"),
            ("failure_class not in V1_REACHABLE", "RecoveryRouterError"),
        )
        assert function_raise_guards(RECOVERY_SOURCE, "_class_for") == (
            ("kind not in FAILURE_KIND_TO_CLASS", "RecoveryRouterError"),
        )
        assert function_raise_guards(RECOVERY_SOURCE, "_target_for") == (
            ("failure_class not in FAILURE_CLASS_TO_TARGET", "RecoveryRouterError"),
        )

    def test_no_try_except(self) -> None:
        # 表未命中必须**显式失败**，不得 try/except KeyError 吞掉（LV-8 同纪律）
        assert handled_exception_names(RECOVERY_SOURCE) == ()

    def test_single_exception_type(self) -> None:
        assert exception_subclass_names(RECOVERY_SOURCE) == ("RecoveryRouterError",)

    def test_probe_dropped_reachability_guard_goes_red(self) -> None:
        mutated = RECOVERY_SOURCE.replace(
            "    if failure_class not in V1_REACHABLE:\n"
            "        raise RecoveryRouterError(\n"
            '            f"{failure_class!r} 在 V1 没有具名 producer（A17.3）⇒ 不得进入'
            ' RecoveryDecision"\n'
            '            "（「没有事实来源」≠「不支持」）"\n'
            "        )\n",
            "",
        )
        assert mutated != RECOVERY_SOURCE
        assert function_raise_guards(mutated, "decide_recovery") == (
            ("not isinstance(context, RecoveryContext)", "RecoveryRouterError"),
        )


class TestL144TableIsTheContract:
    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("FAILURE_KIND_TO_CLASS", EXPECTED_KIND_TO_CLASS),
            ("FAILURE_CLASS_TO_TARGET", EXPECTED_CLASS_TO_TARGET),
            ("V1_REACHABLE", EXPECTED_REACHABLE),
            ("FAILURE_CLASS_TO_CORRECTION", EXPECTED_CLASS_TO_CORRECTION),
            ("CARRY_FORWARD_TEXT", EXPECTED_CARRY_FORWARD_TEXT),
        ],
    )
    def test_table_rows_equal_the_frozen_contract(
        self, name: str, expected: tuple[str, ...]
    ) -> None:
        assert assigned_literal_items(RECOVERY_SOURCE, name) == expected

    @pytest.mark.parametrize("name", [n for n, _ in [
        ("FAILURE_KIND_TO_CLASS", ()),
        ("FAILURE_CLASS_TO_TARGET", ()),
        ("FAILURE_CLASS_TO_CORRECTION", ()),
        ("CARRY_FORWARD_TEXT", ()),
        ("V1_REACHABLE", ()),
    ]])
    def test_table_has_exactly_one_assignment_site(self, name: str) -> None:
        assert annotated_assign_names(RECOVERY_SOURCE).count(name) == 1

    def test_runtime_tables_also_match_the_literals(self) -> None:
        # 源码锁与**运行时对象**必须一致（防止"表另有构造路径"）
        assert tuple(f"{k!r}: {v!r}" for k, v in FAILURE_KIND_TO_CLASS.items()) == (
            "'crash': <FailureClass.EXTERNAL_SERVICE: 'external_service'>",
            "'timeout': <FailureClass.MODEL: 'model'>",
            "'refused': <FailureClass.PERMISSION: 'permission'>",
            "'network': <FailureClass.EXTERNAL_SERVICE: 'external_service'>",
        )
        assert len(FAILURE_CLASS_TO_TARGET) == 9
        assert len(FAILURE_CLASS_TO_CORRECTION) == 9
        assert len(CARRY_FORWARD_TEXT) == 6

    def test_probe_dropped_row_goes_red(self) -> None:
        mutated = RECOVERY_SOURCE.replace(
            "    FailureClass.TOOL: RecoveryTarget.CAPABILITY,\n", ""
        )
        assert mutated != RECOVERY_SOURCE
        assert (
            assigned_literal_items(mutated, "FAILURE_CLASS_TO_TARGET")
            != EXPECTED_CLASS_TO_TARGET
        )

    def test_probe_changed_mapping_goes_red(self) -> None:
        mutated = RECOVERY_SOURCE.replace(
            "    FailureClass.TOOL: RecoveryTarget.CAPABILITY,",
            "    FailureClass.TOOL: RecoveryTarget.MODEL_ROUTER,",
        )
        assert mutated != RECOVERY_SOURCE
        assert (
            assigned_literal_items(mutated, "FAILURE_CLASS_TO_TARGET")
            != EXPECTED_CLASS_TO_TARGET
        )


class TestL145NoInlineRules:
    def test_decision_function_comparisons_are_only_the_two_structural_ones(self) -> None:
        assert comparison_sites(RECOVERY_SOURCE, "decide_recovery") == (
            ("context.attempt", "GtE", "context.total_attempt_limit"),
            ("failure_class", "NotIn", "V1_REACHABLE"),
        )

    @pytest.mark.parametrize(
        "func", ["decide_recovery", "_class_for", "_target_for", "_carry_forward"]
    )
    def test_no_equality_on_kind_or_class(self, func: str) -> None:
        # 规则**只能**在表里：任何对 failure_kind / failure_class 的等值比较都是"代码复制规则"
        for left, op, right in comparison_sites(RECOVERY_SOURCE, func):
            if op in {"Eq", "NotEq"}:
                assert "failure_kind" not in left + right, (func, left, op, right)
                assert "kind" not in (left, right), (func, left, op, right)
                assert "failure_class" not in left + right, (func, left, op, right)

    def test_probe_inlined_rule_goes_red(self) -> None:
        mutated = RECOVERY_SOURCE.replace(
            "    route_to = _target_for(failure_class)",
            "    if failure_class == FailureClass.TOOL:\n"
            "        route_to = RecoveryTarget.CAPABILITY\n"
            "    else:\n"
            "        route_to = _target_for(failure_class)",
        )
        assert mutated != RECOVERY_SOURCE
        assert ("failure_class", "Eq", "FailureClass.TOOL") in comparison_sites(
            mutated, "decide_recovery"
        )


class TestL146NoFallback:
    def test_no_mapping_get_calls(self) -> None:
        # `.get(key, default)` = 兜底入口（LV-7 同纪律）
        assert mapping_get_calls(RECOVERY_SOURCE) == []

    def test_probe_get_with_default_goes_red(self) -> None:
        mutated = RECOVERY_SOURCE.replace(
            "    return FAILURE_KIND_TO_CLASS[kind]",
            "    return FAILURE_KIND_TO_CLASS.get(kind, FailureClass.TOOL)",
        )
        assert mutated != RECOVERY_SOURCE
        assert mapping_get_calls(mutated) != []


class TestL147Completeness:
    def test_class_table_covers_all_nine_classes(self) -> None:
        assert set(FAILURE_CLASS_TO_TARGET) == set(FailureClass)
        assert set(FAILURE_CLASS_TO_CORRECTION) == set(FailureClass)

    def test_target_table_is_surjective(self) -> None:
        assert set(FAILURE_CLASS_TO_TARGET.values()) == set(RecoveryTarget)

    def test_kind_table_keys_equal_the_frozen_kind_set(self) -> None:
        # 跨模块锁：`[14]` 侧的表键 == A14.4 四值（字符串投影）
        assert set(FAILURE_KIND_TO_CLASS) == {kind.value for kind in AdapterFailureKind}

    def test_probe_missing_class_row_goes_red(self) -> None:
        mutated = RECOVERY_SOURCE.replace(
            "    FailureClass.DEPENDENCY: RecoveryTarget.SCHEDULER,\n", ""
        )
        assert mutated != RECOVERY_SOURCE
        assert (
            assigned_literal_items(mutated, "FAILURE_CLASS_TO_TARGET")
            != EXPECTED_CLASS_TO_TARGET
        )


class TestL148V1Reachability:
    def test_reachable_set(self) -> None:
        assert V1_REACHABLE == frozenset(
            {
                FailureClass.COMPLETION_CRITERIA,
                FailureClass.MODEL,
                FailureClass.PERMISSION,
                FailureClass.EXTERNAL_SERVICE,
            }
        )

    def test_reachability_is_structural(self) -> None:
        """V1 里"不可达类别不得产出"是**结构保证**，不只是守卫。

        可达来源只有两条：`[13]` 路径 ⇒ `COMPLETION_CRITERIA`；kind 路径 ⇒ 表的值域。
        两者都必须落在 `V1_REACHABLE` 里 ⇒ 守卫在 V1 **不可达**（防御性）。
        """
        produced = set(FAILURE_KIND_TO_CLASS.values()) | {FailureClass.COMPLETION_CRITERIA}
        assert produced <= V1_REACHABLE

    def test_unreachable_five_are_absent_from_the_produced_set(self) -> None:
        produced = set(FAILURE_KIND_TO_CLASS.values()) | {FailureClass.COMPLETION_CRITERIA}
        unreachable = set(FailureClass) - V1_REACHABLE
        assert len(unreachable) == 5
        assert produced & unreachable == set()

    def test_probe_inverted_guard_goes_red(self) -> None:
        mutated = RECOVERY_SOURCE.replace(
            "    if failure_class not in V1_REACHABLE:",
            "    if failure_class in V1_REACHABLE:",
        )
        assert mutated != RECOVERY_SOURCE
        assert function_raise_guards(mutated, "decide_recovery") != (
            ("not isinstance(context, RecoveryContext)", "RecoveryRouterError"),
            ("failure_class not in V1_REACHABLE", "RecoveryRouterError"),
        )


class TestL149LimitSemantics:
    def test_return_annotation_and_shapes(self) -> None:
        assert (
            function_return_annotation(RECOVERY_SOURCE, "decide_recovery")
            == "RecoveryDecision | None"
        )
        shapes = function_return_shapes(RECOVERY_SOURCE, "decide_recovery")
        assert shapes[-1] == "None", "None 必须是最后那个（唯一）出口"
        assert len([s for s in shapes if s == "None"]) == 1

    def test_probe_second_none_exit_goes_red(self) -> None:
        mutated = RECOVERY_SOURCE.replace(
            "    failure_class = _class_for(context)",
            "    if context.attempt > 9:\n        return None\n"
            "    failure_class = _class_for(context)",
        )
        assert mutated != RECOVERY_SOURCE
        shapes = function_return_shapes(mutated, "decide_recovery")
        assert len([s for s in shapes if s == "None"]) == 2


class TestL1410NoStateWrite:
    def test_no_runtime_state_symbols(self) -> None:
        names = set(referenced_names(RECOVERY_SOURCE))
        assert names & {
            "BranchState",
            "RoutingRun",
            "AgentManager",
            "AgentStartRequest",
            "commit_started",
            "record_outcome",
            "apply_runtime_control",
        } == set()

    def test_no_adapter_symbols(self) -> None:
        names = set(referenced_names(RECOVERY_SOURCE)) | set(
            referenced_names(CONTEXT_SOURCE)
        )
        assert names & {
            "CapabilityMismatch",
            "AdapterFailure",
            "AdapterFailureKind",
            "AdapterStartReceipt",
        } == set()


class TestL1411NoConvergenceGuess:
    @pytest.mark.parametrize("source_name", ["recovery", "context"])
    def test_convergence_names_absent(self, source_name: str) -> None:
        source = RECOVERY_SOURCE if source_name == "recovery" else CONTEXT_SOURCE
        names = set(referenced_names(source))
        assert names & {
            "control_flow_converged",
            "is_converged",
            "no_pending_approval",
            "no_pending_recovery",
        } == set()


class TestL1412NoCapabilityMismatch:
    def test_only_allowed_kind_literals(self) -> None:
        # 四值字符串在**表**里出现（这是允许的：A14.4 的字符串投影），但类型名一个都不许有
        names = set(referenced_names(RECOVERY_SOURCE))
        assert "CapabilityMismatch" not in names
        assert "AdapterFailureKind" not in names


class TestL1413AuditNotInJudgment:
    @pytest.mark.parametrize(
        "func", ["decide_recovery", "_class_for", "_target_for", "_carry_forward"]
    )
    def test_audit_fields_never_compared(self, func: str) -> None:
        for left, op, right in comparison_sites(RECOVERY_SOURCE, func):
            assert "message" not in left + right, (func, left, op, right)
            assert "timestamp" not in left + right, (func, left, op, right)

    def test_probe_message_branch_goes_red(self) -> None:
        mutated = RECOVERY_SOURCE.replace(
            "    failure_class = _class_for(context)",
            "    if context.message == '配额不足':\n"
            "        failure_class = FailureClass.PERMISSION\n"
            "    else:\n"
            "        failure_class = _class_for(context)",
        )
        assert mutated != RECOVERY_SOURCE
        assert ("context.message", "Eq", "'配额不足'") in comparison_sites(
            mutated, "decide_recovery"
        )


class TestL1414ContextHasNoComputedFields:
    def test_fields(self) -> None:
        assert (
            dataclass_field_names(CONTEXT_SOURCE, "RecoveryContext")
            == EXPECTED_CONTEXT_FIELDS
        )

    @pytest.mark.parametrize("name", COMPUTED_FIELD_NAMES)
    def test_computed_fields_absent(self, name: str) -> None:
        assert name not in dataclass_field_names(CONTEXT_SOURCE, "RecoveryContext")

    def test_failed_criteria_is_a_derived_property(self) -> None:
        # 第八个名字是**派生属性**（不重复存储）
        assert "failed_criteria" not in dataclass_field_names(
            CONTEXT_SOURCE, "RecoveryContext"
        )
        ctx = validation_path()
        assert ctx.failed_criteria == ("2) 测试通过",)
        assert kind_path().failed_criteria == ()

    def test_probe_injected_computed_field_goes_red(self) -> None:
        mutated = CONTEXT_SOURCE.replace(
            "    message: str | None\n    timestamp: str | None\n",
            "    message: str | None\n    timestamp: str | None\n"
            "    failure_class: object | None = None\n",
        )
        assert mutated != CONTEXT_SOURCE
        assert "failure_class" in dataclass_field_names(mutated, "RecoveryContext")


class TestL1415StatelessNoIO:
    def test_module_level_assignments_are_five_tables_plus_one_constant(self) -> None:
        # 文案统一（复审 P2）：**六处**模块级赋值 = **五张规则表** + 一个常量 `DECISION_CONFIDENCE`
        assert annotated_assign_names(RECOVERY_SOURCE) == EXPECTED_MODULE_ASSIGNMENTS
        assert len(EXPECTED_MODULE_ASSIGNMENTS) == 6
        assert EXPECTED_MODULE_ASSIGNMENTS[-1] == "DECISION_CONFIDENCE"
        assert module_level_container_assignments(RECOVERY_SOURCE) == ()
        assert module_level_container_assignments(CONTEXT_SOURCE) == ()

    @pytest.mark.parametrize(
        ("source_name", "expected"),
        [
            (
                "recovery",
                tuple(EXPECTED_MODULE_ASSIGNMENTS)
                + ("RecoveryRouterError", "decide_recovery"),
            ),
            ("context", ("CarryForwardKind", "RecoveryContext")),
        ],
    )
    def test_dunder_all_matches_real_definitions(
        self, source_name: str, expected: tuple[str, ...]
    ) -> None:
        """**`__all__` 必须与真实定义一致**（复审 P1 暴露的**锁缺口**）。

        两条断言缺一不可：
          ① 声明的每个名字都**真的存在**（陈旧导出 ⇒ `import *` 抛 `AttributeError`）；
          ② 每个**公开**定义都被声明（漏导出 ⇒ `import *` 拿不到东西）。
        """
        source = RECOVERY_SOURCE if source_name == "recovery" else CONTEXT_SOURCE
        declared = set(declared_public_names(source))
        defined = (
            set(module_level_function_names(source))
            | set(class_def_names(source))
            | set(annotated_assign_names(source))
        )
        public_defined = {name for name in defined if not name.startswith("_")}
        assert declared <= defined, f"陈旧导出：{sorted(declared - defined)}"
        assert public_defined <= declared, f"漏导出：{sorted(public_defined - declared)}"
        assert declared == set(expected)

    def test_probe_stale_all_entry_goes_red(self) -> None:
        # 复现复审 P1 的那个缺陷形态：`__all__` 里留一个已删除的符号
        mutated = RECOVERY_SOURCE.replace(
            '    "V1_REACHABLE",', '    "V1_REACHABLE",\n    "REASON_BY_CLASS",'
        )
        assert mutated != RECOVERY_SOURCE
        declared = set(declared_public_names(mutated))
        defined = (
            set(module_level_function_names(mutated))
            | set(class_def_names(mutated))
            | set(annotated_assign_names(mutated))
        )
        assert "REASON_BY_CLASS" in declared - defined

    def test_probe_missing_all_entry_goes_red(self) -> None:
        mutated = RECOVERY_SOURCE.replace('    "V1_REACHABLE",\n', "")
        assert mutated != RECOVERY_SOURCE
        declared = set(declared_public_names(mutated))
        defined = (
            set(module_level_function_names(mutated))
            | set(class_def_names(mutated))
            | set(annotated_assign_names(mutated))
        )
        public_defined = {name for name in defined if not name.startswith("_")}
        assert public_defined - declared == {"V1_REACHABLE"}

    def test_class_surface(self) -> None:
        assert class_def_names(RECOVERY_SOURCE) == ("RecoveryRouterError",)
        assert class_def_names(CONTEXT_SOURCE) == ("CarryForwardKind", "RecoveryContext")

    def test_no_clock_no_io_no_cache(self) -> None:
        assert clock_call_hits(RECOVERY_SOURCE) == []
        assert open_calls(RECOVERY_SOURCE) == []
        assert set(referenced_names(RECOVERY_SOURCE)) & {
            "lru_cache",
            "cache",
            "setdefault",
            "global",
        } == set()

    def test_single_decision_construction_site(self) -> None:
        assert len(calls_to(RECOVERY_SOURCE, ("RecoveryDecision",))) == 1

    def test_probe_added_cache_goes_red(self) -> None:
        mutated = RECOVERY_SOURCE.replace(
            "__all__ = [", "_CACHE: dict[str, object] = {}\n\n__all__ = ["
        )
        assert mutated != RECOVERY_SOURCE
        assert "annotation" in mutated or "_CACHE" in mutated
        assert annotated_assign_names(mutated) != EXPECTED_MODULE_ASSIGNMENTS


class TestContextInvariants:
    def test_exactly_one_path(self) -> None:
        assert hasattr(rec := validation_path(), "validation") and rec.validation is not None
        assert kind_path().failure_kind == "timeout"

    def test_neither_or_both_paths_rejected(self) -> None:
        with pytest.raises(ValueError):
            RecoveryContext(
                branch_id=new_branch_id(),
                attempt=1,
                total_attempt_limit=2,
                validation=None,
                failure_kind=None,
                message=None,
                timestamp=None,
            )
        with pytest.raises(ValueError):
            RecoveryContext(
                branch_id=new_branch_id(),
                attempt=1,
                total_attempt_limit=2,
                validation=failed_validation(),
                failure_kind="timeout",
                message="m",
                timestamp="t",
            )

    def test_pass_record_rejected(self) -> None:
        passed = ValidationRecord(
            branch_id=new_branch_id(),
            passed=True,
            evidence=(),
            failed_criteria=(),
            detail="全满足",
        )
        with pytest.raises(ValueError):
            RecoveryContext(
                branch_id=passed.branch_id,
                attempt=1,
                total_attempt_limit=2,
                validation=passed,
                failure_kind=None,
                message=None,
                timestamp=None,
            )

    def test_cross_branch_validation_rejected(self) -> None:
        with pytest.raises(ValueError):
            RecoveryContext(
                branch_id=new_branch_id(),
                attempt=1,
                total_attempt_limit=2,
                validation=failed_validation(),
                failure_kind=None,
                message=None,
                timestamp=None,
            )

    def test_audit_fields_must_match_the_path(self) -> None:
        with pytest.raises(ValueError):
            validation_path(message="不该有")
        with pytest.raises(ValueError):
            kind_path(message="")
        with pytest.raises(ValueError):
            kind_path(timestamp="")

    def test_unknown_kind_rejected(self) -> None:
        with pytest.raises(ValueError):
            kind_path(kind="unknown_kind")

    def test_attempt_bounds(self) -> None:
        with pytest.raises(ValueError):
            kind_path(attempt=0)
        with pytest.raises(ValueError):
            kind_path(total_attempt_limit=0)


# ==========================================================================
# 行为测试 B1 – B16
# ==========================================================================


class TestB1ValidationPath:
    def test_completion_criteria_routes_to_planner(self) -> None:
        decision = decide_recovery(context=validation_path())
        assert isinstance(decision, RecoveryDecision)
        assert decision.failure_class is FailureClass.COMPLETION_CRITERIA
        assert decision.route_to is RecoveryTarget.PLANNER
        assert decision.minimal_correction == "对照未满足的完成标准重新规划"
        assert decision.carry_forward == ("验证状态：2) 测试通过",)
        assert decision.confidence is DECISION_CONFIDENCE


class TestB2KindPaths:
    @pytest.mark.parametrize(
        ("kind", "expected_class", "expected_target"),
        [
            ("crash", FailureClass.EXTERNAL_SERVICE, RecoveryTarget.CAPABILITY),
            ("timeout", FailureClass.MODEL, RecoveryTarget.MODEL_ROUTER),
            ("refused", FailureClass.PERMISSION, RecoveryTarget.PLANNER),
            ("network", FailureClass.EXTERNAL_SERVICE, RecoveryTarget.CAPABILITY),
        ],
    )
    def test_four_kinds(
        self, kind: str, expected_class: FailureClass, expected_target: RecoveryTarget
    ) -> None:
        decision = decide_recovery(context=kind_path(kind))
        assert decision is not None
        assert decision.failure_class is expected_class
        assert decision.route_to is expected_target
        assert decision.carry_forward == (
            f"复用事实：运行期失败类别 {kind}",
        )

    def test_all_five_targets_are_reachable_by_the_tables(self) -> None:
        assert set(FAILURE_CLASS_TO_TARGET.values()) == set(RecoveryTarget)


class TestB3Limit:
    @pytest.mark.parametrize(
        ("attempt", "limit", "expect_decision"),
        [(1, 2, True), (2, 2, False), (3, 2, False), (1, 1, False)],
    )
    def test_limit_semantics(
        self, attempt: int, limit: int, expect_decision: bool
    ) -> None:
        decision = decide_recovery(
            context=kind_path(attempt=attempt, total_attempt_limit=limit)
        )
        assert (decision is not None) is expect_decision

    def test_none_is_the_only_non_decision_exit(self) -> None:
        # 所有**可达**输入都必须产出决策（None 只属于"到上限"）
        for kind in sorted(FAILURE_KIND_TO_CLASS):
            assert decide_recovery(context=kind_path(kind)) is not None
        assert decide_recovery(context=validation_path()) is not None


class TestB4CarryForwardV1Honesty:
    def test_v1_carries_only_sourced_items(self) -> None:
        """A17.4 的"排除方向必带"在 V1 **没有 producer** ⇒ 本实现不带（README §84 上报项）。

        这条测试把"V1 只带有来源的事实"变成**可执行的事实**（而不是一句注释）。
        """
        texts = (
            decide_recovery(context=validation_path()).carry_forward
            + decide_recovery(context=kind_path()).carry_forward
        )
        prefixes = tuple(
            CARRY_FORWARD_TEXT[CarryForwardKind.VALIDATION_STATE],
        ) + (CARRY_FORWARD_TEXT[CarryForwardKind.FACTS],)
        for text in texts:
            assert text.startswith(prefixes), text
        assert not any(
            text.startswith(CARRY_FORWARD_TEXT[CarryForwardKind.EXCLUDED_DIRECTIONS])
            for text in texts
        )


class TestB5Determinism:
    def test_same_context_same_decision(self) -> None:
        context = kind_path("refused")
        first = decide_recovery(context=context)
        second = decide_recovery(context=context)
        assert first == second

    def test_context_is_untouched(self) -> None:
        context = validation_path()
        before = (context.branch_id, context.attempt, context.failed_criteria)
        decide_recovery(context=context)
        assert (context.branch_id, context.attempt, context.failed_criteria) == before


class TestB6WrongInputType:
    def test_non_context_rejected(self) -> None:
        with pytest.raises(RecoveryRouterError):
            decide_recovery(context="not-a-context")  # type: ignore[arg-type]


class TestB7DecisionDoesNotCarryBranchIdentity:
    def test_decision_has_no_branch_id_field(self) -> None:
        # `RecoveryDecision` 不带身份（契约现状）⇒ "决策 ↔ 分支"由持有 context 的调用方保对应
        assert "branch_id" not in RecoveryDecision.__dataclass_fields__
        decision = decide_recovery(context=validation_path())
        assert not hasattr(decision, "branch_id")


class TestB8Traceability:
    def test_recovery_ids_are_not_renumbered(self) -> None:
        # 本轮的测试责任：TEST-RECOVERY-001（两张表）/ -002（上限）/ -003（不猜收敛）
        assert FailureClass.COMPLETION_CRITERIA.value == "completion_criteria"
        assert RecoveryTarget.PLANNER.value == "planner"
        assert isinstance(FAILURE_CLASS_TO_TARGET[FailureClass.TOOL], RecoveryTarget)
        assert Confidence.HIGH in {c for c in Confidence}
        assert ContractError is not None  # 契约层异常未被本轮包装
