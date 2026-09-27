"""Evaluator 行为测试（Step 5A ①）。

对应追溯矩阵（docs/architecture.md 第 8 节，Test ID 已冻结）：
  TEST-EVAL-001  低成本评估 11 个维度  → TestEvaluationIsElevenDimensions
  TEST-EVAL-002  先做低成本判断        → TestEvaluationIsACheapAssessment

架构依据（2 节 [04]）：
  输入：用户任务、ProjectContext、CapabilitySnapshot
  输出：11 维 Evaluation
  约束：[04] 只**读** [03] 的 Snapshot，**不得自行探测**
"""

from __future__ import annotations

import ast
import dataclasses
import pathlib

import pytest

from adaptive_router.contracts import (
    CapabilitySnapshot,
    Evaluation,
    ExecutionMode,
    ImpactScope,
    Ordinal,
    ProjectContext,
)
from adaptive_router.planner import (
    EvidenceStrength,
    Evaluator,
    TaskFacts,
    TaskScale,
)

from _import_detectors import imported_module_tokens, imported_names

FIXED_TIME = "2026-09-17T00:00:00Z"
MODULE_SOURCE = (
    pathlib.Path(__file__).resolve().parents[2] / "src/adaptive_router/planner.py"
).read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# AST 工具（与项目其余部分同一纪律：不做子串扫描）
# --------------------------------------------------------------------------


def _filesystem_accesses(source: str) -> set[str]:
    """源码里**真实**的文件系统访问点（AST 级）。

    判据与原 ``forbidden not in MODULE_SOURCE`` 子串扫描**逐条对应**，
    且**按旧判据自己的形状**选取 AST 形状 —— 既不放宽真实代码，也不再把文档当行为：

    | 旧判据（子串） | 新判据（AST） |
    |---|---|
    | ``pathlib``（**模块名**） | 共用检测器 ``imported_module_tokens``（调用处判定） |
    | ``open(``（**调用形**） | ``Call``，其 ``func`` 是 ``Name("open")`` 或 ``Attribute(attr="open")``（覆盖 ``io.open`` / ``obj.open``） |
    | ``os.walk``（**点路径**） | **任何** ``Attribute(value=Name("os"), attr="walk")`` —— **不要求**被直接调用 |
    | ``rglob``（**裸名**） | **任何** ``Attribute(attr="rglob")``，或 ``Call(func=Name("rglob"))`` |

    ⚠️ ``os.walk`` 必须按**属性访问**记：``walker = os.walk`` 同样是真实访问点，
    只记"直接调用"会比旧规则**放松**（复审指出的偏差）。
    ⚠️ docstring / 注释里的同名字样**不算**（那是文档）；
    ``reopen(...)`` / ``rglob_count`` 这类**另有其名**的标识符也不算
    —— 那是旧子串判据的**假红**，不是真实访问点。
    """
    hits: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Attribute):
            if node.attr == "rglob":
                hits.add("rglob")
            elif (
                node.attr == "walk"
                and isinstance(node.value, ast.Name)
                and node.value.id == "os"
            ):
                hits.add("os.walk")
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id in ("open", "rglob"):
                hits.add(func.id)
            elif isinstance(func, ast.Attribute) and func.attr == "open":
                hits.add("open")
    return hits


# --------------------------------------------------------------------------
# 测试替身
# --------------------------------------------------------------------------


def snapshot(**overrides: object) -> CapabilitySnapshot:
    base: dict[str, object] = dict(
        subagents=True,
        async_background=True,
        concurrency_limit=4,
        available_models=("gpt-5-codex",),
        available_effort_levels=(),
        state_wait_append_stop=True,
        hot_switch=True,
        reliable_cost_data=False,
        probed_dimensions=("subagents",),
        unavailable_reason=None,
        probed_at=FIXED_TIME,
    )
    base.update(overrides)
    return CapabilitySnapshot(**base)  # type: ignore[arg-type]


def project(**overrides: object) -> ProjectContext:
    base: dict[str, object] = dict(
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
        read_at=FIXED_TIME,
    )
    base.update(overrides)
    return ProjectContext(**base)  # type: ignore[arg-type]


