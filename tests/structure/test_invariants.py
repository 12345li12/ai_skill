"""Step 4A / 4A.1 — Structural CI：INV-S3 / INV-S4 / INV-S5 的静态强制。

架构依据：`docs/architecture.md` v1.0 FROZEN 第 7.2 节（结构不变量）

    INV-S3  Cost Ledger 不得导入 Scheduler / AgentMgr / Recovery 的**控制接口**
    INV-S4  Cost Estimator 不得调用 Model Router 的**决策函数**
    INV-S5  Cost 与 Results 共用同一 ``ConfirmationSource`` 定义

强制方式（架构规定）：**导入图分析**与**类型定义唯一性检查**，
不是 grep 关键词（见 7.2 节对 INV-S1/S2 的措辞说明）。

⚠️ **本文件不覆盖 INV-S1 / INV-S2。**
   两者在 Step 4 Preflight 中被判定为 BLOCKED：

       S1  缺真实 Router 总入口（orchestration seam）→ `router.py` 为 0 字节
       S2  缺 ``[17] Project Context Loader`` 的实现函数 →
           当前只有 ``ProjectContext`` 输出契约，无读取函数可 mock，
           ``call_count == 0`` 会**平凡成立**

   按冻结架构的要求，S1/S2 需要**真实调用路径**与 **mock call count**，
   因此在实现节点出现之前**不写测试**（不 xfail、不 skip、不假装通过）。

   状态见 `tests/structure/README.md`。

---

第 4A.1 节加固说明：
  检测逻辑全部搬到 ``_detectors.py`` 的**纯函数**，本文件只负责
  "把真实仓库源码喂给纯函数"。纯函数自身的行为由
  ``test_detectors.py`` 用**合成源码**覆盖 —— 因此变异证据永久留在 CI 里，
  **不需要再修改真实仓库文件**。

第 4A.2 节范围收窄说明：
  本文件**只强制**冻结架构第 7.2 节写明的禁止边：

      INV-S3  ``cost.ledger`` → 控制模块（scheduler / agent_mgr /
              agent_manager / recovery）的导入边必须为空
      INV-S4  ``cost.estimator`` → ``model_router`` 的导入边必须为空
      INV-S5  单一 ``ConfirmationSource`` 定义 + 真实类型身份

  **已删除**四条超出冻结架构范围的 package-purity 断言
  （"整个 cost/* 一律禁止"、"ledger/estimator 内部依赖只能是 contracts"）：
  它们会冻结一套更大的依赖策略，将来出现合法的 helper 模块
  （例如只负责解析 provider quote evidence 的纯 helper）时会被误伤。
  S3/S4 应当**精准失败在真正违反架构边界**，而不是"出现任何新合法依赖"。
"""

from __future__ import annotations

import pathlib
import typing

from _detectors import (
    defining_modules,
    forbidden_edges,
    imports_from_source,
    module_name_for,
    resolve_type_hints,
)

PKG = "adaptive_router"
SRC = pathlib.Path(__file__).resolve().parents[2] / "src" / PKG

#: 受禁控制模块（INV-S3 的目标前缀）。
CONTROL_PREFIXES = (
    f"{PKG}.scheduler",
    f"{PKG}.agent_mgr",
    f"{PKG}.agent_manager",
    f"{PKG}.recovery",
)

#: INV-S4 的目标前缀。
MODEL_ROUTER_PREFIX = f"{PKG}.model_router"


# ==========================================================================
# 仓库源码装载
# ==========================================================================


def _repo_sources() -> dict[str, tuple[str, bool, str]]:
    """``{模块名: (源码, 是否包, 相对路径)}``。"""
    out: dict[str, tuple[str, bool, str]] = {}
    for path in sorted(p for p in SRC.rglob("*.py") if "__pycache__" not in str(p)):
        rel = str(path.relative_to(SRC))
        name, is_package = module_name_for(rel, PKG)
        out[name] = (path.read_text(encoding="utf-8"), is_package, rel)
    return out


def _imports_of(module_name: str) -> set[str]:
    source, is_package, _ = _repo_sources()[module_name]
    return imports_from_source(
        source, module_name=module_name, is_package=is_package
    )


def _has_code(module_name: str) -> bool:
    """模块是否已真实实现（非空、含顶层类/函数）。"""
    import ast

    source, _, _ = _repo_sources()[module_name]
    if not source.strip():
        return False
    return any(
        isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        for n in ast.parse(source).body
    )


# ==========================================================================
# INV-S3 —— Cost Ledger 不得导入控制接口
# ==========================================================================


