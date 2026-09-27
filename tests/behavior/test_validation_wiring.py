"""``[12] → [13] Wiring`` —— 结构锁 LW-1 – LW-12 + 行为测试 B1 – B12。

架构依据：`docs/architecture.md` 2 节 ``[12]`` / ``[13]`` 与 **A11.4b**
（`architecture.md:1974-1985`：``ReusableResultRef`` 的两半**必须来自同一次真实的
``[12] → [13]`` 配对事件**，**不得**事后仅凭 ``branch_id`` 从两个独立集合重新配对）、
**A15.4**（`:2511-2512`：谁持有 ``BranchReturn`` 直到 ``[13]`` 消费 ⇒ 属本配对轮）、
**A16.1 / A16.2 / A16.3 / A16.6**。
范围、锁清单与 probe 计划：`tests/behavior/README_step5b.md` §76（复审批准：方案 A）。

**本模块要守住的三条主张：**

  1. **配对见证不可拆分**：``BranchValidationOutcome`` 只有**一个构造点**，
     且**全 ``src/`` 只有接线层构造它**（§76.5 的四个审查点）；
  2. **接线不做判定**：wiring **不读** ``validation_judgment``、**不重建** ``BranchReturn`` /
     ``ValidationRecord``、**不改写** ``passed`` / ``failed_criteria``；
  3. **接线缺陷 ≠ 业务 FAIL**：未知 ``branch_id`` / 缺 bundle / 注入错配 ⇒ **显式失败**，
     **绝不**产出 ``ValidationRecord(passed=False)``（否则污染 ``[14]`` 的失败分类）。

⚠️ **禁止子串扫描**：本文件的 docstring 里**故意提及** ``validation_state`` /
``RecoveryDecision`` / ``completion_criteria`` 等词（说明边界）⇒ 所有锁一律 **AST 级**。
⚠️ 敏感性 probe 一律在**源码字符串**上完成，**不覆写** ``src/`` 里的真实文件；
真实源码的一次性变异证据见 README §77.5（含逐字节还原核对）。
"""

from __future__ import annotations

import pathlib

import pytest

from adaptive_router.contracts import (
    BranchReturn,
    BranchSpec,
    Confidence,
    ConfirmationSource,
    ContextBundle,
    ContractError,
    EffortLevel,
    ExecutionMode,
    ExecutionValue,
    ModelTier,
    Plan,
    ValidationRecord,
    new_agent_id,
    new_branch_id,
)
from adaptive_router.validation import Validator
from adaptive_router.validation_judgment import CriterionVerdict, ValidationJudgment
from adaptive_router.validation_wiring import (
    BranchValidationOutcome,
    ValidationWiringError,
    validate_branch,
)