def facts(**overrides: object) -> TaskFacts:
    base: dict[str, object] = dict(
        affected_modules=1,
        mechanical=False,
        touches_cross_module_contracts=False,
        requires_iterative_debugging=False,
        is_review_task=False,
        subtasks=(),
        shared_context_size=0,
        has_sequential_dependency=False,
        branch_setup_cost=Ordinal.LOW,
        evidence=EvidenceStrength.STRONG,
        local_and_reversible=True,
    )
    base.update(overrides)
    return TaskFacts(**base)  # type: ignore[arg-type]


def evaluate(**overrides: object) -> Evaluation:
    return Evaluator().evaluate(
        facts(**overrides), capability=snapshot(), project=project()
    )


# ==========================================================================
# TEST-EVAL-001 —— 11 个维度
# ==========================================================================


class TestEvaluationIsElevenDimensions:
    def test_returns_the_frozen_contract_type(self) -> None:
        assert isinstance(evaluate(), Evaluation)

    def test_exactly_eleven_dimensions(self) -> None:
        assert len(evaluate().dimensions) == 11

    def test_dimension_names_match_protocol_order(self) -> None:
        expected = [
            "scale",
            "complexity",
            "risk",
            "impact_scope",
            "independence",
            "parallel_benefit",
            "context_volume",
            "scheduling_cost",
            "rework_probability",
            "review_value",
            "concurrency_capacity",
        ]
        assert [f.name for f in dataclasses.fields(Evaluation)][:11] == expected

    def test_no_twelfth_dimension_leaks_in(self) -> None:
        """执行方式是 [05] 的职责，不得混进 Evaluation。"""
        field_names = {f.name for f in dataclasses.fields(Evaluation)}
        assert field_names == set(
            [f.name for f in dataclasses.fields(Evaluation)][:11]
        ) | {"reason"}

    @pytest.mark.parametrize(
        "field_name",
        [
            "scale",
            "complexity",
            "risk",
            "independence",
            "parallel_benefit",
            "context_volume",
            "scheduling_cost",
            "rework_probability",
            "review_value",
            "concurrency_capacity",
        ],
    )
    def test_ordinal_dimensions_are_discrete(self, field_name: str) -> None:
        assert getattr(evaluate(), field_name) in tuple(Ordinal)

    def test_impact_scope_is_a_scope_not_an_ordinal(self) -> None:
        assert evaluate().impact_scope in tuple(ImpactScope)


# ==========================================================================
# TEST-EVAL-002 —— 低成本判断
# ==========================================================================


