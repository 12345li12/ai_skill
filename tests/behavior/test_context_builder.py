"""``[08] Context Builder`` —— 行为锁 + 结构锁（Step 5B.6.1 实现）。

架构依据：`docs/architecture.md` 2 节 ``[08] Context Builder``（**Amendment A10**，FROZEN）。
范围与锁清单：`tests/behavior/README_step5b.md` §26（L1–L10 / B1–B9）。

**三类证据：**

| 类 | 位置 | 内容 |
|---|---|---|
| **行为锁 B1–B9** | `TestBundleCardinality` / `TestExactKeyset` / `TestRequiredInputs` / `TestFixedFields` / `TestNoCrossChildBleed` / `TestSignature` | 真跑 builder，含负例 |
| **结构锁 L1–L10** | `TestStructuralLocks` | 对**真实**生产源码做 AST 判定 |
| **敏感性 probe** | `TestLockSensitivity` | 合成变异源码 ⇒ 每条锁必须**因目标语义变异而红** |

⚠️ 敏感性 probe 一律在**源码字符串**上完成，**不覆写** `src/` 里的真实文件
（§26.5 的实施注意事项）。
"""

from __future__ import annotations

import inspect
import pathlib

import pytest

from adaptive_router.context_builder import ContextBuilder, ContextBuilderError
from adaptive_router.contracts import (
    BRANCH_RETURN_FIELDS,
    BranchSpec,
    ContextBundle,
    ExecutionMode,
    Plan,
)
from adaptive_router.execution_context_facts import ExecutionContextFacts