class TestInvS3LedgerHasNoControlDependency:
    """INV-S3：``Cost Ledger 只观测，不拥有 stop / retry / rebuild 控制权``（INV-06）。"""

    def test_ledger_is_actually_implemented(self) -> None:
        """前置条件：被检查的模块必须真实存在，否则断言是空地的。"""
        assert f"{PKG}.cost.ledger" in _repo_sources()
        assert _has_code(f"{PKG}.cost.ledger"), (
            "cost/ledger.py 尚未实现，INV-S3 的断言会平凡成立"
        )

    def test_ledger_has_no_control_dependency(self) -> None:
        hits = forbidden_edges(_imports_of(f"{PKG}.cost.ledger"), CONTROL_PREFIXES)
        assert hits == set(), f"Cost Ledger 不得依赖控制模块：{sorted(hits)}"

    def test_ledger_has_no_control_methods(self) -> None:
        """INV-06 的接口面落点：无 stop / retry / rebuild。

        ⚠️ 这是 INV-S3 的**直接**对应项（"Ledger 只观测，不拥有控制权"），
           不是泛化的 package-purity 规则。
        """
        from adaptive_router.cost import CostLedger

        public = {name for name in dir(CostLedger) if not name.startswith("_")}
        for forbidden in ("stop", "retry", "rebuild", "cancel", "abort", "reschedule"):
            assert forbidden not in public, forbidden


# ==========================================================================
# INV-S4 —— Cost Estimator 不得调用 Model Router 的决策函数
# ==========================================================================


class TestInvS4EstimatorHasNoModelRouterDependency:
    """INV-S4：``Cost Estimator 只估算，不获得模型选择权``（INV-05）。

    ⚠️ 即使 ``model_router.py`` 当前是空文件，这条**禁止依赖边**依然有效：
       禁止边判定只比较模块名字符串，**不要求目标模块存在**
       （见 ``_detectors.forbidden_edges`` 与 ``test_detectors.py``）。
    """

    def test_estimator_is_actually_implemented(self) -> None:
        assert f"{PKG}.cost.estimator" in _repo_sources()
        assert _has_code(f"{PKG}.cost.estimator")

    def test_no_estimator_to_model_router_edge(self) -> None:
        hits = forbidden_edges(
            _imports_of(f"{PKG}.cost.estimator"), (MODEL_ROUTER_PREFIX,)
        )
        assert hits == set(), f"禁止依赖边被建立：{sorted(hits)}"

    def test_estimator_reads_tier_as_a_value_not_a_decision(self) -> None:
        """INV-05 允许 Estimator **读**已定档位（冻结值类型），
        受禁的是依赖**决策函数所在的模块**。
        """
        from adaptive_router.contracts import EffortLevel, ModelTier
        from adaptive_router.cost import estimator as estimator_module

        hints = resolve_type_hints(estimator_module.CostDriverSet)
        assert hints["tier"] is ModelTier
        assert hints["effort"] is EffortLevel


# ==========================================================================
# INV-S5 —— Cost 与 Results 共用同一 ConfirmationSource
# ==========================================================================


