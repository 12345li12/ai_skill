"""Step 4A.1 — 结构检测器自身的测试（合成源码，纯函数）。

架构依据：`docs/architecture.md` v1.0 FROZEN 第 7.2 节

⚠️ **本文件不修改任何真实仓库源文件。**
   "变异证据"以合成源码形式**永久留在 CI 里**，
   而不是人工改文件一次后写进 README。

覆盖第 4A.1 节要求的全部 import 形式，以及三类 synthetic mutation。
"""

from __future__ import annotations

import enum
from dataclasses import dataclass

import pytest

from _detectors import (
    defining_modules,
    forbidden_edges,
    imports_from_source,
    module_name_for,
    resolve_type_hints,
)

PKG = "adaptive_router"


# --------------------------------------------------------------------------
# 影子类型：与 canonical **同名但不同一个类**，用于验证类型解析不做字符串猜测。
# 定义在模块级，因为 ``typing.get_type_hints`` 在全局名字空间中解析注解。
# --------------------------------------------------------------------------


class _ShadowConfirmationSource(enum.Enum):
    """另一个名为 ``ConfirmationSource`` 的枚举，**不是** canonical（故意不同名变量名）。"""

    UNAVAILABLE = "unavailable"


@dataclass
class _ShadowHolder:
    source: "_ShadowConfirmationSource"


def imports(source: str, module: str, *, is_package: bool = False) -> set[str]:
    return imports_from_source(source, module_name=module, is_package=is_package)


# ==========================================================================
# 1. 模块身份解析
# ==========================================================================


class TestModuleNameFor:
    def test_plain_module(self) -> None:
        assert module_name_for("cost/ledger.py", PKG) == (f"{PKG}.cost.ledger", False)

    def test_package_init(self) -> None:
        assert module_name_for("cost/__init__.py", PKG) == (f"{PKG}.cost", True)

    def test_top_level_module(self) -> None:
        assert module_name_for("router.py", PKG) == (f"{PKG}.router", False)

    def test_top_level_package(self) -> None:
        assert module_name_for("__init__.py", PKG) == (PKG, True)

    def test_nested_package_init(self) -> None:
        assert module_name_for("cost/sub/__init__.py", PKG) == (
            f"{PKG}.cost.sub",
            True,
        )

    def test_rejects_non_python(self) -> None:
        with pytest.raises(ValueError):
            module_name_for("README.md", PKG)


# ==========================================================================
# 2. import 形式覆盖（第 4A.1 节清单）
# ==========================================================================


class TestImportFormCoverage:
    def test_plain_dotted_import(self) -> None:
        found = imports(f"import {PKG}.scheduler", f"{PKG}.cost.ledger")
        assert f"{PKG}.scheduler" in found

    def test_dotted_import_with_alias(self) -> None:
        found = imports(f"import {PKG}.scheduler as s", f"{PKG}.cost.ledger")
        assert f"{PKG}.scheduler" in found

    def test_from_absolute_import_name(self) -> None:
        """``from adaptive_router import scheduler`` —— 旧解析器漏掉的形式。"""
        found = imports(f"from {PKG} import scheduler", f"{PKG}.cost.ledger")
        assert f"{PKG}.scheduler" in found

    def test_from_absolute_import_submodule(self) -> None:
        found = imports(
            f"from {PKG}.scheduler import Scheduler", f"{PKG}.cost.ledger"
        )
        assert f"{PKG}.scheduler" in found
        assert f"{PKG}.scheduler.Scheduler" in found

    def test_from_parent_import_name(self) -> None:
        """``from .. import scheduler`` —— 旧解析器只得到 ``adaptive_router``。"""
        found = imports("from .. import scheduler", f"{PKG}.cost.ledger")
        assert f"{PKG}.scheduler" in found

    def test_from_parent_module_import_symbol(self) -> None:
        found = imports("from ..scheduler import Scheduler", f"{PKG}.cost.ledger")
        assert f"{PKG}.scheduler" in found

    def test_from_sibling_import_name(self) -> None:
        found = imports("from . import sibling", f"{PKG}.cost.ledger")
        assert f"{PKG}.cost.sibling" in found

    def test_from_sibling_module_import_symbol(self) -> None:
        found = imports("from .sibling import Thing", f"{PKG}.cost.ledger")
        assert f"{PKG}.cost.sibling" in found
        assert f"{PKG}.cost.sibling.Thing" in found

    def test_package_init_relative_import_is_not_over_popped(self) -> None:
        """包 ``__init__.py`` 里的 ``from .estimator import X``。

        旧解析器按普通模块公式计算，会向上多退一级，
        得到 ``adaptive_router.estimator`` 而不是 ``adaptive_router.cost.estimator``。
        """
        found = imports(
            "from .estimator import CostEstimator",
            f"{PKG}.cost",
            is_package=True,
        )
        assert f"{PKG}.cost.estimator" in found
        assert f"{PKG}.estimator" not in found, "被多退了一级"

    def test_package_init_from_dot_import_name(self) -> None:
        found = imports(
            "from . import summary", f"{PKG}.cost", is_package=True
        )
        assert f"{PKG}.cost.summary" in found

    def test_package_init_double_dot(self) -> None:
        found = imports(
            "from .. import risk", f"{PKG}.cost", is_package=True
        )
        assert f"{PKG}.risk" in found

    def test_two_level_up_from_module(self) -> None:
        found = imports("from ... import thing", f"{PKG}.a.b.c")
        assert found == {f"{PKG}.thing", f"{PKG}.thing.thing"} or f"{PKG}.thing" in found