from _context_locks import (
    attribute_reads,
    calls_to,
    construction_keyword_shapes,
    context_bundle_construction_sites,
    exact_keys_comparisons,
    forbidden_import_hits,
    helper_call_count,
    keyword_only_defaults,
    mapping_get_calls,
    open_calls,
    output_format_expression,
    resolved_symbols,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
MODULE_PATH = REPO_ROOT / "src/adaptive_router/context_builder.py"
MODULE_SOURCE = MODULE_PATH.read_text(encoding="utf-8")
SEAM_PATH = REPO_ROOT / "src/adaptive_router/execution_context_facts.py"

#: L1 的**属性读取 allowlist**（README §26.4）：多一个都会红。
ALLOWED_ATTRIBUTE_READS = frozenset(
    {
        "branches",
        "branch_id",
        "goal",
        "completion_criteria",
        "background",
        "constraints",
        "allowed_operations",
        "forbidden_operations",
        "join",
    }
)

#: L8 的 import 禁用清单（README §26.4）。
FORBIDDEN_IMPORTS = (
    "scheduler",
    "agent_manager",
    "adapters",
    "results",
    "validation",
    "recovery",
    "session",
    "entrypoint",
    "project_context",
    "pathlib",
    "os",
    "subprocess",
)

#: L7：`Plan` **允许**作为输入类型 import，但**禁止构造**它。
CONSTRUCT_ONLY_FORBIDDEN = ("Plan",)
#: L7：这五个符号**既不许 import、也不许构造**（第二决策者）。
NEVER_SYMBOLS = (
    "ModelSelection",
    "BranchState",
    "Planner",
    "ModelRouter",
    "AgentManager",
)


# --------------------------------------------------------------------------
# 测试替身
# --------------------------------------------------------------------------


def branch(branch_id: str, *, goal: str | None = None) -> BranchSpec:
    return BranchSpec(
        branch_id=branch_id,
        goal=goal or f"goal {branch_id}",
        read_only=True,
        completion_criteria=f"done {branch_id}",
    )


def plan(mode: ExecutionMode, *branches: BranchSpec) -> Plan:
    return Plan(
        mode=mode,
        main_thread_duty="execute on the main thread",
        reason="test",
        branches=branches,
    )


def facts(
    *,
    background: tuple[str, ...] = ("bg",),
    constraints: tuple[str, ...] = ("no network",),
    allowed_operations: tuple[str, ...] = ("read",),
    forbidden_operations: tuple[str, ...] = ("write",),
) -> ExecutionContextFacts:
    return ExecutionContextFacts(
        background=background,
        constraints=constraints,
        allowed_operations=allowed_operations,
        forbidden_operations=forbidden_operations,
    )


def multi_plan(*branch_ids: str) -> Plan:
    return plan(ExecutionMode.MULTI_AGENT, *(branch(bid) for bid in branch_ids))


def build(plan_: Plan, per_child_facts: dict, conclusions: dict):
    return ContextBuilder().build(
        plan=plan_,
        facts_by_branch=per_child_facts,
        confirmed_conclusions_by_branch=conclusions,
    )


# ==========================================================================
# B1 / B2 —— 接收者与基数
# ==========================================================================


class TestBundleCardinality:
    def test_multi_agent_covers_exactly_the_plan_children(self) -> None:
        """B1：键集**精确覆盖** `Plan.branches`（缺一个、多一个都失败）。

        ⚠️ 本测试**不**断言 `Mapping` 顺序 —— A10 只冻结"键集精确覆盖 +
        `branch_id → bundle` 显式关联"，**未**把顺序定义为业务语义。
        """
        plan_ = multi_plan("br_a", "br_b")
        bundles = build(
            plan_,
            {"br_a": facts(background=("a",)), "br_b": facts(background=("b",))},
            {"br_a": ("ca",), "br_b": ("cb",)},
        )
        assert set(bundles) == {"br_a", "br_b"}
        assert all(isinstance(b, ContextBundle) for b in bundles.values())

    def test_direct_produces_an_empty_mapping(self) -> None:
        """B2：`DIRECT`（0 child）⇒ `{}` —— 这是**正常结果**，不是"漏做"。"""
        plan_ = plan(ExecutionMode.DIRECT)
        assert build(plan_, {}, {}) == {}

    def test_staged_single_agent_produces_an_empty_mapping(self) -> None:
        """B2：`STAGED_SINGLE_AGENT`（0 child）⇒ `{}`。"""
        plan_ = plan(ExecutionMode.STAGED_SINGLE_AGENT)
        assert build(plan_, {}, {}) == {}

    def test_direct_with_branches_is_unconstructible(self) -> None:
        """B2 **旁证**：契约本身不允许"DIRECT 带分支" ⇒ `[08]` 无需读 `mode` 也能得到 `{}`。"""
        with pytest.raises(Exception):
            plan(ExecutionMode.DIRECT, branch("br_x"))


# ==========================================================================
# B3 / B4 —— 精确 keyset（subset 也失败）
# ==========================================================================


class TestExactKeyset:
    def test_missing_facts_key_fails(self) -> None:
        """B3：facts 少一个 child ⇒ 显式失败（不得 fail-open）。"""
        plan_ = multi_plan("br_a", "br_b")
        with pytest.raises(ContextBuilderError, match="精确覆盖"):
            build(plan_, {"br_a": facts()}, {"br_a": (), "br_b": ()})

    def test_extra_facts_key_fails(self) -> None:
        """B3：facts 多一个不属于本计划的 child ⇒ 显式失败（superset 也不许）。"""
        plan_ = multi_plan("br_a", "br_b")
        with pytest.raises(ContextBuilderError, match="精确覆盖"):
            build(
                plan_,
                {"br_a": facts(), "br_b": facts(), "br_ghost": facts()},
                {"br_a": (), "br_b": ()},
            )

    def test_missing_conclusions_key_fails(self) -> None:
        """B4：conclusions 少一个 child ⇒ 显式失败。"""
        plan_ = multi_plan("br_a", "br_b")
        with pytest.raises(ContextBuilderError, match="精确覆盖"):
            build(plan_, {"br_a": facts(), "br_b": facts()}, {"br_a": ()})

    def test_extra_conclusions_key_fails(self) -> None:
        """B4：conclusions 多一个 ⇒ 显式失败。"""
        plan_ = multi_plan("br_a", "br_b")
        with pytest.raises(ContextBuilderError, match="精确覆盖"):
            build(
                plan_,
                {"br_a": facts(), "br_b": facts()},
                {"br_a": (), "br_b": (), "br_ghost": ()},
            )

    def test_non_empty_mappings_with_a_direct_plan_fail(self) -> None:
        """B2/B3 交叉：0 child 时**两张映射都必须为空**（多余即失败）。"""
        plan_ = plan(ExecutionMode.DIRECT)
        with pytest.raises(ContextBuilderError, match="精确覆盖"):
            build(plan_, {"br_a": facts()}, {})


# ==========================================================================
# B5 —— 两个映射必填（无默认值）
# ==========================================================================


class TestRequiredInputs:
    def test_conclusions_mapping_is_required(self) -> None:
        """B5：不传 conclusions ⇒ `TypeError`（不得有默认 `{}`）。"""
        with pytest.raises(TypeError):
            ContextBuilder().build(plan=multi_plan("br_a", "br_b"), facts_by_branch={})

    def test_facts_mapping_is_required(self) -> None:
        """B5：不传 facts ⇒ `TypeError`。"""
        with pytest.raises(TypeError):
            ContextBuilder().build(
                plan=multi_plan("br_a", "br_b"),
                confirmed_conclusions_by_branch={},
            )


# ==========================================================================
# B6 / B7 —— 固定字段
# ==========================================================================


class TestFixedFields:
    def test_excluded_and_history_are_fixed_in_v1(self) -> None:
        """B6：V1 无 candidate facts / 无 history seam ⇒ `excluded == ()`、`includes_history is False`。"""
        bundles = build(
            multi_plan("br_a", "br_b"),
            {"br_a": facts(), "br_b": facts()},
            {"br_a": (), "br_b": ()},
        )
        for bundle in bundles.values():
            assert bundle.excluded == ()
            assert bundle.includes_history is False

    def test_output_format_is_exactly_the_contract_join(self) -> None:
        """B7：`output_format` **精确等于** `" / ".join(BRANCH_RETURN_FIELDS)`。

        期望值由**契约常量**算出，测试侧**不手抄**十项。
        """
        bundles = build(
            multi_plan("br_a", "br_b"),
            {"br_a": facts(), "br_b": facts()},
            {"br_a": (), "br_b": ()},
        )
        expected = " / ".join(BRANCH_RETURN_FIELDS)
        for bundle in bundles.values():
            assert bundle.output_format == expected
        assert expected.count(" / ") == len(BRANCH_RETURN_FIELDS) - 1

    def test_per_child_fields_come_from_their_own_branch_spec(self) -> None:
        plan_ = plan(
            ExecutionMode.MULTI_AGENT,
            branch("br_a", goal="goal A"),
            branch("br_b", goal="goal B"),
        )
        bundles = build(
            plan_,
            {"br_a": facts(), "br_b": facts()},
            {"br_a": (), "br_b": ()},
        )
        assert bundles["br_a"].goal == "goal A"
        assert bundles["br_b"].goal == "goal B"
        assert bundles["br_a"].completion_criteria == "done br_a"


# ==========================================================================
# B8 —— 不串味（防"复制给所有 child" / 全局 fallback）
# ==========================================================================


class TestNoCrossChildBleed:
    def test_each_child_gets_its_own_facts(self) -> None:
        """B8：两个 child 给不同 facts ⇒ 各自 bundle 等于各自输入。"""
        bundles = build(
            multi_plan("br_a", "br_b"),
            {
                "br_a": facts(background=("A-bg",), constraints=("A-c",)),
                "br_b": facts(background=("B-bg",), constraints=("B-c",)),
            },
            {"br_a": ("A-conclusion",), "br_b": ("B-conclusion",)},
        )
        assert bundles["br_a"].background == ("A-bg",)
        assert bundles["br_a"].constraints == ("A-c",)
        assert bundles["br_b"].background == ("B-bg",)
        assert bundles["br_b"].constraints == ("B-c",)
        assert bundles["br_a"].confirmed_conclusions == ("A-conclusion",)
        assert bundles["br_b"].confirmed_conclusions == ("B-conclusion",)


# ==========================================================================
# B9 —— 签名不含未声明输入（N1 的钉子）
# ==========================================================================


class TestSignature:
    def test_signature_has_no_undeclared_inputs(self) -> None:
        """B9：参数名**恰好**三个 —— `RoutingPlan` / `ProjectContext` / `AuthorizationContext`
        都不在（deliberate non-read，A10 + N1）。

        ⚠️ 用**绑定方法**取签名（`ContextBuilder().build`），因此不必处理 `self`。
        """
        parameters = set(inspect.signature(ContextBuilder().build).parameters)
        assert parameters == {
            "plan",
            "facts_by_branch",
            "confirmed_conclusions_by_branch",
        }


# ==========================================================================
# seam DTO（A10.3 的四项事实）
# ==========================================================================


class TestExecutionContextFactsSeam:
    def test_it_is_not_a_frozen_contract(self) -> None:
        """A10.8：实现级 DTO，**不在** `contracts/` 里（`contracts/` 逐字节冻结）。"""
        assert SEAM_PATH.exists()
        assert MODULE_PATH.parent == SEAM_PATH.parent
        assert "contracts" not in SEAM_PATH.parts

    def test_rejects_non_string_tuple(self) -> None:
        with pytest.raises(TypeError):
            ExecutionContextFacts(
                background=["bg"],  # type: ignore[arg-type]
                constraints=(),
                allowed_operations=("read",),
                forbidden_operations=(),
            )

    def test_rejects_an_operation_that_is_both_allowed_and_forbidden(self) -> None:
        with pytest.raises(ValueError, match="既允许又禁止"):
            facts(allowed_operations=("read", "write"), forbidden_operations=("write",))


# ==========================================================================
# 结构锁 L1–L10（对**真实**生产源码断言）
# ==========================================================================


class TestStructuralLocks:
    def test_l1_attribute_reads_stay_within_the_allowlist(self) -> None:
        """L1：属性读取白名单 —— 只读必要字段；不读 `read_only` / `mode` / `project.*`。"""
        reads = attribute_reads(MODULE_SOURCE)
        assert reads <= ALLOWED_ATTRIBUTE_READS, sorted(reads - ALLOWED_ATTRIBUTE_READS)
        # `join` 不是死条目：`output_format` 真的用了它（否则上面那条 allowlist 会名不副实）
        assert "join" in reads

    def test_l2_mapping_parameters_are_keyword_only_without_defaults(self) -> None:
        """L2：两个映射参数 keyword-only 且**无默认值**（也不许多出别的 kw-only 参数）。"""
        assert keyword_only_defaults(MODULE_SOURCE, "ContextBuilder", "build") == {
            "plan": False,
            "facts_by_branch": False,
            "confirmed_conclusions_by_branch": False,
        }

    def test_l3_exact_keyset_check_is_bound_to_the_real_comparison(self) -> None:
        """L3：唯一 chokepoint `_require_exact_keys` 内**唯一**比较是 `set(...) == set(...)`，
        且它被**恰好调用两次**（两张映射各一次）。"""
        assert exact_keys_comparisons(MODULE_SOURCE) == [(["Eq"], True)]
        assert helper_call_count(MODULE_SOURCE) == 2

    def test_l4_context_bundle_has_exactly_one_construction_site_in_src(self) -> None:
        """L4：全 `src/` 只有 `[08]` 构造 `ContextBundle`（alias-aware 解析）。"""
        sources = {
            path.relative_to(REPO_ROOT).as_posix(): path.read_text(encoding="utf-8")
            for path in sorted((REPO_ROOT / "src").rglob("*.py"))
        }
        assert sources, "src/ 下应当有 Python 文件"
        sites = context_bundle_construction_sites(sources)
        assert set(sites) == {"src/adaptive_router/context_builder.py"}, sites
        assert sum(len(lines) for lines in sites.values()) == 1

    def test_l5_output_format_is_the_contract_join_expression(self) -> None:
        """L5：`output_format=` 的值**就是这个表达式**（不是手抄字面量）。"""
        assert (
            output_format_expression(MODULE_SOURCE)
            == "' / '.join(BRANCH_RETURN_FIELDS)"
        )

    def test_l6_excluded_and_history_are_literals(self) -> None:
        """L6：`excluded=()` 与 `includes_history=False` 是**字面固定**。"""
        shapes = construction_keyword_shapes(MODULE_SOURCE)
        assert shapes["excluded"] == "()"
        assert shapes["includes_history"] == "False"

    def test_l7_no_second_decision_maker(self) -> None:
        """L7：允许 import `Plan` 作**输入类型**；**禁止构造** `Plan`；五个第二决策者符号
        既不许 import 也不许构造。

        ⚠️ 判据必须 **alias-aware**：`imported_names()` 返回的是**本地绑定名**
        （`from .contracts import Plan as P` 只给出 `P`），只用它会**假绿**。
        """
        assert calls_to(MODULE_SOURCE, CONSTRUCT_ONLY_FORBIDDEN) == []
        assert resolved_symbols(MODULE_SOURCE) & set(NEVER_SYMBOLS) == set()
        assert calls_to(MODULE_SOURCE, NEVER_SYMBOLS) == []
        # `Plan` 只作类型依赖：确实 import 了，但确实没构造
        assert "Plan" in resolved_symbols(MODULE_SOURCE)

    def test_l8_forbidden_imports_are_absent(self) -> None:
        """L8：不读文件系统 / 不重新加载项目 / 不碰 session·bypass / 不碰下游节点。"""
        assert forbidden_import_hits(MODULE_SOURCE, FORBIDDEN_IMPORTS) == set()

    def test_l9_mapping_get_is_forbidden(self) -> None:
        """L9：**禁止 `Mapping.get`**（带不带默认值都不许）⇒ 必须直接索引。"""
        assert mapping_get_calls(MODULE_SOURCE) == []

    def test_l10_no_open_call(self) -> None:
        """L10：不调用 `open`。"""
        assert open_calls(MODULE_SOURCE) == []


# ==========================================================================
# 敏感性 probe：每条锁都必须能"因目标语义变异而红"
# ==========================================================================

MUT_READ_ONLY = MODULE_SOURCE + "\n_ = spec.read_only\n"
MUT_MODE = MODULE_SOURCE + "\n_ = plan.mode\n"
MUT_PROJECT = MODULE_SOURCE + "\n_ = project.denied\n"
MUT_L2_DEFAULT = MODULE_SOURCE.replace(
    "confirmed_conclusions_by_branch: Mapping[BranchId, tuple[str, ...]],",
    "confirmed_conclusions_by_branch: Mapping[BranchId, tuple[str, ...]] = {},",
)
MUT_L3_SUBSET = MODULE_SOURCE.replace(
    "if set(actual) == set(expected):", "if set(actual) <= set(expected):"
)
MUT_L3_ONE_CALL = MODULE_SOURCE.replace(
    '_require_exact_keys(facts_by_branch, expected, label="facts_by_branch")',
    "pass",
)
MUT_L5_LITERAL = MODULE_SOURCE.replace(
    'output_format=" / ".join(BRANCH_RETURN_FIELDS),',
    'output_format="branch_id / agent_id / attempt",',
)
MUT_L5_SEPARATOR = MODULE_SOURCE.replace(
    '" / ".join(BRANCH_RETURN_FIELDS)', '", ".join(BRANCH_RETURN_FIELDS)'
)
MUT_L6_EXCLUDED = MODULE_SOURCE.replace("excluded=(),", "excluded=tuple(extra),")
MUT_L6_HISTORY = MODULE_SOURCE.replace(
    "includes_history=False,", "includes_history=flag,"
)
MUT_L7_CONSTRUCT = MODULE_SOURCE + '\n_ = Plan(mode=None, main_thread_duty="x", reason="y")\n'
MUT_L7_IMPORT = MODULE_SOURCE + "\nfrom .model_router import ModelRouter\n"
MUT_L7_ALIAS_PLAN = MODULE_SOURCE + "\nfrom .contracts import Plan as P\n_ = P(mode=None)\n"
MUT_L7_ALIAS_SELECTION = MODULE_SOURCE + "\nfrom .contracts import ModelSelection as MS\n"
MUT_L7_ALIAS_ROUTER = (
    MODULE_SOURCE + "\nfrom .model_router import ModelRouter as MR\n_ = MR()\n"
)
MUT_L8_SCHEDULER = MODULE_SOURCE + "\nfrom . import scheduler\n"
MUT_L9_GET = MODULE_SOURCE + '\n_ = facts_by_branch.get("br_x")\n'
MUT_L9_GET_DEFAULT = MODULE_SOURCE + '\n_ = facts_by_branch.get("br_x", {})\n'
MUT_L10_OPEN = MODULE_SOURCE + '\n_ = open("x")\n'


class TestLockSensitivity:
    """每条结构锁的**非平凡性**证据：变异 ⇒ 红（全部在源码字符串上做，不碰真实文件）。"""

    def test_l1_probes_go_red(self) -> None:
        assert attribute_reads(MUT_READ_ONLY) - ALLOWED_ATTRIBUTE_READS == {"read_only"}
        assert attribute_reads(MUT_MODE) - ALLOWED_ATTRIBUTE_READS == {"mode"}
        assert attribute_reads(MUT_PROJECT) - ALLOWED_ATTRIBUTE_READS == {"denied"}

    def test_l2_probe_goes_red(self) -> None:
        assert MUT_L2_DEFAULT != MODULE_SOURCE
        mutated = keyword_only_defaults(MUT_L2_DEFAULT, "ContextBuilder", "build")
        assert mutated["confirmed_conclusions_by_branch"] is True

    def test_l3_probes_go_red(self) -> None:
        """subset 化 / 少一次调用都必须被 L3 的判据抓住。"""
        assert MUT_L3_SUBSET != MODULE_SOURCE
        assert exact_keys_comparisons(MUT_L3_SUBSET) == [(["LtE"], True)]
        assert MUT_L3_ONE_CALL != MODULE_SOURCE
        assert helper_call_count(MUT_L3_ONE_CALL) == 1

    def test_l4_probes_catch_every_alias_form(self) -> None:
        """P1–P5 必须被捕获；N1 / N2 必须**不**被误捕（README §26.4 的 L4 规格）。"""
        probes = {
            "p1": (
                "from .contracts import ContextBundle\nx = ContextBundle()\n",
                2,
            ),
            "p2": (
                "from .contracts import ContextBundle as Bundle\nx = Bundle()\n",
                2,
            ),
            "p3": (
                "from . import contracts\nx = contracts.ContextBundle()\n",
                2,
            ),
            "p4": (
                "import adaptive_router.contracts as c\nx = c.ContextBundle()\n",
                2,
            ),
            "p5": (
                "from .contracts import ContextBundle\n"
                "Bundle = ContextBundle\n"
                "x = Bundle()\n",
                3,
            ),
            "p6": (
                "from adaptive_router import contracts as c\nx = c.ContextBundle()\n",
                2,
            ),
        }
        for name, (source, line) in probes.items():
            sites = context_bundle_construction_sites({f"{name}.py": source})
            assert sites == {f"{name}.py": [line]}, (name, sites)

        negatives = {
            "n1": "from .contracts import ModelSelection\nx = ModelSelection()\n",
            "n2": (
                "from .contracts import ContextBundle\n"
                "def f(x: ContextBundle) -> ContextBundle:\n"
                '    """ContextBundle 只在注解与 docstring 里出现。"""\n'
                "    return x\n"
            ),
            # ⚠️ 模块**身份**负例：只有确认绑定到 contracts 模块的名字才可解析，
            # 否则"某个导入对象恰好有个叫 ContextBundle 的属性"会被**误报**。
            "n3": "import json as c\nx = c.ContextBundle()\n",
            "n4": "from .contracts import ModelSelection as c\nx = c.ContextBundle()\n",
            "n5": "import some_other_module as c\nx = c.ContextBundle()\n",
        }
        for name, source in negatives.items():
            assert context_bundle_construction_sites({f"{name}.py": source}) == {}, name

    def test_l4_counts_a_second_site_as_a_violation(self) -> None:
        """L4 的核心语义：**第二个**构造点必须被发现（否则它证明不了"唯一"）。"""
        sources = {
            "src/adaptive_router/context_builder.py": MODULE_SOURCE,
            "src/adaptive_router/other.py": (
                "from .contracts import ContextBundle\nx = ContextBundle()\n"
            ),
        }
        sites = context_bundle_construction_sites(sources)
        assert set(sites) == {
            "src/adaptive_router/context_builder.py",
            "src/adaptive_router/other.py",
        }
        assert sum(len(lines) for lines in sites.values()) == 2

    def test_l5_probes_go_red(self) -> None:
        assert MUT_L5_LITERAL != MODULE_SOURCE
        assert output_format_expression(MUT_L5_LITERAL) != (
            "' / '.join(BRANCH_RETURN_FIELDS)"
        )
        assert MUT_L5_SEPARATOR != MODULE_SOURCE
        assert output_format_expression(MUT_L5_SEPARATOR) == (
            "', '.join(BRANCH_RETURN_FIELDS)"
        )

    def test_l6_probes_go_red(self) -> None:
        assert construction_keyword_shapes(MUT_L6_EXCLUDED)["excluded"] != "()"
        assert (
            construction_keyword_shapes(MUT_L6_HISTORY)["includes_history"] != "False"
        )

    def test_l7_probes_go_red(self) -> None:
        """直呼 + **三种 alias 绕法**都必须被抓（复审点名的假绿路径）。"""
        assert calls_to(MUT_L7_CONSTRUCT, CONSTRUCT_ONLY_FORBIDDEN) != []
        # `from .contracts import Plan as P` + `P(...)` ⇒ 必须解析回 Plan
        assert calls_to(MUT_L7_ALIAS_PLAN, CONSTRUCT_ONLY_FORBIDDEN) != []
        # `from .contracts import ModelSelection as MS`（仅 import 也要红）
        assert resolved_symbols(MUT_L7_ALIAS_SELECTION) & set(NEVER_SYMBOLS) == {
            "ModelSelection"
        }
        # `from .model_router import ModelRouter as MR` + `MR()`
        assert calls_to(MUT_L7_ALIAS_ROUTER, NEVER_SYMBOLS) != []
        assert resolved_symbols(MUT_L7_IMPORT) & set(NEVER_SYMBOLS) == {"ModelRouter"}
        # 负对照：`Plan` 作为类型 import 本身**不**违规
        assert resolved_symbols(MODULE_SOURCE) & set(NEVER_SYMBOLS) == set()
        assert calls_to(MODULE_SOURCE, CONSTRUCT_ONLY_FORBIDDEN) == []

    def test_l8_probe_goes_red(self) -> None:
        assert forbidden_import_hits(MUT_L8_SCHEDULER, FORBIDDEN_IMPORTS) == {
            "scheduler"
        }

    def test_l9_probes_go_red(self) -> None:
        assert mapping_get_calls(MUT_L9_GET) != []
        assert mapping_get_calls(MUT_L9_GET_DEFAULT) != []

    def test_l10_probe_goes_red(self) -> None:
        assert open_calls(MUT_L10_OPEN) != []