class TestInvS5SingleConfirmationSource:
    """INV-S5：``Cost 与 Results 共用同一 ConfirmationSource 定义``。

    架构第 8 节追溯矩阵把 L19 的承载标为 **``[12] + [15b]``**，
    两者的**冻结数据承载**都已存在：

        [12]  Results      → ``BranchReturn``（不要求单独的 results.py）
        [15b] Cost Ledger  → ``CostActual``
    """

    CLASS_NAME = "ConfirmationSource"

    def test_defined_exactly_once_in_the_whole_package(self) -> None:
        sources = {name: src for name, (src, _, _) in _repo_sources().items()}
        assert defining_modules(sources, self.CLASS_NAME) == [
            f"{PKG}.contracts.common"
        ]

    def test_single_class_object_across_referencing_modules(self) -> None:
        """所有引用点必须拿到**同一个类对象**（身份相等，非等值）。"""
        from adaptive_router import models as models_mod
        from adaptive_router.contracts import common as common_mod
        from adaptive_router.contracts import cost as cost_mod

        canonical = common_mod.ConfirmationSource
        assert cost_mod.ConfirmationSource is canonical
        assert models_mod.ConfirmationSource is canonical

    def test_execution_value_sources_resolve_to_the_canonical_class(self) -> None:
        """用**真实类型解析**（不是注解字符串匹配）验证三个 source 字段。"""
        from adaptive_router.contracts import common as common_mod

        hints = resolve_type_hints(common_mod.ExecutionValue)
        for field_name in ("planned_source", "requested_source", "actual_source"):
            assert hints[field_name] is common_mod.ConfirmationSource, field_name

    def test_cost_side_source_resolves_to_the_canonical_class(self) -> None:
        """``[15b]`` 的承载 ``CostActual.source``。"""
        from adaptive_router.contracts import common as common_mod
        from adaptive_router.contracts import cost as cost_mod

        hints = resolve_type_hints(cost_mod.CostActual)
        assert hints["source"] is common_mod.ConfirmationSource

    def test_results_side_origin_is_the_shared_execution_value(self) -> None:
        """``[12]`` 的承载 ``BranchReturn`` 的 ``model`` / ``effort``。

        只证明其 **origin 是公共 ``ExecutionValue``** ——
        不假装 ``BranchReturn`` 自身直接声明了 ``ConfirmationSource``。
        """
        from adaptive_router.contracts import common as common_mod
        from adaptive_router.contracts import runtime as runtime_mod

        hints = resolve_type_hints(runtime_mod.BranchReturn)
        for field_name in ("model", "effort"):
            origin = typing.get_origin(hints[field_name])
            assert origin is common_mod.ExecutionValue, field_name

    def test_results_and_cost_resolve_to_one_identity(self) -> None:
        """把 ``[12]`` 与 ``[15b]`` 两侧的**真实类型对象**放在一起比较。

        这是 INV-S5 原文"Cost 与 Results **共用**同一 ``ConfirmationSource``
        定义"的直接落点。
        """
        from adaptive_router.contracts import common as common_mod
        from adaptive_router.contracts import cost as cost_mod
        from adaptive_router.contracts import runtime as runtime_mod

        canonical = common_mod.ConfirmationSource

        cost_side = {resolve_type_hints(cost_mod.CostActual)["source"]}
        results_side = {
            resolve_type_hints(common_mod.ExecutionValue)[name]
            for name in ("planned_source", "requested_source", "actual_source")
        }
        branch_return_origins = {
            typing.get_origin(resolve_type_hints(runtime_mod.BranchReturn)[name])
            for name in ("model", "effort")
        }

        assert cost_side == {canonical}
        assert results_side == {canonical}
        assert branch_return_origins == {common_mod.ExecutionValue}
        assert cost_side == results_side

    def test_no_module_rebinds_the_name_to_something_else(self) -> None:
        """任何模块都不得把 ``ConfirmationSource`` 重新绑定成别的对象。"""
        import importlib

        canonical = importlib.import_module(
            f"{PKG}.contracts.common"
        ).ConfirmationSource

        referencing = [
            name
            for name, (src, _, _) in _repo_sources().items()
            if self.CLASS_NAME in src
        ]
        assert referencing, "没有任何模块引用 ConfirmationSource？检查分析器"

        checked = 0
        for module_name in referencing:
            module = importlib.import_module(module_name)
            if hasattr(module, self.CLASS_NAME):
                assert getattr(module, self.CLASS_NAME) is canonical, (
                    f"{module_name} 把 ConfirmationSource 绑定到了别的对象"
                )
                checked += 1
        assert checked >= 2, (
            "至少应有 contracts.common（定义）与 contracts.cost（引用）两处参与身份检查，"
            f"实际 {checked} 处"
        )


# ==========================================================================
# 检测器可达性（防止"仓库里没有目标模块 → 天然通过"）
# ==========================================================================


class TestDetectorReachability:
    """检测器**不依赖目标模块是否存在**（用合成源码证明）。

    ⚠️ 第 4A.2 节清理：原先这里还有两条断言"今天 scheduler/recovery 不存在"
       与"model_router 必须仍为空"的**仓库状态**测试，已删除 ——
       它们不是 INV-S3/S4 的不变量。将来实现 Scheduler / Recovery /
       Model Router 是**正常进展**，那时这些测试会制造假红灯。

       "当前模块不存在/为空"只作为 **Preflight 历史证据**保留在
       `tests/structure/README.md` 第 4 节，**不是持续 CI 条件**。
    """

    def test_forbidden_edge_detection_works_on_absent_targets(self) -> None:
        """同一函数在**目标模块不存在**时依然能判违规（合成输入）。"""
        synthetic = imports_from_source(
            "from .. import scheduler",
            module_name=f"{PKG}.cost.ledger",
            is_package=False,
        )
        assert forbidden_edges(synthetic, CONTROL_PREFIXES) == {f"{PKG}.scheduler"}

    def test_forbidden_edge_detection_works_on_empty_target(self) -> None:
        """目标模块**存在但为空**时同样能判违规（合成输入）。"""
        synthetic = imports_from_source(
            f"from {PKG} import model_router",
            module_name=f"{PKG}.cost.estimator",
            is_package=False,
        )
        assert forbidden_edges(synthetic, (MODEL_ROUTER_PREFIX,)) == {
            MODEL_ROUTER_PREFIX
        }

    def test_import_detection_works_without_the_target_module(self) -> None:
        """解析器本身也不查"模块是否存在"—— 它只做语法解析与字符串拼接。"""
        synthetic = imports_from_source(
            f"from .. import definitely_not_a_real_module_xyz",
            module_name=f"{PKG}.cost.ledger",
            is_package=False,
        )
        assert f"{PKG}.definitely_not_a_real_module_xyz" in synthetic

    def test_repo_loader_finds_the_expected_modules(self) -> None:
        """仓库装载器能读到 Step 3 的全部基础层模块。"""
        names = set(_repo_sources())
        for expected in (
            f"{PKG}.session",
            f"{PKG}.capabilities",
            f"{PKG}.risk",
            f"{PKG}.cost.ledger",
            f"{PKG}.cost.estimator",
            f"{PKG}.cost.summary",
            f"{PKG}.contracts.common",
        ):
            assert expected in names, expected
