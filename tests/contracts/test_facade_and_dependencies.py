"""契约包门面与依赖方向的契约测试。

覆盖两件事：
  1. ``adaptive_router.models`` 只做 re-export（不含逻辑、不改语义）
  2. 契约包内部依赖方向符合架构 11.2（common 是唯一基座）

架构 11.2 冻结的依赖方向：

        common
       ↑   ↑   ↑
       │   │   │
 decision  runtime  cost
       ↑   ↑   ↑
       └───┴───┴──  models (facade)

这同时是 INV-S4 / INV-S5 的静态前提：只要 ``common`` 是唯一被依赖基座，
``ConfirmationSource`` 就不可能被各模块各写一份。
"""

from __future__ import annotations

import ast
import pathlib

import pytest

import adaptive_router.contracts as contracts
import adaptive_router.models as models

SRC = pathlib.Path(__file__).resolve().parents[2] / "src" / "adaptive_router"
CONTRACTS = SRC / "contracts"

CONTRACT_MODULES = ("common", "decision", "runtime", "cost")


def _imports_of(path: pathlib.Path) -> set[str]:
    """返回该文件顶层 import 的模块名字集合（不做重命名解析）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level and node.module:
                found.add(node.module)  # 相对导入：模块名（不含包前缀）
            elif node.module:
                found.add(node.module)
    return found


# --------------------------------------------------------------------------
# 门面
# --------------------------------------------------------------------------


class TestModelsFacade:
    def test_reexports_everything_the_contracts_package_exports(self) -> None:
        assert set(models.__all__) == set(contracts.__all__)

    def test_exported_objects_are_identical_not_copies(self) -> None:
        """门面不得复制类型 —— 复制会立刻违反 INV-S5（两个 ConfirmationSource）。"""
        for name in contracts.__all__:
            assert getattr(models, name) is getattr(contracts, name), name

    def test_confirmation_source_is_a_single_class_object(self) -> None:
        """INV-S5：全系统唯一，禁止各写一份。

        ``cost`` 与门面都必须拿到 ``common`` 里的同一个类对象。
        """
        from adaptive_router.contracts.common import ConfirmationSource
        from adaptive_router.contracts.cost import ConfirmationSource as CostSide
        from adaptive_router.models import ConfirmationSource as FacadeSide

        assert ConfirmationSource is CostSide is FacadeSide

    def test_terminal_state_is_a_single_class_object(self) -> None:
        from adaptive_router.contracts.common import TerminalState
        from adaptive_router.contracts.runtime import TerminalState as RuntimeSide
        from adaptive_router.models import TerminalState as FacadeSide

        assert TerminalState is RuntimeSide is FacadeSide

    def test_model_tier_is_shared_not_duplicated(self) -> None:
        """档位与推理强度被 decision 与 runtime 共享，必须是同一个类对象。"""
        from adaptive_router.contracts.common import EffortLevel, ModelTier
        from adaptive_router.contracts.decision import ModelTier as DecisionSide
        from adaptive_router.contracts.runtime import EffortLevel as RuntimeSide

        assert ModelTier is DecisionSide
        assert EffortLevel is RuntimeSide

    def test_facade_module_contains_no_logic(self) -> None:
        """models.py 只能有 import 与 __all__ 赋值。"""
        tree = ast.parse((SRC / "models.py").read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                continue
            if isinstance(node, ast.Assign):
                continue
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
                continue  # docstring
            pytest.fail(f"models.py 出现了不该有的语句：{ast.dump(node)[:120]}")

    def test_facade_works_for_the_documented_usage(self) -> None:
        """架构 11.2 原文示例：外部可简单地 import 一个契约。"""
        from adaptive_router.models import RoutingPlan  # noqa: F401

        assert RoutingPlan is not None


# --------------------------------------------------------------------------
# 依赖方向
# --------------------------------------------------------------------------


class TestContractDependencyDirection:
    @pytest.mark.parametrize("module", CONTRACT_MODULES)
    def test_contract_modules_do_not_import_each_other_circularly(
        self, module: str
    ) -> None:
        """decision / runtime / cost **互不依赖**；common 不依赖任何同包模块。"""
        imported = _imports_of(CONTRACTS / f"{module}.py")
        siblings = {m for m in CONTRACT_MODULES if m != module} & imported
        if module == "common":
            assert not siblings, f"common 必须是最底层，却导入了 {siblings}"
        else:
            # decision / runtime / cost 只允许依赖 common
            assert siblings <= {"common"}, f"{module} 出现了横向依赖 {siblings}"

    def test_common_is_the_only_shared_base(self) -> None:
        for module in CONTRACT_MODULES:
            imported = _imports_of(CONTRACTS / f"{module}.py")
            assert "adaptive_router" not in imported, (
                f"{module} 不得依赖契约包之外的核心模块（架构 11.2）"
            )

    def test_contracts_do_not_import_behavior_modules(self) -> None:
        """第 2 步纪律：契约不得依赖任何行为模块。"""
        behavior = {
            "session",
            "capabilities",
            "planner",
            "model_router",
            "risk",
            "router",
        }
        for module in CONTRACT_MODULES:
            imported = _imports_of(CONTRACTS / f"{module}.py")
            leaked = behavior & imported
            assert not leaked, f"{module} 依赖了行为模块 {leaked}"

    def test_no_third_party_runtime_dependencies(self) -> None:
        """契约层只用标准库 —— 它是所有实现的地基。"""
        allowed = {
            "__future__",
            "dataclasses",
            "enum",
            "typing",
            "uuid",
        }
        for module in CONTRACT_MODULES:
            for name in _imports_of(CONTRACTS / f"{module}.py"):
                root = name.split(".")[0]
                assert root in allowed or root in CONTRACT_MODULES, (
                    f"{module} 引入了非标准库依赖 {name!r}"
                )
