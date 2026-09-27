"""``[13] Validation`` —— 结构锁 LV-1 – LV-8 + 行为测试 B1 – B11。

架构依据：`docs/architecture.md` 2 节 ``[13] Validation``（**Amendment A16**，FROZEN，
含不变量 **INV-19 — Validation 独立判定**）。
范围、锁清单、裁定与 probe 计划：`tests/behavior/README_step5b.md` §72。

**本模块要守住的两条主张：**

  1. **判定权归 `[13]`**（PASS/FAIL 由它做）—— 见 B1 / B2 / B6；
  2. **`[13]` 没有事实发明权**（A16.1）—— ``passed`` 只由**判定依据**推出，
     **不复制** ``BranchReturn`` 的自报字段（``validation_state`` / 证据字段）—— 见 LV-2 与 B3。

⚠️ **禁止子串扫描**：本文件的注释与 docstring 里**故意提及** ``validation_state`` /
``bundle.completion_criteria`` / ``RecoveryDecision`` 等词（说明边界）⇒
所有锁一律 **AST 级**（``referenced_names`` 只取 ``Name`` / ``Attribute`` / import，
注释与 docstring **天然不参与**）。

⚠️ 敏感性 probe 一律在**源码字符串**上完成，**不覆写** ``src/`` 里的真实文件；
真实源码的一次性变异证据另见 README §73（含逐字节还原核对）。
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
    ExecutionValue,
    FailureClass,
    ModelTier,
    ValidationRecord,
    new_agent_id,
    new_branch_id,
)
from adaptive_router.validation import Validator
from adaptive_router.validation_judgment import CriterionVerdict, ValidationJudgment

from _validation_locks import (
    assignment_value_shapes,
    attribute_chains,
    call_positional_shapes,
    calls_to,
    class_def_names,
    clock_call_hits,
    containment_checks,
    dataclass_field_names,
    exception_subclass_names,
    forbidden_import_hits,
    function_call_keyword_values,
    handled_exception_names,
    imported_module_tokens,
    manager_self_attribute_reads,
    mapping_get_calls,
    method_flow_shapes,
    module_level_container_assignments,
    open_calls,
    raise_guards,
    referenced_names,
    signature_shape,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
SRC = REPO_ROOT / "src/adaptive_router"
VALIDATION_SOURCE = (SRC / "validation.py").read_text(encoding="utf-8")
JUDGMENT_SOURCE = (SRC / "validation_judgment.py").read_text(encoding="utf-8")

#: LV-1 —— `[13]` 的 import token **精确集合**（实测，见 README §73.3）。
#: 与 §72.7 的 LV-1 草案一致：`[13]` 侧不需要 `dataclasses`（DTO 在 seam 模块里）。
ALLOWED_IMPORT_TOKENS = frozenset(
    {
        "__future__",
        "annotations",
        "typing",
        "Callable",
        "contracts",
        "validation_judgment",
    }
)

#: LV-1 —— 禁用清单（决策 / 执行 / 成本 / 适配 / 恢复层一律不得进入 `[13]`）。
#:
#: ⚠️ `recovery` 在列**是 A16.1 的落点**：``PASS → 终态`` / ``FAIL → [14]`` 的分叉属 wiring，
#: 不属于 `[13]` —— 一旦 `[13]` 能 import `[14]`，它就有了"自己决定失败去路"的能力。
FORBIDDEN_IMPORTS = (
    "scheduler",
    "agent_manager",
    "planner",
    "model_router",
    "policy",
    "cost",
    "recovery",
    "execution",
    "execution_results",
    "result_handoff",
    "adapters",
    "approval_judgment",
    "approval_disclosure",
    "risk",
    "capabilities",
    "project_context",
)

#: LV-2 —— **INV-19 的机械落点**：回传里的**自报字段**，`[13]` 一个都不准读。
SELF_REPORT_NAMES = (
    "validation_state",
    "key_evidence",
    "conclusion_summary",
    "residual_risks",
    "main_agent_todo",
)

#: LV-2 / LV-6 —— 由 verdicts **派生**的量及其冻结形态（实测，见 README §73.3）。
DERIVED_SHAPES = {
    "passed": ("all((verdict.satisfied for verdict in judgment.verdicts))",),
    "failed_criteria": (
        "tuple((verdict.criterion for verdict in judgment.verdicts"
        " if not verdict.satisfied))",
    ),
    "evidence": (
        "_ordered_unique(tuple((item for verdict in judgment.verdicts"
        " for item in verdict.evidence)))",
    ),
}

#: LV-4 —— `ValidationRecord` 唯一构造点的**逐 keyword 实参形态**。
RECORD_KEYWORD_SHAPES = {
    "branch_id": ("branch_return.branch_id",),
    "passed": ("passed",),
    "evidence": ("evidence",),
    "failed_criteria": ("failed_criteria",),
    "detail": ("judgment.detail",),
}

#: LV-4（裁定 ⑥）—— `validate_spec` 的**流程骨架**
#: （身份 → 标准 → 取依据 → 锚定 → 判定 → 装配）。
FLOW_SHAPES = (
    ("if", "branch_return.branch_id != spec.branch_id"),
    ("if", "spec.completion_criteria != bundle.completion_criteria"),
    (
        "assign",
        "judgment",
        "self._judgment_source(branch_return, spec.completion_criteria)",
    ),
    ("if", "not isinstance(judgment, ValidationJudgment)"),
    ("if", "not judgment.verdicts"),
    ("for", "verdict", "judgment.verdicts"),
    ("assign", "passed", DERIVED_SHAPES["passed"][0]),
    ("assign", "failed_criteria", DERIVED_SHAPES["failed_criteria"][0]),
    ("assign", "evidence", DERIVED_SHAPES["evidence"][0]),
    (
        "return",
        "ValidationRecord(branch_id=branch_return.branch_id, passed=passed,"
        " evidence=evidence, failed_criteria=failed_criteria, detail=judgment.detail)",
    ),
)

#: LV-4 / LV-5 —— **显式失败点**：每个 ``raise`` 的守卫条件与异常类型（实测，见 README §73.3）。
#: 没有这条锁，"把 raise 换成 pass"这类**静默降级**是看不见的
#: （锚定的包含判断还在、`for` 还在，但失败点没了）。
RAISE_GUARDS_SPEC = (
    ("branch_return.branch_id != spec.branch_id", "ContractError"),
    ("spec.completion_criteria != bundle.completion_criteria", "ContractError"),
    ("not isinstance(judgment, ValidationJudgment)", "ContractError"),
    ("not judgment.verdicts", "ContractError"),
    ("verdict.criterion.strip() not in spec.completion_criteria", "ContractError"),
)

#: B 系列用的权威完成标准（两条目，便于"部分满足"）。
CRITERIA = "1) 文档已更新；2) 测试通过"


# --------------------------------------------------------------------------
# 测试替身
# --------------------------------------------------------------------------


def make_return(
    *, branch_id: str | None = None, validation_state: str = "ok"
) -> BranchReturn:
    """造一个合法 ``BranchReturn``。

    ⚠️ ``validation_state`` 是**分支自报**（INV-19 的诱饵）：B3 会把它设成"通过"，
    用来证明 `[13]` 的结论**不随它变化**。
    """
    return BranchReturn(
        branch_id=branch_id if branch_id is not None else new_branch_id(),
        agent_id=new_agent_id(),
        attempt=1,
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
        validation_state=validation_state,
        residual_risks=(),
        main_agent_todo=(),
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


def verdict(
    *, criterion: str, satisfied: bool, evidence: tuple[str, ...] = ()
) -> CriterionVerdict:
    return CriterionVerdict(criterion=criterion, satisfied=satisfied, evidence=evidence)


def source_returning(
    judgment: ValidationJudgment, *, calls: list[tuple[str, str]] | None = None
):
    """注入式 producer：**记录调用**并返回给定判定依据（裁定 ③：是可调用，不是类）。"""

    def judgment_source(
        branch_return: BranchReturn, criteria: str
    ) -> ValidationJudgment:
        if calls is not None:
            calls.append((branch_return.branch_id, criteria))
        return judgment

    return judgment_source


def full_source(**kwargs: object):
    """一个"依据齐全"的 producer（两条标准都满足）。"""
    return source_returning(
        ValidationJudgment(
            verdicts=(
                verdict(criterion="1) 文档已更新", satisfied=True, evidence=("diff",)),
                verdict(criterion="2) 测试通过", satisfied=True, evidence=("pytest",)),
            ),
            detail="两条都满足",
        ),
        **kwargs,  # type: ignore[arg-type]
    )


def failing_source(*, criterion: str, evidence: tuple[str, ...] = ("自报 ok",)):
    """一个"依据不足"的 producer（指定条目未满足）。"""
    return source_returning(
        ValidationJudgment(
            verdicts=(
                verdict(criterion=criterion, satisfied=False, evidence=evidence),
            ),
            detail="一条未满足",
        )
    )


# ==========================================================================
# 结构锁 LV-1 – LV-8
# ==========================================================================


class TestLV1ImportBoundary:
    def test_exact_token_set(self) -> None:
        tokens = set(imported_module_tokens(VALIDATION_SOURCE))
        assert tokens == set(ALLOWED_IMPORT_TOKENS), (
            f"新增：{sorted(tokens - set(ALLOWED_IMPORT_TOKENS))}；"
            f"消失：{sorted(set(ALLOWED_IMPORT_TOKENS) - tokens)}"
        )

    def test_forbidden_imports_empty(self) -> None:
        assert forbidden_import_hits(VALIDATION_SOURCE, FORBIDDEN_IMPORTS) == set()

    def test_dto_module_is_standalone(self) -> None:
        # A16.7：seam DTO **不进** `contracts/`，也不依赖契约层 —— 它只定义"依据长什么样"
        assert set(imported_module_tokens(JUDGMENT_SOURCE)) == {
            "__future__",
            "annotations",
            "dataclasses",
            "dataclass",
        }

    @pytest.mark.parametrize(
        "needle",
        [
            "\nfrom .recovery import RecoveryRouter  # noqa\n",
            "\nfrom . import recovery  # noqa\n",
            "\nfrom .scheduler import Scheduler  # noqa\n",
            "\nfrom .execution_results import build_branch_return  # noqa\n",
            "\nfrom . import adapters  # noqa\n",
        ],
    )
    def test_probe_forbidden_import_goes_red(self, needle: str) -> None:
        assert (
            forbidden_import_hits(VALIDATION_SOURCE + needle, FORBIDDEN_IMPORTS)
            != set()
        )


class TestLV2ValidationIndependence:
    """**INV-19**：``passed`` 只能来自判定依据，**不得**是分支自报状态的投影。"""

    def test_self_report_names_never_referenced(self) -> None:
        names = set(referenced_names(VALIDATION_SOURCE))
        assert names & set(SELF_REPORT_NAMES) == set()

    def test_return_is_read_for_identity_only(self) -> None:
        read = {
            chain
            for chain in attribute_chains(VALIDATION_SOURCE)
            if chain.startswith("branch_return.")
        }
        assert read == {"branch_return.branch_id"}

    def test_passed_derived_from_verdicts(self) -> None:
        assert (
            assignment_value_shapes(VALIDATION_SOURCE)["passed"]
            == DERIVED_SHAPES["passed"]
        )

    def test_record_passed_is_the_derived_local(self) -> None:
        assert (
            function_call_keyword_values(VALIDATION_SOURCE, "ValidationRecord", "passed")
            == RECORD_KEYWORD_SHAPES["passed"]
        )

    def test_empty_basis_guard_is_explicit(self) -> None:
        # 空 verdicts 必须被**显式**拒绝（`all(())` 是 True ⇒ 会静默判 PASS）
        assert ("if", "not self.verdicts") in method_flow_shapes(
            JUDGMENT_SOURCE, "ValidationJudgment", "__post_init__"
        )
        assert ("not self.verdicts", "ValueError") in raise_guards(
            JUDGMENT_SOURCE, "ValidationJudgment", "__post_init__"
        )
        assert ("if", "not judgment.verdicts") in FLOW_SHAPES

    @pytest.mark.parametrize("field", SELF_REPORT_NAMES)
    def test_probe_reading_self_report_goes_red(self, field: str) -> None:
        needle = f"\n    _probe = branch_return.{field}  # noqa\n"
        mutated = VALIDATION_SOURCE.replace(
            "\n\n    def validate_spec(", needle + "\n    def validate_spec(", 1
        )
        assert mutated != VALIDATION_SOURCE
        names = set(referenced_names(mutated))
        read = {
            chain
            for chain in attribute_chains(mutated)
            if chain.startswith("branch_return.")
        }
        assert (names & set(SELF_REPORT_NAMES)) != set()
        assert read != {"branch_return.branch_id"}

    def test_probe_passed_from_self_report_goes_red(self) -> None:
        # 保留名字 `passed`、只换右侧 —— 这正是"只锁名字拦不住"的情形，
        # 所以 LV-2 同时锁**右侧形态**。
        original = "passed = all(verdict.satisfied for verdict in judgment.verdicts)"
        mutated = VALIDATION_SOURCE.replace(
            original, 'passed = branch_return.validation_state == "ok"'
        )
        assert mutated != VALIDATION_SOURCE
        assert (
            assignment_value_shapes(mutated)["passed"] != DERIVED_SHAPES["passed"]
        )
        assert "validation_state" in set(referenced_names(mutated))

    def test_probe_dropped_empty_guard_goes_red(self) -> None:
        mutated = JUDGMENT_SOURCE.replace(
            "        if not self.verdicts:\n"
            "            raise ValueError(\n"
            '                "verdicts 不得为空（空元组会让 all(...) 静默判 PASS；'
            'A16.2 要求给出判定依据）"\n'
            "            )\n",
            "",
        )
        assert mutated != JUDGMENT_SOURCE
        assert ("if", "not self.verdicts") not in method_flow_shapes(
            mutated, "ValidationJudgment", "__post_init__"
        )


class TestLV3SingleProducerCall:
    def test_producer_called_exactly_once(self) -> None:
        assert len(calls_to(VALIDATION_SOURCE, ("_judgment_source",))) == 1

    def test_13_never_constructs_a_judgment(self) -> None:
        # A16.1：`[13]` **没有事实发明权** —— 它不得自己造判定依据
        assert calls_to(VALIDATION_SOURCE, ("ValidationJudgment",)) == []
        assert calls_to(VALIDATION_SOURCE, ("CriterionVerdict",)) == []

    def test_producer_is_injected_callable(self) -> None:
        assert signature_shape(VALIDATION_SOURCE, "Validator", "__init__") == {
            "positional": ["self"],
            "kwonly": ["judgment_source"],
            "defaults": [False],
            "vararg": None,
            "kwarg": None,
        }

    def test_producer_arg_is_the_authority_text(self) -> None:
        # 传给 producer 的第二个实参必须是 **spec**（权威来源），不是 bundle（传递副本）
        assert call_positional_shapes(VALIDATION_SOURCE, "_judgment_source") == (
            ("branch_return", "spec.completion_criteria"),
        )

    def test_seam_dto_field_shapes(self) -> None:
        assert dataclass_field_names(JUDGMENT_SOURCE, "CriterionVerdict") == (
            "criterion",
            "satisfied",
            "evidence",
        )
        assert dataclass_field_names(JUDGMENT_SOURCE, "ValidationJudgment") == (
            "verdicts",
            "detail",
        )

    def test_probe_extra_dto_field_goes_red(self) -> None:
        mutated = JUDGMENT_SOURCE.replace(
            "    verdicts: tuple[CriterionVerdict, ...]\n    detail: str\n",
            "    verdicts: tuple[CriterionVerdict, ...]\n    detail: str\n    note: str\n",
        )
        assert mutated != JUDGMENT_SOURCE
        assert dataclass_field_names(mutated, "ValidationJudgment") != (
            "verdicts",
            "detail",
        )


class TestLV4AuthorityIsTheSpec:
    def test_validate_spec_signature(self) -> None:
        assert signature_shape(VALIDATION_SOURCE, "Validator", "validate_spec") == {
            "positional": ["self"],
            "kwonly": ["branch_return", "spec", "bundle"],
            "defaults": [False, False, False],
            "vararg": None,
            "kwarg": None,
        }

    def test_flow_order_is_frozen(self) -> None:
        assert (
            method_flow_shapes(VALIDATION_SOURCE, "Validator", "validate_spec")
            == FLOW_SHAPES
        )

    def test_explicit_failure_points_are_frozen(self) -> None:
        # 五个显式失败点：身份 / 标准一致 / 依据类型 / 依据非空 / 锚定。
        # 顺序与守卫条件都属于契约（裁定 ⑥ + A16.3 + A16.4）。
        assert (
            raise_guards(VALIDATION_SOURCE, "Validator", "validate_spec")
            == RAISE_GUARDS_SPEC
        )

    def test_bundle_copy_is_read_once(self) -> None:
        # A16.3：bundle 的副本**只用于一致性核对**（一次读取），不得成为判定输入
        assert attribute_chains(VALIDATION_SOURCE).count("bundle.completion_criteria") == 1
        assert attribute_chains(VALIDATION_SOURCE).count("spec.completion_criteria") == 3

    def test_probe_bundle_as_authority_goes_red(self) -> None:
        mutated = VALIDATION_SOURCE.replace(
            "self._judgment_source(branch_return, spec.completion_criteria)",
            "self._judgment_source(branch_return, bundle.completion_criteria)",
        )
        assert mutated != VALIDATION_SOURCE
        assert call_positional_shapes(mutated, "_judgment_source") != (
            ("branch_return", "spec.completion_criteria"),
        )
        assert method_flow_shapes(mutated, "Validator", "validate_spec") != FLOW_SHAPES

    def test_probe_reordered_checks_go_red(self) -> None:
        # 裁定 ⑥：身份校验必须**先于**标准核对，标准核对必须**先于**取判定依据。
        # 交换前两条 if 的**条件**（其余文本不动）⇒ 流程锁必须红。
        identity = "if branch_return.branch_id != spec.branch_id:"
        criteria = "if spec.completion_criteria != bundle.completion_criteria:"
        assert identity in VALIDATION_SOURCE and criteria in VALIDATION_SOURCE
        mutated = (
            VALIDATION_SOURCE.replace(identity, "@@A@@")
            .replace(criteria, identity)
            .replace("@@A@@", criteria)
        )
        assert mutated != VALIDATION_SOURCE
        assert method_flow_shapes(mutated, "Validator", "validate_spec") != FLOW_SHAPES

    def test_probe_dropped_consistency_check_goes_red(self) -> None:
        # 删掉标准核对（bundle 副本不再被核对）⇒ 流程锁 + 出现次数锁同时红
        mutated = VALIDATION_SOURCE.replace(
            "        if spec.completion_criteria != bundle.completion_criteria:\n"
            "            raise ContractError(\n"
            '                f"{spec.branch_id} 的完成标准在 Plan 与 ContextBundle 之间不一致"\n'
            '                "（A16.3：这是契约传递错误，不判 FAIL）"\n'
            "            )\n",
            "",
        )
        assert mutated != VALIDATION_SOURCE
        assert attribute_chains(mutated).count("bundle.completion_criteria") == 0


class TestLV5Anchoring:
    def test_anchoring_check_shape(self) -> None:
        assert containment_checks(VALIDATION_SOURCE) == (
            ("value", "seen"),
            ("verdict.criterion.strip()", "spec.completion_criteria"),
        )

    def test_probe_unanchored_criterion_goes_red(self) -> None:
        # 去掉 `.strip()`：空白条目会变成"永远锚定成功"的漏洞
        mutated = VALIDATION_SOURCE.replace(
            "if verdict.criterion.strip() not in spec.completion_criteria:",
            "if verdict.criterion not in spec.completion_criteria:",
        )
        assert mutated != VALIDATION_SOURCE
        assert containment_checks(mutated) != containment_checks(VALIDATION_SOURCE)

    def test_probe_anchoring_against_bundle_goes_red(self) -> None:
        mutated = VALIDATION_SOURCE.replace(
            "if verdict.criterion.strip() not in spec.completion_criteria:",
            "if verdict.criterion.strip() not in bundle.completion_criteria:",
        )
        assert mutated != VALIDATION_SOURCE
        assert ("verdict.criterion.strip()", "bundle.completion_criteria") in (
            containment_checks(mutated)
        )

    def test_probe_dropped_anchoring_raise_goes_red(self) -> None:
        # 锚定失败不再显式失败（改成 pass）⇒ **显式失败点锁**必须红。
        # ⚠️ 注意：包含判断本身还在（`for` / `if` 都还在）——
        # 只靠 `containment_checks` 是**看不见**这次静默降级的，所以本条锁是必需的。
        mutated = VALIDATION_SOURCE.replace(
            "                raise ContractError(\n"
            '                    f"判定条目 {verdict.criterion!r} 不是权威完成标准的片段"\n'
            '                    "（producer 不得引入权威文本之外的标准）"\n'
            "                )\n",
            "                pass\n",
        )
        assert mutated != VALIDATION_SOURCE
        assert containment_checks(mutated) == containment_checks(VALIDATION_SOURCE)
        assert (
            raise_guards(mutated, "Validator", "validate_spec")
            == RAISE_GUARDS_SPEC[:-1]
        )


class TestLV6DerivedFailures:
    def test_failed_criteria_derived_from_verdicts(self) -> None:
        shapes = assignment_value_shapes(VALIDATION_SOURCE)
        assert shapes["failed_criteria"] == DERIVED_SHAPES["failed_criteria"]
        assert shapes["evidence"] == DERIVED_SHAPES["evidence"]

    def test_record_keywords_are_locals(self) -> None:
        for keyword, expected in RECORD_KEYWORD_SHAPES.items():
            assert (
                function_call_keyword_values(
                    VALIDATION_SOURCE, "ValidationRecord", keyword
                )
                == expected
            ), keyword

    def test_single_construction_site(self) -> None:
        # 唯一构造点（§72.7 LV-6）：多一处就是"记录可以由第二个地方拼出来"
        assert len(calls_to(VALIDATION_SOURCE, ("ValidationRecord",))) == 1

    def test_probe_second_construction_site_goes_red(self) -> None:
        mutated = VALIDATION_SOURCE + (
            "\n\ndef _second_site(record: ValidationRecord) -> ValidationRecord:\n"
            "    return ValidationRecord(\n"
            "        branch_id=record.branch_id,\n"
            "        passed=record.passed,\n"
            "        evidence=record.evidence,\n"
            "        failed_criteria=record.failed_criteria,\n"
            "        detail=record.detail,\n"
            "    )\n"
        )
        assert len(calls_to(mutated, ("ValidationRecord",))) == 2
        assert (
            function_call_keyword_values(mutated, "ValidationRecord", "passed")
            != RECORD_KEYWORD_SHAPES["passed"]
        )

    def test_probe_failed_criteria_from_self_report_goes_red(self) -> None:
        mutated = VALIDATION_SOURCE.replace(
            "        failed_criteria = tuple(\n"
            "            verdict.criterion for verdict in judgment.verdicts"
            " if not verdict.satisfied\n"
            "        )\n",
            "        failed_criteria = branch_return.main_agent_todo\n",
        )
        assert mutated != VALIDATION_SOURCE
        assert (
            assignment_value_shapes(mutated)["failed_criteria"]
            != DERIVED_SHAPES["failed_criteria"]
        )


class TestLV7NoHiddenState:
    def test_only_the_injected_source_is_read(self) -> None:
        # `[13]` 的实例状态**只有**注入的 producer 本身（无缓存、无计数、无时钟）
        assert manager_self_attribute_reads(VALIDATION_SOURCE) == {"_judgment_source"}

    def test_no_module_level_state(self) -> None:
        assert module_level_container_assignments(VALIDATION_SOURCE) == ()
        assert module_level_container_assignments(JUDGMENT_SOURCE) == ()

    def test_no_clock_and_no_io(self) -> None:
        assert clock_call_hits(VALIDATION_SOURCE) == []
        assert open_calls(VALIDATION_SOURCE) == []
        # `Mapping.get(..., default)` 是"静默默认"的常见入口（§72.7 LV-7）
        assert mapping_get_calls(VALIDATION_SOURCE) == []

    def test_class_surface(self) -> None:
        assert class_def_names(VALIDATION_SOURCE) == ("Validator",)
        assert class_def_names(JUDGMENT_SOURCE) == (
            "CriterionVerdict",
            "ValidationJudgment",
        )


class TestLV8NoSwallowedFailure:
    def test_no_custom_exception_type(self) -> None:
        assert exception_subclass_names(VALIDATION_SOURCE) == ()

    def test_no_try_except(self) -> None:
        # 契约不一致必须**显式失败**，不得吞掉后降级成 FAIL
        #（那会把接线缺陷吸收成"分支没做到"）
        assert handled_exception_names(VALIDATION_SOURCE) == ()


# ==========================================================================
# 行为测试 B1 – B11
# ==========================================================================


class TestB1PassPath:
    def test_all_satisfied_passes(self) -> None:
        branch_id = new_branch_id()
        record = Validator(judgment_source=full_source()).validate_spec(
            branch_return=make_return(branch_id=branch_id),
            spec=make_spec(branch_id=branch_id),
            bundle=make_bundle(),
        )
        assert isinstance(record, ValidationRecord)
        assert record.branch_id == branch_id
        assert record.passed is True
        assert record.failed_criteria == ()
        assert record.evidence == ("diff", "pytest")
        assert record.detail == "两条都满足"


class TestB2FailPath:
    def test_one_unsatisfied_fails_with_that_criterion(self) -> None:
        branch_id = new_branch_id()
        record = Validator(
            judgment_source=source_returning(
                ValidationJudgment(
                    verdicts=(
                        verdict(
                            criterion="1) 文档已更新", satisfied=True, evidence=("diff",)
                        ),
                        verdict(
                            criterion="2) 测试通过",
                            satisfied=False,
                            evidence=("pytest 未运行",),
                        ),
                    ),
                    detail="第二条缺少证据",
                )
            )
        ).validate_spec(
            branch_return=make_return(branch_id=branch_id),
            spec=make_spec(branch_id=branch_id),
            bundle=make_bundle(),
        )
        assert record.passed is False
        assert record.failed_criteria == ("2) 测试通过",)
        assert record.detail == "第二条缺少证据"
        assert record.evidence == ("diff", "pytest 未运行")

    def test_failed_entry_points_back_to_authority_and_maps_to_l49(self) -> None:
        """TEST-VALID-002：失败条目必须能**回指**权威标准，从而映射到 L49 的完成标准类失败。"""
        branch_id = new_branch_id()
        record = Validator(
            judgment_source=failing_source(criterion="2) 测试通过")
        ).validate_spec(
            branch_return=make_return(branch_id=branch_id),
            spec=make_spec(branch_id=branch_id),
            bundle=make_bundle(),
        )
        assert FailureClass.COMPLETION_CRITERIA.value == "completion_criteria"
        assert record.failed_criteria, "FAIL 必须给出未满足的标准（供 [14] 分类）"
        for criterion in record.failed_criteria:
            assert criterion.strip() in CRITERIA


class TestB3ValidationIndependence:
    """**TEST-VALID-001 / INV-19**：自报状态**不参与**判定。"""

    @pytest.mark.parametrize("self_report", ["ok", "通过", "全部完成"])
    def test_self_reported_success_without_basis_must_not_pass(
        self, self_report: str
    ) -> None:
        branch_id = new_branch_id()
        record = Validator(
            judgment_source=failing_source(criterion="1) 文档已更新")
        ).validate_spec(
            branch_return=make_return(branch_id=branch_id, validation_state=self_report),
            spec=make_spec(branch_id=branch_id),
            bundle=make_bundle(),
        )
        assert record.passed is False
        assert record.failed_criteria == ("1) 文档已更新",)

    @pytest.mark.parametrize("self_report", ["失败", "未验证", "blocked"])
    def test_self_reported_failure_with_full_basis_still_passes(
        self, self_report: str
    ) -> None:
        # 反方向同样重要：`[13]` 也不是"照抄自报失败"，它按依据判
        branch_id = new_branch_id()
        record = Validator(judgment_source=full_source()).validate_spec(
            branch_return=make_return(branch_id=branch_id, validation_state=self_report),
            spec=make_spec(branch_id=branch_id),
            bundle=make_bundle(),
        )
        assert record.passed is True
        assert record.failed_criteria == ()

    def test_record_carries_no_self_report_field(self) -> None:
        # 结论载体里**没有**可以塞自报状态的位置
        assert set(SELF_REPORT_NAMES) & set(ValidationRecord.__dataclass_fields__) == set()


class TestB4IdentityMismatch:
    def test_cross_branch_check_refused(self) -> None:
        calls: list[tuple[str, str]] = []
        validator = Validator(
            judgment_source=source_returning(
                ValidationJudgment(
                    verdicts=(verdict(criterion="1) 文档已更新", satisfied=True),),
                    detail="不应当被调用",
                ),
                calls=calls,
            )
        )
        with pytest.raises(ContractError) as excinfo:
            validator.validate_spec(
                branch_return=make_return(branch_id=new_branch_id()),
                spec=make_spec(branch_id=new_branch_id()),
                bundle=make_bundle(),
            )
        assert "身份" in str(excinfo.value)
        assert calls == [], "身份不一致时**不得**调用 producer"


class TestB5CriteriaMismatchIsNotFail:
    """A16.3：Plan 与 bundle 的标准不一致 = **契约传递错误** ⇒ 显式失败，**不判 FAIL**。"""

    @pytest.mark.parametrize(
        "copy_text",
        [
            CRITERIA + " ",  # 尾随空白（逐字比较，不做规范化）
            "1) 文档已更新; 2) 测试通过",  # 标点差异
            "1) 文档已更新",  # 只传了第一条（副本被截断）
        ],
    )
    def test_verbatim_mismatch_raises(self, copy_text: str) -> None:
        calls: list[tuple[str, str]] = []
        branch_id = new_branch_id()
        validator = Validator(
            judgment_source=source_returning(
                ValidationJudgment(
                    verdicts=(verdict(criterion="1) 文档已更新", satisfied=True),),
                    detail="不应当被调用",
                ),
                calls=calls,
            )
        )
        with pytest.raises(ContractError) as excinfo:
            validator.validate_spec(
                branch_return=make_return(branch_id=branch_id),
                spec=make_spec(branch_id=branch_id),
                bundle=make_bundle(criteria=copy_text),
            )
        assert "ContextBundle" in str(excinfo.value)
        assert calls == [], "标准不一致时**不得**调用 producer"

    def test_mismatch_never_produces_a_record(self) -> None:
        # 若把不一致吸收成 FAIL，接线缺陷就会被记成"分支没做到" —— 这是本测试的防线
        branch_id = new_branch_id()
        validator = Validator(judgment_source=failing_source(criterion="1) 文档已更新"))
        with pytest.raises(ContractError):
            validator.validate_spec(
                branch_return=make_return(branch_id=branch_id),
                spec=make_spec(branch_id=branch_id),
                bundle=make_bundle(criteria="完全不同的标准"),
            )


class TestB6AnchoringViolation:
    def test_foreign_criterion_refused(self) -> None:
        branch_id = new_branch_id()
        validator = Validator(
            judgment_source=source_returning(
                ValidationJudgment(
                    verdicts=(
                        verdict(criterion="3) 顺便重构了模块", satisfied=True),
                    ),
                    detail="依据引入了权威文本之外的标准",
                )
            )
        )
        with pytest.raises(ContractError) as excinfo:
            validator.validate_spec(
                branch_return=make_return(branch_id=branch_id),
                spec=make_spec(branch_id=branch_id),
                bundle=make_bundle(),
            )
        assert "权威完成标准" in str(excinfo.value)

    def test_equal_to_authority_is_allowed(self) -> None:
        # 裁定 ②：**子串或相等**都算锚定成功（整条标准作为一个 verdict）
        branch_id = new_branch_id()
        record = Validator(
            judgment_source=source_returning(
                ValidationJudgment(
                    verdicts=(
                        verdict(criterion=CRITERIA, satisfied=True, evidence=("e",)),
                    ),
                    detail="整条判定",
                )
            )
        ).validate_spec(
            branch_return=make_return(branch_id=branch_id),
            spec=make_spec(branch_id=branch_id),
            bundle=make_bundle(),
        )
        assert record.passed is True
        assert record.evidence == ("e",)


class TestB7EmptyOrWrongBasis:
    def test_dto_refuses_empty_verdicts(self) -> None:
        with pytest.raises(ValueError):
            ValidationJudgment(verdicts=(), detail="空")

    @pytest.mark.parametrize("blank", ["", "   ", "\n"])
    def test_dto_refuses_blank_criterion(self, blank: str) -> None:
        # 空白条目会让 `"" in authority` 永远成立 ⇒ DTO 层就堵死
        with pytest.raises(ValueError):
            verdict(criterion=blank, satisfied=True)

    def test_13_refuses_empty_verdicts_even_if_dto_weakened(self) -> None:
        # 双保险：若 DTO 的不变量被削弱（含被绕过），`[13]` 仍必须拒绝
        class EmptyJudgment(ValidationJudgment):
            def __post_init__(self) -> None:  # 模拟"契约被削弱"
                object.__setattr__(self, "verdicts", ())
                object.__setattr__(self, "detail", "空")

        branch_id = new_branch_id()
        validator = Validator(judgment_source=source_returning(EmptyJudgment((), "空")))
        with pytest.raises(ContractError) as excinfo:
            validator.validate_spec(
                branch_return=make_return(branch_id=branch_id),
                spec=make_spec(branch_id=branch_id),
                bundle=make_bundle(),
            )
        assert "不得为空" in str(excinfo.value)

    def test_wrong_type_from_producer_refused(self) -> None:
        branch_id = new_branch_id()
        validator = Validator(
            judgment_source=lambda _r, _c: "ok"  # type: ignore[arg-type,return-value]
        )
        with pytest.raises(ContractError) as excinfo:
            validator.validate_spec(
                branch_return=make_return(branch_id=branch_id),
                spec=make_spec(branch_id=branch_id),
                bundle=make_bundle(),
            )
        assert "ValidationJudgment" in str(excinfo.value)

    def test_dto_refuses_non_bool_satisfied(self) -> None:
        with pytest.raises(TypeError):
            CriterionVerdict(  # type: ignore[arg-type]
                criterion="1) 文档已更新", satisfied="yes", evidence=()
            )

    def test_dto_refuses_non_tuple_evidence(self) -> None:
        with pytest.raises(TypeError):
            CriterionVerdict(  # type: ignore[arg-type]
                criterion="1) 文档已更新", satisfied=True, evidence=["e"]
            )

    def test_dto_refuses_foreign_verdict_type(self) -> None:
        with pytest.raises(TypeError):
            ValidationJudgment(verdicts=("1) 文档已更新",), detail="类型不对")  # type: ignore[arg-type]


class TestB8ProducerContract:
    def test_producer_receives_branch_id_and_authority(self) -> None:
        calls: list[tuple[str, str]] = []
        branch_id = new_branch_id()
        Validator(judgment_source=full_source(calls=calls)).validate_spec(
            branch_return=make_return(branch_id=branch_id),
            spec=make_spec(branch_id=branch_id),
            bundle=make_bundle(),
        )
        assert calls == [(branch_id, CRITERIA)]

    def test_producer_called_once_per_validation(self) -> None:
        calls: list[tuple[str, str]] = []
        branch_id = new_branch_id()
        validator = Validator(judgment_source=full_source(calls=calls))
        for _ in range(2):
            validator.validate_spec(
                branch_return=make_return(branch_id=branch_id),
                spec=make_spec(branch_id=branch_id),
                bundle=make_bundle(),
            )
        assert len(calls) == 2, "每次验证调用一次；不得跨调用缓存判定依据"


class TestB9StatelessAndDeterministic:
    def test_same_inputs_same_record(self) -> None:
        branch_id = new_branch_id()
        judgment = ValidationJudgment(
            verdicts=(
                verdict(
                    criterion="1) 文档已更新", satisfied=True, evidence=("x",)
                ),
                verdict(criterion="2) 测试通过", satisfied=False, evidence=("y",)),
            ),
            detail="一条未满足",
        )
        validator = Validator(judgment_source=source_returning(judgment))
        first = validator.validate_spec(
            branch_return=make_return(branch_id=branch_id),
            spec=make_spec(branch_id=branch_id),
            bundle=make_bundle(),
        )
        second = validator.validate_spec(
            branch_return=make_return(branch_id=branch_id),
            spec=make_spec(branch_id=branch_id),
            bundle=make_bundle(),
        )
        assert first == second


class TestB10EvidenceOrderedUnique:
    def test_order_preserved_and_duplicates_dropped(self) -> None:
        branch_id = new_branch_id()
        record = Validator(
            judgment_source=source_returning(
                ValidationJudgment(
                    verdicts=(
                        verdict(
                            criterion="1) 文档已更新",
                            satisfied=True,
                            evidence=("e1", "e2"),
                        ),
                        verdict(
                            criterion="2) 测试通过",
                            satisfied=True,
                            evidence=("e2", "e3", "e1"),
                        ),
                    ),
                    detail="ok",
                )
            )
        ).validate_spec(
            branch_return=make_return(branch_id=branch_id),
            spec=make_spec(branch_id=branch_id),
            bundle=make_bundle(),
        )
        assert record.evidence == ("e1", "e2", "e3")

    def test_no_evidence_is_allowed(self) -> None:
        branch_id = new_branch_id()
        record = Validator(
            judgment_source=source_returning(
                ValidationJudgment(
                    verdicts=(verdict(criterion="1) 文档已更新", satisfied=True),),
                    detail="无可引证据，但依据成立",
                )
            )
        ).validate_spec(
            branch_return=make_return(branch_id=branch_id),
            spec=make_spec(branch_id=branch_id),
            bundle=make_bundle(),
        )
        assert record.evidence == ()
        assert record.passed is True


class TestB11NoRecoveryRouting:
    def test_13_has_no_recovery_symbols(self) -> None:
        names = set(referenced_names(VALIDATION_SOURCE))
        assert names & {"RecoveryDecision", "RecoveryTarget", "RecoveryRouter"} == set()

    def test_validation_record_fields_are_the_frozen_five(self) -> None:
        assert tuple(ValidationRecord.__dataclass_fields__) == (
            "branch_id",
            "passed",
            "evidence",
            "failed_criteria",
            "detail",
        )