from _validation_wiring_locks import (
    annotated_assign_names,
    attribute_chains,
    call_keyword_shapes,
    calls_to,
    class_def_names,
    clock_call_hits,
    dataclass_field_names,
    exception_subclass_names,
    forbidden_import_hits,
    function_call_keyword_values,
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
WIRING_SOURCE = (SRC / "validation_wiring.py").read_text(encoding="utf-8")

#: LW-1 —— 接线层的 import token **精确集合**（实测，见 README §77.3）。
ALLOWED_IMPORT_TOKENS = frozenset(
    {
        "__future__",
        "annotations",
        "collections",
        "abc",
        "collections.abc",
        "dataclasses",
        "dataclass",
        "contracts",
        "validation",
    }
)

#: LW-1 —— 禁用清单。
#:
#: ⚠️ ``validation_judgment`` 在列**是 §76.2 的新边界**：wiring **不得**读 verdicts、
#: 不得重新解释判定依据、不得再生成一条 ``ValidationRecord``（三层职责不合并，A16）。
#: ``recovery`` 在列是 A16.1 的落点：``PASS → 终态`` / ``FAIL → [14]`` 属**下一轮**。
FORBIDDEN_IMPORTS = (
    "scheduler",
    "agent_manager",
    "context_builder",
    "planner",
    "model_router",
    "policy",
    "cost",
    "recovery",
    "execution",
    "execution_results",
    "result_handoff",
    "reusable_results",
    "validation_judgment",
    "adapters",
    "approval_judgment",
    "approval_disclosure",
    "risk",
    "capabilities",
    "project_context",
    "router",
    "entrypoint",
    "session",
)

#: LW-10 —— 接线层**不得**触碰的符号（恢复 / 调度 / 运行态 / 复用链）。
FORBIDDEN_SYMBOLS = (
    "RecoveryDecision",
    "RecoveryTarget",
    "RecoveryRouter",
    "BranchState",
    "AgentManager",
    "AgentStartRequest",
    "Scheduler",
    "DispatchDecision",
    "RoutingRun",
    "ReusableResultRef",
    "CapabilityMismatch",
    "AdapterFailure",
)

#: LW-12 —— **INV-19 在接线侧仍成立**：自报字段一个都不准读。
SELF_REPORT_NAMES = (
    "validation_state",
    "key_evidence",
    "conclusion_summary",
    "residual_risks",
    "main_agent_todo",
    "passed",
    "failed_criteria",
)

#: LW-3 —— `validate_branch` 的五个显式失败点（实测，见 README §77.3）。
RAISE_GUARDS_VALIDATE = (
    ("not isinstance(plan, Plan)", "ValidationWiringError"),
    ("not isinstance(bundles, Mapping)", "ValidationWiringError"),
    ("not isinstance(branch_return, BranchReturn)", "ValidationWiringError"),
    ("not isinstance(validator, Validator)", "ValidationWiringError"),
    ("branch_id not in bundles", "ValidationWiringError"),
)

#: LW-3 —— `_spec_for` 的显式失败点（循环外无条件抛出）。
RAISE_GUARDS_SPEC_FOR = (("<unconditional>", "ValidationWiringError"),)

#: LW-11 —— `[12]` / `[13]` 冻结实现的 SHA256（§73 / §74 登记值）。
PROTECTED_BASELINES = {
    "validation.py": (
        "2039596379E7E037FE5E0A63B3508A8B3F8462DA256FBA0002742D638D9C26D2"
    ),
    "validation_judgment.py": (
        "F2272E879C3C664825490E84C5D401BC1E9809055E96FB3B3E3AC31FED9ACB9A"
    ),
    "execution_results.py": (
        "A70C846AE09DDAC9B138A52B22C3DB85ABF30077D384A76DA23F0FDC5B06BC00"
    ),
    "result_handoff.py": (
        "43DA0276924BA13B0B06398D13AFFD1509AA42BE33AD37CB1DB1D3E47F3325D9"
    ),
    "execution.py": (
        "BBAF6C29F8AD3793811D88D65EB204BF90D1589D3F69A7610E465CD90918C409"
    ),
}

#: `[13]` 的显式失败点（实测 `validation.py`）：接线**不得**吞掉它们。
RAISE_GUARDS_VALIDATION = (
    ("branch_return.branch_id != spec.branch_id", "ContractError"),
    ("spec.completion_criteria != bundle.completion_criteria", "ContractError"),
    ("not isinstance(judgment, ValidationJudgment)", "ContractError"),
    ("not judgment.verdicts", "ContractError"),
    ("verdict.criterion.strip() not in spec.completion_criteria", "ContractError"),
)

CRITERIA = "1) 文档已更新；2) 测试通过"


# --------------------------------------------------------------------------
# 测试替身
# --------------------------------------------------------------------------


class RecordingValidator(Validator):
    """记录调用的 ``Validator`` 子类（接线要求 ``isinstance(validator, Validator)``）。

    ⚠️ 用**子类**而不是鸭子类型替身：接线层刻意做严格 ``isinstance``（与
    ``reusable_results.py`` 的"不靠 duck typing"同纪律），替身必须满足它。
    """

    def __init__(self, judgment: ValidationJudgment) -> None:
        super().__init__(judgment_source=lambda _r, _c: judgment)
        self.calls: list[tuple[str, str, str]] = []

    def validate_spec(
        self,
        *,
        branch_return: BranchReturn,
        spec: BranchSpec,
        bundle: ContextBundle,
    ) -> ValidationRecord:
        self.calls.append(
            (branch_return.branch_id, spec.completion_criteria, bundle.completion_criteria)
        )
        return super().validate_spec(
            branch_return=branch_return, spec=spec, bundle=bundle
        )


def judgment(*, satisfied: bool = True, self_report_basis: str = "diff") -> ValidationJudgment:
    return ValidationJudgment(
        verdicts=(
            CriterionVerdict(
                criterion="1) 文档已更新",
                satisfied=satisfied,
                evidence=(self_report_basis,),
            ),
            CriterionVerdict(
                criterion="2) 测试通过", satisfied=satisfied, evidence=("pytest",)
            ),
        ),
        detail="依据齐全" if satisfied else "一条未满足",
    )


def make_spec(*, branch_id: str, criteria: str = CRITERIA) -> BranchSpec:
    return BranchSpec(
        branch_id=branch_id,
        goal="写文档",
        read_only=False,
        completion_criteria=criteria,
    )


def make_bundle(*, criteria: str = CRITERIA) -> ContextBundle:
    return ContextBundle(
        goal="写文档",
        background=(),
        constraints=(),
        allowed_operations=(),
        forbidden_operations=(),
        output_format="markdown",
        completion_criteria=criteria,
        confirmed_conclusions=(),
        excluded=(),
        includes_history=False,
    )


def make_return(*, branch_id: str, attempt: int = 1) -> BranchReturn:
    return BranchReturn(
        branch_id=branch_id,
        agent_id=new_agent_id(),
        attempt=attempt,
        model=ExecutionValue(
            planned=ModelTier.BALANCED,
            planned_source=ConfirmationSource.PLANNER,
            requested="gpt-5-codex",
            requested_source=ConfirmationSource.REQUEST,
            actual="gpt-5-codex-2025-xx",
            actual_source=ConfirmationSource.PROVIDER_API,
            observed=True,
            confidence=Confidence.HIGH,
        ),
        effort=ExecutionValue(
            planned=EffortLevel.MEDIUM,
            planned_source=ConfirmationSource.PLANNER,
            requested=EffortLevel.MEDIUM,
            requested_source=ConfirmationSource.REQUEST,
            actual=EffortLevel.HIGH,
            actual_source=ConfirmationSource.PROVIDER_API,
            observed=True,
            confidence=Confidence.HIGH,
        ),
        conclusion_summary="done",
        key_evidence=("e1",),
        validation_state="ok",
        residual_risks=(),
        main_agent_todo=(),
    )


def make_plan(*, branch_ids: tuple[str, ...]) -> Plan:
    """一个合法 ``MULTI_AGENT`` 计划（≥2 child；L25）。"""
    return Plan(
        mode=ExecutionMode.MULTI_AGENT,
        main_thread_duty="汇总",
        reason="两个独立子任务",
        branches=tuple(make_spec(branch_id=branch_id) for branch_id in branch_ids),
    )


def single_plan() -> tuple[Plan, str]:
    """返回一个"计划里恰好一个 child"的 plan 与它的 branch_id。"""
    branch_id = new_branch_id()
    return make_plan(branch_ids=(branch_id, new_branch_id())), branch_id


# ==========================================================================
# 结构锁 LW-1 – LW-12
# ==========================================================================


class TestLW1ImportBoundary:
    def test_exact_token_set(self) -> None:
        tokens = set(imported_module_tokens(WIRING_SOURCE))
        assert tokens == set(ALLOWED_IMPORT_TOKENS), (
            f"新增：{sorted(tokens - set(ALLOWED_IMPORT_TOKENS))}；"
            f"消失：{sorted(set(ALLOWED_IMPORT_TOKENS) - tokens)}"
        )

    def test_forbidden_imports_empty(self) -> None:
        assert forbidden_import_hits(WIRING_SOURCE, FORBIDDEN_IMPORTS) == set()

    def test_wiring_never_reads_judgment_module(self) -> None:
        # §76.2 的新边界：三层职责不合并（producer 供依据 / [13] 判定 / wiring 只证明配对）
        assert "validation_judgment" not in set(
            imported_module_tokens(WIRING_SOURCE)
        )
        assert "ValidationJudgment" not in set(referenced_names(WIRING_SOURCE))
        assert "CriterionVerdict" not in set(referenced_names(WIRING_SOURCE))

    @pytest.mark.parametrize(
        "needle",
        [
            "\nfrom .validation_judgment import ValidationJudgment  # noqa\n",
            "\nfrom .recovery import RecoveryRouter  # noqa\n",
            "\nfrom .agent_manager import AgentManager  # noqa\n",
            "\nfrom .reusable_results import ReusableResultRef  # noqa\n",
            "\nfrom . import scheduler  # noqa\n",
        ],
    )
    def test_probe_forbidden_import_goes_red(self, needle: str) -> None:
        assert (
            forbidden_import_hits(WIRING_SOURCE + needle, FORBIDDEN_IMPORTS) != set()
        )


class TestLW2EntryShape:
    def test_entry_is_module_level_pure_function(self) -> None:
        # 模块级函数 ⇒ 没有实例 ⇒ 结构上不可能有实例状态 / 缓存
        assert module_level_function_names(WIRING_SOURCE) == (
            "_spec_for",
            "validate_branch",
        )

    def test_entry_signature(self) -> None:
        assert function_signature_shape(WIRING_SOURCE, "validate_branch") == {
            "positional": [],
            "kwonly": ["plan", "bundles", "branch_return", "validator"],
            "defaults": [False, False, False, False],
            "vararg": None,
            "kwarg": None,
        }

    def test_validator_has_no_default(self) -> None:
        # 无默认值 ⇒ 接线层在结构上不可能"自己装一个 producer"
        shape = function_signature_shape(WIRING_SOURCE, "validate_branch")
        assert shape["kwonly"] == ["plan", "bundles", "branch_return", "validator"]
        assert all(default is False for default in shape["defaults"])  # type: ignore[union-attr]

    def test_probe_default_validator_goes_red(self) -> None:
        original = (
            "    branch_return: BranchReturn,\n    validator: Validator,\n"
        )
        mutated = WIRING_SOURCE.replace(
            original,
            "    branch_return: BranchReturn,\n    validator: Validator = None,\n",
        )
        assert mutated != WIRING_SOURCE
        shape = function_signature_shape(mutated, "validate_branch")
        assert shape["defaults"] != [False, False, False, False]


class TestLW3ExplicitFailures:
    def test_raise_guards_validate_branch(self) -> None:
        assert (
            function_raise_guards(WIRING_SOURCE, "validate_branch")
            == RAISE_GUARDS_VALIDATE
        )

    def test_raise_guards_spec_for(self) -> None:
        assert (
            function_raise_guards(WIRING_SOURCE, "_spec_for") == RAISE_GUARDS_SPEC_FOR
        )

    def test_all_wiring_failures_are_wiring_error(self) -> None:
        guards = function_raise_guards(
            WIRING_SOURCE, "validate_branch"
        ) + function_raise_guards(WIRING_SOURCE, "_spec_for")
        assert {name for _, name in guards} == {"ValidationWiringError"}

    def test_probe_dropped_guard_goes_red(self) -> None:
        # 删掉"缺 bundle"守卫 ⇒ 接线缺陷不再显式失败（会静默走到 [13]）
        mutated = WIRING_SOURCE.replace(
            "    if branch_id not in bundles:\n"
            "        raise ValidationWiringError(\n"
            '            f"分支 {branch_id} 没有 prepared ContextBundle"\n'
            '            "（A10.1b：被派发的 child 必有副本；缺失 = 接线缺陷，不是 FAIL）"\n'
            "        )\n",
            "",
        )
        assert mutated != WIRING_SOURCE
        assert (
            function_raise_guards(mutated, "validate_branch")
            == RAISE_GUARDS_VALIDATE[:-1]
        )

    def test_probe_swapped_error_type_goes_red(self) -> None:
        # 把接线失败换成"业务失败"类型（ContractError）⇒ 守卫锁必须红
        mutated = WIRING_SOURCE.replace(
            "raise ValidationWiringError(", "raise ContractError("
        )
        assert mutated != WIRING_SOURCE
        assert (
            function_raise_guards(mutated, "validate_branch")
            != RAISE_GUARDS_VALIDATE
        )


class TestLW4SingleJudgmentCall:
    def test_judgment_called_exactly_once(self) -> None:
        assert len(calls_to(WIRING_SOURCE, ("validate_spec",))) == 1

    def test_call_arguments_are_the_resolved_objects(self) -> None:
        # ⚠️ 这是**属性调用**（`validator.validate_spec(...)`）⇒ 用 keyword 形态检测器；
        #    `_results_locks.function_call_keyword_values` 只看裸 `Name(...)` 调用。
        assert call_keyword_shapes(WIRING_SOURCE, "validate_spec") == {
            "branch_return": ("branch_return",),
            "spec": ("spec",),
            "bundle": ("bundles[branch_id]",),
        }

    def test_probe_second_judgment_call_goes_red(self) -> None:
        mutated = WIRING_SOURCE.replace(
            "    return BranchValidationOutcome(",
            "    validator.validate_spec(\n"
            "        branch_return=branch_return, spec=spec, bundle=bundles[branch_id]\n"
            "    )\n"
            "    return BranchValidationOutcome(",
        )
        assert mutated != WIRING_SOURCE
        assert len(calls_to(mutated, ("validate_spec",))) == 2


class TestLW5AuthorityIsThePlan:
    def test_spec_comes_from_the_plan(self) -> None:
        assert len(calls_to(WIRING_SOURCE, ("_spec_for",))) == 1

    def test_wiring_never_reads_the_copy_contents(self) -> None:
        # 副本的**字段内容**（尤其 completion_criteria）是 [13] 的核对对象，不是接线的输入
        names = set(referenced_names(WIRING_SOURCE))
        assert "completion_criteria" not in names
        assert "bundle.completion_criteria" not in attribute_chains(WIRING_SOURCE)

    def test_probe_reading_copy_criteria_goes_red(self) -> None:
        mutated = WIRING_SOURCE.replace(
            "    branch_id = branch_return.branch_id\n",
            "    branch_id = branch_return.branch_id\n"
            "    _probe = bundles[branch_id].completion_criteria  # noqa\n",
        )
        assert mutated != WIRING_SOURCE
        assert "completion_criteria" in set(referenced_names(mutated))


class TestLW6PairWitnessSingleConstruction:
    def test_single_construction_site_in_module(self) -> None:
        assert len(calls_to(WIRING_SOURCE, ("BranchValidationOutcome",))) == 1

    def test_single_return_site_returns_the_witness(self) -> None:
        assert function_return_shapes(WIRING_SOURCE, "validate_branch") == (
            "BranchValidationOutcome(branch_return=branch_return, validation=validation)",
        )

    def test_return_annotation_is_the_witness(self) -> None:
        assert (
            function_return_annotation(WIRING_SOURCE, "validate_branch")
            == "BranchValidationOutcome"
        )

    def test_no_other_module_constructs_the_witness(self) -> None:
        # **repo-wide**：全 src/ 只有接线层构造配对载体
        offenders = sorted(
            str(path.relative_to(SRC))
            for path in SRC.rglob("*.py")
            if path.name != "validation_wiring.py"
            and calls_to(path.read_text(encoding="utf-8"), ("BranchValidationOutcome",))
        )
        assert offenders == []

    def test_probe_naked_record_return_goes_red(self) -> None:
        # M1 的合成版本：返回裸 `ValidationRecord`（配对见证消失）
        mutated = WIRING_SOURCE.replace(
            "    return BranchValidationOutcome(branch_return=branch_return,"
            " validation=validation)",
            "    return validation",
        )
        assert mutated != WIRING_SOURCE
        assert calls_to(mutated, ("BranchValidationOutcome",)) == []
        assert function_return_shapes(mutated, "validate_branch") == ("validation",)

    def test_probe_second_construction_site_goes_red(self) -> None:
        mutated = WIRING_SOURCE + (
            "\n\ndef _second(outcome: BranchValidationOutcome) ->"
            " BranchValidationOutcome:\n"
            "    return BranchValidationOutcome(\n"
            "        branch_return=outcome.branch_return, validation=outcome.validation\n"
            "    )\n"
        )
        assert len(calls_to(mutated, ("BranchValidationOutcome",))) == 2


class TestLW7NoRebuildNoModify:
    def test_no_branch_return_construction(self) -> None:
        assert calls_to(WIRING_SOURCE, ("BranchReturn",)) == []

    def test_no_validation_record_construction(self) -> None:
        # 接线**不得**再生成一条记录（[13] 的唯一构造权，A16.6）
        assert calls_to(WIRING_SOURCE, ("ValidationRecord",)) == []

    def test_no_passed_or_failed_criteria_assignment(self) -> None:
        names = set(referenced_names(WIRING_SOURCE))
        assert names & {"passed", "failed_criteria", "evidence", "verdicts"} == set()

    def test_probe_rebuild_branch_return_goes_red(self) -> None:
        mutated = WIRING_SOURCE.replace(
            "    return BranchValidationOutcome(branch_return=branch_return,"
            " validation=validation)",
            "    branch_return = BranchReturn(\n"
            "        branch_id=branch_return.branch_id,\n"
            "        agent_id=branch_return.agent_id,\n"
            "        attempt=branch_return.attempt,\n"
            "        model=branch_return.model,\n"
            "        effort=branch_return.effort,\n"
            "        conclusion_summary=branch_return.conclusion_summary,\n"
            "        key_evidence=branch_return.key_evidence,\n"
            "        validation_state=branch_return.validation_state,\n"
            "        residual_risks=branch_return.residual_risks,\n"
            "        main_agent_todo=branch_return.main_agent_todo,\n"
            "    )\n"
            "    return BranchValidationOutcome(branch_return=branch_return,"
            " validation=validation)",
        )
        assert mutated != WIRING_SOURCE
        assert calls_to(mutated, ("BranchReturn",)) != []
        assert "validation_state" in set(referenced_names(mutated))


class TestLW8StatelessNoIO:
    def test_no_module_level_state(self) -> None:
        assert module_level_container_assignments(WIRING_SOURCE) == ()
        # ⚠️ 本轮 probe 发现的 detector 漏洞：带注解的赋值（`_CACHE: dict = {}`）
        #    不在 `module_level_container_assignments` 的覆盖范围内 ⇒ 补这条同义词锁。
        assert annotated_assign_names(WIRING_SOURCE) == ()

    def test_class_surface(self) -> None:
        assert class_def_names(WIRING_SOURCE) == (
            "ValidationWiringError",
            "BranchValidationOutcome",
        )

    def test_no_clock_no_io_no_silent_default(self) -> None:
        assert clock_call_hits(WIRING_SOURCE) == []
        assert open_calls(WIRING_SOURCE) == []
        # `.get(key, default)` = "静默默认"的常见入口（缺 bundle 必须显式失败）
        assert mapping_get_calls(WIRING_SOURCE) == []

    def test_no_cache_decorators(self) -> None:
        names = set(referenced_names(WIRING_SOURCE))
        assert names & {"lru_cache", "cache", "setdefault", "global"} == set()

    def test_no_custom_exception_beyond_wiring_error(self) -> None:
        assert exception_subclass_names(WIRING_SOURCE) == ("ValidationWiringError",)

    def test_no_try_except(self) -> None:
        # 吞掉 [13] 的 ContractError 会把契约失败降级成"没判过"（B7 的靶子）
        assert handled_exception_names(WIRING_SOURCE) == ()

    def test_probe_added_cache_goes_red(self) -> None:
        for declaration in ("_CACHE = {}", "_CACHE: dict = {}"):
            mutated = WIRING_SOURCE.replace(
                "__all__ = [", declaration + "\n\n__all__ = ["
            )
            assert mutated != WIRING_SOURCE
            assert (
                module_level_container_assignments(mutated) != ()
                or annotated_assign_names(mutated) != ()
            ), declaration


class TestLW9WitnessShape:
    def test_fields(self) -> None:
        assert dataclass_field_names(WIRING_SOURCE, "BranchValidationOutcome") == (
            "branch_return",
            "validation",
        )

    def test_invariants(self) -> None:
        assert raise_guards(
            WIRING_SOURCE, "BranchValidationOutcome", "__post_init__"
        ) == (
            (
                "not isinstance(self.branch_return, BranchReturn)",
                "TypeError",
            ),
            ("not isinstance(self.validation, ValidationRecord)", "TypeError"),
            (
                "self.branch_return.branch_id != self.validation.branch_id",
                "ValueError",
            ),
        )

    def test_probe_extra_field_goes_red(self) -> None:
        mutated = WIRING_SOURCE.replace(
            "    branch_return: BranchReturn\n    validation: ValidationRecord\n",
            "    branch_return: BranchReturn\n    validation: ValidationRecord\n"
            "    note: str\n",
        )
        assert mutated != WIRING_SOURCE
        assert dataclass_field_names(mutated, "BranchValidationOutcome") != (
            "branch_return",
            "validation",
        )


class TestLW10NoRecoveryNoRuntimeState:
    def test_forbidden_symbols_absent(self) -> None:
        names = set(referenced_names(WIRING_SOURCE))
        assert names & set(FORBIDDEN_SYMBOLS) == set()

    def test_only_branch_id_is_read_from_the_return(self) -> None:
        # 接线从回传里只读身份（与 [13] 的 LV-2 同纪律）
        read = {
            chain
            for chain in attribute_chains(WIRING_SOURCE)
            if chain.startswith("branch_return.")
        }
        assert read == {"branch_return.branch_id"}


class TestLW11ProtectedBaselines:
    @pytest.mark.parametrize("name", sorted(PROTECTED_BASELINES))
    def test_frozen_module_bytes_unchanged(self, name: str) -> None:
        import hashlib

        digest = hashlib.sha256((SRC / name).read_bytes()).hexdigest().upper()
        assert digest == PROTECTED_BASELINES[name], (
            f"{name} 是已冻结实现，本轮接线**一字不该改**；"
            "确需修改必须先在架构层裁定 + 更新本表 + 记录"
        )

    def test_probe_hash_mismatch_goes_red(self) -> None:
        # 证明这条锁**真的会因为字节变化而红**（在内存里算，不用 pytest tmp_path：
        # 本环境 sandbox 下 pytest 的 tmp 目录不可用）
        import hashlib

        data = (SRC / "validation.py").read_bytes()
        assert hashlib.sha256(data).hexdigest().upper() == PROTECTED_BASELINES[
            "validation.py"
        ]
        assert hashlib.sha256(data + b"\n").hexdigest().upper() != PROTECTED_BASELINES[
            "validation.py"
        ]


class TestLW12ValidationIndependenceInWiring:
    def test_self_report_names_absent(self) -> None:
        names = set(referenced_names(WIRING_SOURCE))
        assert names & set(SELF_REPORT_NAMES) == set()

    def test_wiring_does_not_rebuild_a_judgment(self) -> None:
        assert calls_to(WIRING_SOURCE, ("ValidationJudgment",)) == []
        assert calls_to(WIRING_SOURCE, ("CriterionVerdict",)) == []

    def test_validation_module_keeps_its_own_guards(self) -> None:
        # 接线轮的 LW-3 只锁 wiring 自己的失败点；`[13]` 的五个失败点**原样保留**
        validation_source = (SRC / "validation.py").read_text(encoding="utf-8")
        assert (
            raise_guards(validation_source, "Validator", "validate_spec")
            == RAISE_GUARDS_VALIDATION
        )


# ==========================================================================
# 行为测试 B1 – B12
# ==========================================================================


class TestB1PassPairing:
    def test_pass_pair_is_the_original_objects(self) -> None:
        plan, branch_id = single_plan()
        branch_return = make_return(branch_id=branch_id)
        validator = RecordingValidator(judgment(satisfied=True))
        outcome = validate_branch(
            plan=plan,
            bundles={branch_id: make_bundle()},
            branch_return=branch_return,
            validator=validator,
        )
        assert isinstance(outcome, BranchValidationOutcome)
        assert outcome.validation.passed is True
        # **同一对象**（不重建、不复制）—— A11.4b 的"同一次事件"由此可断言
        assert outcome.branch_return is branch_return
        assert outcome.branch_return.branch_id == outcome.validation.branch_id
        assert len(validator.calls) == 1


class TestB2FailPairing:
    def test_fail_is_paired_too(self) -> None:
        plan, branch_id = single_plan()
        branch_return = make_return(branch_id=branch_id)
        validator = RecordingValidator(judgment(satisfied=False))
        outcome = validate_branch(
            plan=plan,
            bundles={branch_id: make_bundle()},
            branch_return=branch_return,
            validator=validator,
        )
        assert outcome.validation.passed is False
        assert outcome.validation.failed_criteria == ("1) 文档已更新", "2) 测试通过")
        assert outcome.branch_return is branch_return

    def test_wiring_does_not_interpret_fail(self) -> None:
        # 接线**不解释** FAIL（不产生异常、不写运行态、不给去路）
        plan, branch_id = single_plan()
        validator = RecordingValidator(judgment(satisfied=False))
        outcome = validate_branch(
            plan=plan,
            bundles={branch_id: make_bundle()},
            branch_return=make_return(branch_id=branch_id),
            validator=validator,
        )
        assert isinstance(outcome, BranchValidationOutcome)


class TestB3PairIdentity:
    def test_identity_mismatch_is_unrepresentable(self) -> None:
        # DTO 层直接拒绝"两半不同分支"的配对
        plan, branch_id = single_plan()
        validator = RecordingValidator(judgment(satisfied=True))
        outcome = validate_branch(
            plan=plan,
            bundles={branch_id: make_bundle()},
            branch_return=make_return(branch_id=branch_id),
            validator=validator,
        )
        with pytest.raises(ValueError):
            BranchValidationOutcome(
                branch_return=outcome.branch_return,
                validation=ValidationRecord(
                    branch_id=new_branch_id(),
                    passed=True,
                    evidence=(),
                    failed_criteria=(),
                    detail="另一个分支的记录",
                ),
            )

    def test_dto_rejects_wrong_types(self) -> None:
        with pytest.raises(TypeError):
            BranchValidationOutcome(branch_return="nope", validation="nope")  # type: ignore[arg-type]


class TestB4UnknownBranch:
    def test_unknown_branch_refused_before_judgment(self) -> None:
        plan, _ = single_plan()
        unknown = new_branch_id()
        validator = RecordingValidator(judgment(satisfied=True))
        with pytest.raises(ValidationWiringError) as excinfo:
            validate_branch(
                plan=plan,
                bundles={unknown: make_bundle()},
                branch_return=make_return(branch_id=unknown),
                validator=validator,
            )
        assert "Plan.branches" in str(excinfo.value)
        assert validator.calls == [], "接线缺陷**不得**先消耗一次判定"

    def test_unknown_branch_never_produces_a_record(self) -> None:
        plan, _ = single_plan()
        unknown = new_branch_id()
        validator = RecordingValidator(judgment(satisfied=True))
        with pytest.raises(ValidationWiringError):
            validate_branch(
                plan=plan,
                bundles={},
                branch_return=make_return(branch_id=unknown),
                validator=validator,
            )
        assert validator.calls == []


class TestB5MissingBundle:
    def test_missing_bundle_refused_before_judgment(self) -> None:
        plan, branch_id = single_plan()
        validator = RecordingValidator(judgment(satisfied=True))
        with pytest.raises(ValidationWiringError) as excinfo:
            validate_branch(
                plan=plan,
                bundles={},
                branch_return=make_return(branch_id=branch_id),
                validator=validator,
            )
        assert "ContextBundle" in str(excinfo.value)
        assert validator.calls == []


class TestB6InjectionMismatch:
    @pytest.mark.parametrize(
        "kwargs",
        [
            {"plan": "not-a-plan"},
            {"bundles": []},
            {"branch_return": "not-a-return"},
            {"validator": object()},
        ],
    )
    def test_wrong_injection_refused(self, kwargs: dict[str, object]) -> None:
        plan, branch_id = single_plan()
        base: dict[str, object] = {
            "plan": plan,
            "bundles": {branch_id: make_bundle()},
            "branch_return": make_return(branch_id=branch_id),
            "validator": RecordingValidator(judgment(satisfied=True)),
        }
        base.update(kwargs)
        with pytest.raises(ValidationWiringError):
            validate_branch(**base)  # type: ignore[arg-type]


class TestB7ContractFailureIsNotAbsorbed:
    def test_spec_bundle_mismatch_raises_contract_error(self) -> None:
        """A16.3：副本不一致 = **契约传递错误** ⇒ `[13]` 抛 `ContractError`。

        ⚠️ 接线**不得**把它吞掉、也不得降级成 `ValidationWiringError` 或 FAIL 记录。
        """
        plan, branch_id = single_plan()
        validator = RecordingValidator(judgment(satisfied=True))
        with pytest.raises(ContractError) as excinfo:
            validate_branch(
                plan=plan,
                bundles={branch_id: make_bundle(criteria="被篡改的副本")},
                branch_return=make_return(branch_id=branch_id),
                validator=validator,
            )
        assert "ContextBundle" in str(excinfo.value)

    def test_no_wiring_error_wrapping(self) -> None:
        plan, branch_id = single_plan()
        validator = RecordingValidator(judgment(satisfied=True))
        with pytest.raises(ContractError):
            validate_branch(
                plan=plan,
                bundles={branch_id: make_bundle(criteria="被篡改的副本")},
                branch_return=make_return(branch_id=branch_id),
                validator=validator,
            )


class TestB8NoRecordOnWiringFailure:
    def test_failure_path_has_no_side_effect(self) -> None:
        plan, branch_id = single_plan()
        validator = RecordingValidator(judgment(satisfied=True))
        for bad_bundles in ({}, {new_branch_id(): make_bundle()}):
            with pytest.raises(ValidationWiringError):
                validate_branch(
                    plan=plan,
                    bundles=bad_bundles,
                    branch_return=make_return(branch_id=branch_id),
                    validator=validator,
                )
        assert validator.calls == []


class TestB9OneEventPerCall:
    def test_each_call_is_a_new_event(self) -> None:
        plan, branch_id = single_plan()
        branch_return = make_return(branch_id=branch_id)
        validator = RecordingValidator(judgment(satisfied=True))
        first = validate_branch(
            plan=plan,
            bundles={branch_id: make_bundle()},
            branch_return=branch_return,
            validator=validator,
        )
        second = validate_branch(
            plan=plan,
            bundles={branch_id: make_bundle()},
            branch_return=branch_return,
            validator=validator,
        )
        assert first is not second, "每次调用必须是一个**新的**配对事件"
        assert len(validator.calls) == 2, "不得跨调用缓存判定结果"


class TestB10DirectAndStagedModes:
    def test_direct_plan_has_no_branch_to_pair(self) -> None:
        # DIRECT / STAGED_SINGLE_AGENT ⇒ 0 child ⇒ `bundles == {}`（A10.1b）
        plan = Plan(
            mode=ExecutionMode.DIRECT, main_thread_duty="直接执行", reason="简单任务"
        )
        validator = RecordingValidator(judgment(satisfied=True))
        with pytest.raises(ValidationWiringError):
            validate_branch(
                plan=plan,
                bundles={},
                branch_return=make_return(branch_id=new_branch_id()),
                validator=validator,
            )
        assert validator.calls == [], "接线缺陷**不得**先消耗一次判定"

    def test_wiring_does_not_invent_a_bundle(self) -> None:
        # 缺 bundle 时**显式失败**，绝不自己造一个（否则会产出"无上下文的验证"）
        plan, branch_id = single_plan()
        mutated_bundles: dict[str, ContextBundle] = {}
        validator = RecordingValidator(judgment(satisfied=True))
        with pytest.raises(ValidationWiringError):
            validate_branch(
                plan=plan,
                bundles=mutated_bundles,
                branch_return=make_return(branch_id=branch_id),
                validator=validator,
            )
        assert mutated_bundles == {}, "接线层不得写回 / 填充调用方的载体"


class TestB11IdentityPassthrough:
    def test_attempt_and_agent_id_unchanged(self) -> None:
        plan, branch_id = single_plan()
        branch_return = make_return(branch_id=branch_id, attempt=2)
        validator = RecordingValidator(judgment(satisfied=True))
        outcome = validate_branch(
            plan=plan,
            bundles={branch_id: make_bundle()},
            branch_return=branch_return,
            validator=validator,
        )
        assert outcome.branch_return.attempt == 2
        assert outcome.branch_return.agent_id == branch_return.agent_id
        assert outcome.validation.branch_id == branch_id


class TestB12NoSelfReportInWiring:
    def test_self_reported_success_does_not_change_the_pairing(self) -> None:
        # 分支自报 "ok" 但依据不足 ⇒ 配对照样产出，且结论仍是 FAIL（INV-19）
        plan, branch_id = single_plan()
        branch_return = make_return(branch_id=branch_id)
        assert branch_return.validation_state == "ok"
        validator = RecordingValidator(judgment(satisfied=False))
        outcome = validate_branch(
            plan=plan,
            bundles={branch_id: make_bundle()},
            branch_return=branch_return,
            validator=validator,
        )
        assert outcome.validation.passed is False
        assert outcome.branch_return.validation_state == "ok"

    def test_pair_is_losslessly_reusable(self) -> None:
        """A11.4b：``ReusableResultRef`` 可以从**一个对象**派生（不跨集合重配对）。"""
        from adaptive_router.reusable_results import ReusableResultRef

        plan, branch_id = single_plan()
        validator = RecordingValidator(judgment(satisfied=True))
        outcome = validate_branch(
            plan=plan,
            bundles={branch_id: make_bundle()},
            branch_return=make_return(branch_id=branch_id),
            validator=validator,
        )
        ref = ReusableResultRef(
            source_branch_id=outcome.branch_return.branch_id,
            branch_return=outcome.branch_return,
            validation=outcome.validation,
        )
        assert ref.branch_return is outcome.branch_return
        assert ref.validation is outcome.validation