# ==========================================================================
# 3. synthetic mutation —— 三类违规必须被识别
# ==========================================================================


class TestSyntheticMutations:
    """M1/M2/M3 的**永久化**版本。不触碰真实仓库文件。"""

    def test_synthetic_m1_ledger_imports_scheduler(self) -> None:
        """synthetic M1：``from .. import scheduler`` 必须识别为目标模块。"""
        source = "from __future__ import annotations\nfrom .. import scheduler\n"
        found = imports(source, f"{PKG}.cost.ledger")
        hits = forbidden_edges(found, [f"{PKG}.scheduler"])
        assert hits == {f"{PKG}.scheduler"}

    def test_synthetic_m2_estimator_imports_model_router(self) -> None:
        """synthetic M2：``from adaptive_router import model_router`` 必须识别。"""
        source = f"from {PKG} import model_router\n"
        found = imports(source, f"{PKG}.cost.estimator")
        hits = forbidden_edges(found, [f"{PKG}.model_router"])
        assert hits == {f"{PKG}.model_router"}

    def test_synthetic_m2_variant_plain_import(self) -> None:
        source = f"import {PKG}.model_router\n"
        found = imports(source, f"{PKG}.cost.estimator")
        assert forbidden_edges(found, [f"{PKG}.model_router"]) == {f"{PKG}.model_router"}

    def test_synthetic_m2_variant_from_submodule(self) -> None:
        source = f"from {PKG}.model_router import decide\n"
        found = imports(source, f"{PKG}.cost.estimator")
        assert forbidden_edges(found, [f"{PKG}.model_router"])

    def test_synthetic_m3_duplicate_class_definition(self) -> None:
        """synthetic M3：复制一份 ``class ConfirmationSource`` 必须被报告。"""
        sources = {
            f"{PKG}.contracts.common": "class ConfirmationSource:\n    pass\n",
            f"{PKG}.contracts.cost": "class ConfirmationSource:\n    pass\n",
        }
        assert defining_modules(sources, "ConfirmationSource") == [
            f"{PKG}.contracts.common",
            f"{PKG}.contracts.cost",
        ]

    def test_synthetic_m3_import_only_is_not_a_definition(self) -> None:
        sources = {
            f"{PKG}.contracts.common": "class ConfirmationSource:\n    pass\n",
            f"{PKG}.contracts.cost": "from .common import ConfirmationSource\n",
        }
        assert defining_modules(sources, "ConfirmationSource") == [
            f"{PKG}.contracts.common"
        ]

    def test_synthetic_m3_nested_class_is_not_top_level(self) -> None:
        """嵌套定义的同类名不算重复顶层定义。"""
        sources = {
            f"{PKG}.contracts.common": "class ConfirmationSource:\n    pass\n",
            f"{PKG}.other": "class Outer:\n    class ConfirmationSource:\n        pass\n",
        }
        assert defining_modules(sources, "ConfirmationSource") == [
            f"{PKG}.contracts.common"
        ]


# ==========================================================================
# 4. 反向：不得误报
# ==========================================================================