class TestEvaluationIsACheapAssessment:
    def test_no_float_scores_anywhere(self) -> None:
        """不得制造浮点打分器 —— 离散 11 维判断，不要 0.73 这种伪精度。"""
        evaluation = evaluate()
        for value in evaluation.dimensions:
            assert not isinstance(value, float)
            assert isinstance(value, (Ordinal, ImpactScope))

    def test_reason_is_always_present(self) -> None:
        assert evaluate().reason.strip()

    def test_reason_names_the_driving_facts(self) -> None:
        """评估摘要要能解释"为什么是这个量级"，而不是一句空话。"""
        evaluation = evaluate(affected_modules=12, touches_cross_module_contracts=True)
        text = evaluation.reason
        assert "modules=12" in text or "12" in text

    def test_scale_tracks_affected_modules(self) -> None:
        assert evaluate(affected_modules=1).scale is Ordinal.LOW
        assert evaluate(affected_modules=3).scale is Ordinal.MEDIUM
        assert evaluate(affected_modules=9).scale is Ordinal.HIGH

    def test_complexity_notices_mechanical_work(self) -> None:
        """机械明确的任务复杂度必须最低 —— L33 的档位依赖这一点。"""
        assert evaluate(mechanical=True).complexity is Ordinal.LOW

    def test_complexity_rises_for_iterative_debugging(self) -> None:
        assert evaluate(requires_iterative_debugging=True).complexity in (
            Ordinal.MEDIUM,
            Ordinal.HIGH,
        )

    def test_impact_scope_tracks_module_count(self) -> None:
        assert evaluate(affected_modules=1).impact_scope is ImpactScope.LOCAL
        assert evaluate(affected_modules=4).impact_scope is ImpactScope.CROSS_MODULE
        assert (
            evaluate(affected_modules=12).impact_scope is ImpactScope.SYSTEM_WIDE
        )

    def test_risk_rises_for_shared_or_irreversible_work(self) -> None:
        safe = evaluate(local_and_reversible=True)
        unsafe = evaluate(local_and_reversible=False)
        assert safe.risk is Ordinal.LOW
        assert unsafe.risk is not Ordinal.LOW

    def test_context_volume_tracks_shared_context_size(self) -> None:
        assert evaluate(shared_context_size=0).context_volume is Ordinal.LOW
        assert evaluate(shared_context_size=40).context_volume is Ordinal.HIGH

    def test_concurrency_capacity_comes_from_the_snapshot(self) -> None:
        """[04] 只能**读** [03] 的 Snapshot，不得自行探测。"""
        rich = Evaluator().evaluate(
            facts(), capability=snapshot(concurrency_limit=8), project=project()
        )
        poor = Evaluator().evaluate(
            facts(), capability=snapshot(concurrency_limit=1), project=project()
        )
        assert rich.concurrency_capacity is Ordinal.HIGH
        assert poor.concurrency_capacity is Ordinal.LOW

    def test_missing_subagents_shows_up_as_zero_capacity(self) -> None:
        evaluation = Evaluator().evaluate(
            facts(), capability=snapshot(subagents=False), project=project()
        )
        assert evaluation.concurrency_capacity is Ordinal.LOW

    def test_unknown_capacity_is_not_treated_as_zero(self) -> None:
        """未知 ≠ 不支持：不得把 None 当成"没有并发能力"。"""
        from adaptive_router.contracts import UnavailableReason

        evaluation = Evaluator().evaluate(
            facts(),
            capability=snapshot(
                subagents=None, unavailable_reason=UnavailableReason.TOOL_FAILURE
            ),
            project=project(),
        )
        assert evaluation.concurrency_capacity in tuple(Ordinal)

    def test_review_value_reflects_review_tasks(self) -> None:
        assert evaluate(is_review_task=True).review_value is Ordinal.HIGH
        assert evaluate(is_review_task=False).review_value is Ordinal.LOW

    def test_rework_probability_reflects_evidence_strength(self) -> None:
        assert evaluate(evidence=EvidenceStrength.STRONG).rework_probability is (
            Ordinal.LOW
        )
        assert evaluate(evidence=EvidenceStrength.WEAK).rework_probability is (
            Ordinal.HIGH
        )

    def test_scheduling_cost_reflects_branch_setup_cost(self) -> None:
        assert evaluate(branch_setup_cost=Ordinal.LOW).scheduling_cost is Ordinal.LOW
        assert evaluate(branch_setup_cost=Ordinal.HIGH).scheduling_cost is Ordinal.HIGH

    def test_is_deterministic(self) -> None:
        assert evaluate(affected_modules=5) == evaluate(affected_modules=5)

    def test_evaluation_is_frozen(self) -> None:
        evaluation = evaluate()
        with pytest.raises(dataclasses.FrozenInstanceError):
            evaluation.scale = Ordinal.HIGH  # type: ignore[misc]


# ==========================================================================
# 边界：不探测、不决定模式、不选模型
# ==========================================================================


