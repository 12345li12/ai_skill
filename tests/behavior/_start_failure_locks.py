"""``[10]`` **start-side failure commit**（``commit_start_failure``）的结构锁检测器（纯函数）。

范围、冻结骨架与断言计划见 `tests/behavior/README_step5b.md` **§249**（实现授权申请）；
实现记录 / `LSF` 实测 / `MSF` 变异证据见 **`§250`**。

⚠️ **一律复用，不抄第二套判定**：签名 / 守卫 / 逐方法哈希 / 调用点 / 属性读取 / 模块常量 /
导入面 / 时钟全部**转发**既有检测器（见下方 import 块）。本文件**不新增任何判定谓词**：

    「恰 3 个 kw-only · 全无默认值 · 无 vararg/kwarg」  由 ``signature_shape`` 表达；
    「返回 ``None``」·「守卫**顺序**」                由 **测试侧**局部 AST helper
                                                      （``returns`` 形态 · ``raise`` 行号）表达；
    「封闭状态面 = ``None`` / ``WAITING``」            由 **测试侧**局部 AST helper
                                                      （方法体内 ``BranchState.<成员>`` 引用集）表达；
    「``new_agent_id`` 恰 4 处、新增的**恰一处**落在 T1 分支内 · T2 分支 0 处」
                                                      由 ``calls_to``（全作用域）计数 +
                                                      **测试侧**行号范围过滤表达；
    「``AgentRun(`` 构造点仍恰 1」                     由 ``calls_to`` 的计数表达；
    「``record_outcome`` 不被新入口调用」              由 ``calls_to``（本模块内 = 0）
                                                      + **测试侧**跨文件普查表达；
    「`LSF-4` / `LSF-5` 冻结方法逐字节未改」           由 ``method_source_hashes`` + 权威表对照表达；
    「`LSF-3` 模块常量逐字未改」                       由 ``module_constant_expression`` 表达；
    「`LSF-6` adapter-independence」                   由 ``signature_shape`` · ``referenced_names`` ·
                                                      ``imported_module_tokens`` / ``imported_names`` 表达
    —— 以上都**不需要**新的检测器谓词。

⚠️ **本文件与 `test_start_failure_materialization.py` 的边界**：检测器**只**"取事实"，
   ⛔ **不做**判断；所有阈值 / 期望集 / 顺序断言都在**测试侧**。

⚠️ **⛔ 本层不证明**「`CapabilityMismatch` 调用方不会调用本入口」—— 那是 **`P6` wiring** 的
   生产可达性锁（见 `§248.3` 的 `LSF-6` 分层）。
"""

from __future__ import annotations

# ⚠️ `calls_to` 取自 **`[10]` 家族**的转发层（其本源 = `_context_locks`，**全作用域** `ast.walk`；
#    已实测与 `_terminal_locks.calls_to` 在真实源码与嵌套探针上读数一致）—— 只此一处，⛔ 不并行两套。
from _agent_manager_locks import (  # noqa: F401  (re-export 供测试统一引用)
    calls_to,
    clock_call_hits,
    imported_module_tokens,
    manager_data_attribute_reads,
    manager_self_attribute_reads,
    module_constant_expression,
)
from _terminal_locks import (  # noqa: F401  (方法级检测器：签名 / 守卫 / 逐方法哈希)
    call_target_counts,
    exception_subclass_names,
    forbidden_import_hits,
    imported_names,
    method_call_target_counts,
    method_source_hashes,
    method_sources,
    raise_guards,
    referenced_names,
    signature_shape,
)

__all__ = [
    "call_target_counts",
    "calls_to",
    "clock_call_hits",
    "exception_subclass_names",
    "forbidden_import_hits",
    "imported_module_tokens",
    "imported_names",
    "manager_data_attribute_reads",
    "manager_self_attribute_reads",
    "method_call_target_counts",
    "method_source_hashes",
    "method_sources",
    "module_constant_expression",
    "raise_guards",
    "referenced_names",
    "signature_shape",
]