class TestNoFalsePositives:
    def test_docstring_mention_is_not_an_import(self) -> None:
        """synthetic negative：docstring 里写模块名不算 import。"""
        source = f'"""说明：本模块不得依赖 {PKG}.model_router。"""\n'
        found = imports(source, f"{PKG}.cost.estimator")
        assert forbidden_edges(found, [f"{PKG}.model_router"]) == set()

    def test_comment_mention_is_not_an_import(self) -> None:
        source = f"# 禁止：from .. import model_router\nx = 1\n"
        found = imports(source, f"{PKG}.cost.estimator")
        assert found == set()

    def test_string_literal_mention_is_not_an_import(self) -> None:
        source = f'NAME = "{PKG}.model_router"\n'
        found = imports(source, f"{PKG}.cost.estimator")
        assert forbidden_edges(found, [f"{PKG}.model_router"]) == set()

    def test_class_named_like_a_module_is_not_an_import(self) -> None:
        """``from contracts import model_router`` 里的 ``model_router`` 是**名字**。

        保守策略会额外产生 ``...contracts.model_router``，
        它**不匹配**禁止前缀 ``adaptive_router.model_router``。
        """
        source = f"from {PKG}.contracts import model_router\n"
        found = imports(source, f"{PKG}.cost.estimator")
        assert forbidden_edges(found, [f"{PKG}.model_router"]) == set()

    def test_forbidden_prefix_does_not_match_a_lookalike(self) -> None:
        found = {f"{PKG}.model_router_helpers", f"{PKG}.model_routerX"}
        assert forbidden_edges(found, [f"{PKG}.model_router"]) == set()

    def test_forbidden_prefix_matches_submodules(self) -> None:
        found = {f"{PKG}.model_router.decide"}
        assert forbidden_edges(found, [f"{PKG}.model_router"]) == {
            f"{PKG}.model_router.decide"
        }


# ==========================================================================
# 5. 禁止边判定不依赖目标模块是否存在
# ==========================================================================


class TestForbiddenEdgeIsExistenceIndependent:
    def test_hit_even_when_target_module_absent(self) -> None:
        """核心修正：禁止边判定**不得**先查"仓库里有没有这个模块"。

        否则尚未实现的禁止模块会天然得到空 target 集合，
        源码里写 ``from .. import scheduler`` 也检不出来。
        """
        found = imports("from .. import scheduler", f"{PKG}.cost.ledger")
        # scheduler.py 在仓库中并不存在，但判定依然命中
        assert forbidden_edges(found, [f"{PKG}.scheduler"]) == {f"{PKG}.scheduler"}

    def test_hit_for_never_existing_module(self) -> None:
        found = imports("from .. import nonexistent_control", f"{PKG}.cost.ledger")
        assert forbidden_edges(found, [f"{PKG}.nonexistent_control"])


# ==========================================================================
# 6. 真实类型解析（不做字符串猜测）
# ==========================================================================


class TestResolveTypeHints:
    def test_resolves_real_type_objects(self) -> None:
        """``typing.get_type_hints`` 返回真实对象，可用于身份比较。"""
        from adaptive_router.contracts import common as common_mod
        from adaptive_router.contracts import cost as cost_mod

        ev_hints = resolve_type_hints(common_mod.ExecutionValue)
        for field_name in ("planned_source", "requested_source", "actual_source"):
            assert ev_hints[field_name] is common_mod.ConfirmationSource, field_name

        cost_hints = resolve_type_hints(cost_mod.CostActual)
        assert cost_hints["source"] is common_mod.ConfirmationSource

    def test_distinguishes_a_lookalike_class(self) -> None:
        """同类名的**另一个类**不会被误判为 canonical。

        这是旧 ``_sources_of()`` 的真实缺陷：它只看注解字符串里
        是否含 ``ConfirmationSource``，然后把**正确答案**塞进结果，
        因此 ``source: <别的>.ConfirmationSource`` 也会"通过"。

        真实类型解析必须把它解析成那个**别的**类。
        """
        from adaptive_router.contracts import common as common_mod

        hints = resolve_type_hints(_ShadowHolder)
        assert hints["source"] is _ShadowConfirmationSource
        assert hints["source"] is not common_mod.ConfirmationSource

    def test_string_matching_would_have_been_fooled(self) -> None:
        """对照：旧做法的判据（注解串含 "ConfirmationSource"）确实会被骗。"""
        import dataclasses

        annotation = str(
            dataclasses.fields(_ShadowHolder)[0].type
        )
        # 旧判据（子串匹配）会说"这是 ConfirmationSource"
        assert "ConfirmationSource" in annotation
        # 但真实类型不是 canonical
        from adaptive_router.contracts import common as common_mod

        assert resolve_type_hints(_ShadowHolder)["source"] is not (
            common_mod.ConfirmationSource
        )

    def test_branch_return_model_origin_is_execution_value(self) -> None:
        """``BranchReturn`` 只需证明 ``model`` / ``effort`` 的 origin 是公共 ``ExecutionValue``。

        不假装它自身直接声明了 ``ConfirmationSource``。
        """
        import typing

        from adaptive_router.contracts import common as common_mod
        from adaptive_router.contracts import runtime as runtime_mod

        hints = resolve_type_hints(runtime_mod.BranchReturn)
        for field_name in ("model", "effort"):
            origin = typing.get_origin(hints[field_name])
            assert origin is common_mod.ExecutionValue, field_name