class TestEvaluatorBoundaries:
    def test_evaluator_does_not_probe_capabilities(self) -> None:
        """[04] 不得自行探测 —— 它只接收 Snapshot。

        ⚠️ 用**共用**检测器：原先这段内联实现只看 ``ImportFrom.module``，
        ``from . import capabilities`` 这种形式会被整条跳过（假绿）。
        """
        imported = imported_names(MODULE_SOURCE)
        for forbidden in ("CapabilityProbe", "CapabilityRegistry", "scan_project_facts"):
            assert forbidden not in imported, forbidden

    def test_evaluator_returns_no_execution_mode(self) -> None:
        """执行方式是 [05] Planner 的唯一职责。"""
        assert not hasattr(evaluate(), "mode")
        assert ExecutionMode.DIRECT is not None  # 契约仍可用，但不在 Evaluation 上

    def test_evaluator_returns_no_model_choice(self) -> None:
        for forbidden in ("tier", "model_id", "effort"):
            assert not hasattr(evaluate(), forbidden)

    def test_evaluator_does_not_read_the_filesystem(self) -> None:
        """它接收 ProjectContext 事实，不自己去读项目。

        ⚠️ 用 **AST**，不做子串扫描（原实现是 ``forbidden not in MODULE_SOURCE``）：
        本模块的 docstring 会**说明**"不读文件系统"，注释里也可能出现 ``open(``，
        子串扫描会把文档当行为 —— **假红**；而真正该看的"调用点"却不看。

        判据与原实现**逐一等价**（``pathlib`` 模块 / ``open`` 调用 /
        ``os.walk`` 调用 / ``rglob`` 属性），**不**扩大为新的"文件系统策略"。
        """
        assert "pathlib" not in imported_module_tokens(MODULE_SOURCE)
        assert _filesystem_accesses(MODULE_SOURCE) == set()

    def test_the_filesystem_check_is_not_vacuous(self) -> None:
        """非平凡性：真访问点必须被看见，文档/注释里的字样必须**不**被看见。

        （对**合成源码**判定，不碰生产代码 —— 与 `tests/structure/test_detectors.py`
        同一纪律：变异证据永久留在 CI 里。）
        """
        assert _filesystem_accesses("f = open('x')\n") == {"open"}
        assert _filesystem_accesses("for root, dirs, files in os.walk('.'):\n    pass\n") == {
            "os.walk"
        }
        assert _filesystem_accesses("p.rglob('*.py')\n") == {"rglob"}
        # ⚠️ 复审指出的偏差：`os.walk` 值被取用（未直接调用）同样是真实访问点，
        # 旧子串判据会红，只记"直接调用"的新判据**不能**放过它。
        assert _filesystem_accesses("walker = os.walk\n") == {"os.walk"}
        assert _filesystem_accesses("walker = os.walk\nwalker('.')\n") == {"os.walk"}
        # 等价形状补充：`io.open` / `obj.open` 也是调用形（旧子串 `open(` 同样命中）
        assert _filesystem_accesses("with io.open('x') as f:\n    pass\n") == {"open"}
        assert _filesystem_accesses("buf = stream.open('x')\n") == {"open"}
        assert _filesystem_accesses("rglob('*.py')\n") == {"rglob"}
        # 负对照：子串扫描会假红的写法，AST 判定必须放行
        assert _filesystem_accesses('"""本模块不调用 open( ，也不做 os.walk。"""\n') == set()
        assert _filesystem_accesses("# 历史实现曾用 os.walk 扫目录\nvalue = 1\n") == set()
        assert _filesystem_accesses("opened = 1\n") == set()
        assert _filesystem_accesses("def reopen(path):\n    return path\n") == set()
        assert _filesystem_accesses("rglob_count = 1\n") == set()
        assert _filesystem_accesses("os.walk_count = 1\n") == set()
        # 模块级判据同样非平凡：真 import 必须被共用检测器看见
        assert "pathlib" in imported_module_tokens("import pathlib\n")

    def test_project_context_is_consumed_but_not_decided(self) -> None:
        """项目信息只作为事实输入（例如高风险文件影响风险维度）。"""
        risky = Evaluator().evaluate(
            facts(local_and_reversible=True),
            capability=snapshot(),
            project=project(high_risk_files=("pyproject.toml",)),
        )
        assert isinstance(risky, Evaluation)

    def test_task_facts_are_frozen(self) -> None:
        f = facts()
        with pytest.raises(dataclasses.FrozenInstanceError):
            f.affected_modules = 99  # type: ignore[misc]


class TestTaskFactsValidation:
    def test_negative_module_count_is_rejected(self) -> None:
        with pytest.raises((TypeError, ValueError)):
            facts(affected_modules=-1)

    def test_scale_enum_is_used_for_branch_setup_cost(self) -> None:
        with pytest.raises((TypeError, ValueError)):
            facts(branch_setup_cost="cheap")

    def test_task_scale_enum_exists_for_declared_size(self) -> None:
        assert {m.name for m in TaskScale} == {"SMALL", "MEDIUM", "LARGE"}
