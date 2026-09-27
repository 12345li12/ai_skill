"""`5B.6.52.3`（P2 · Run 级 attempt 推进 + `E1` scalar ingress）结构锁的**检测器入口**（**纯转发**）。

范围、锁编号与落点见 `tests/behavior/README_step5b.md`：
**`§265.4`**（`LP2-1…9`）· **`§265.5`**（`MP2-1…9`）· **`§266.4`**（锁落点）·
**`§267.6`** / **`§268.5`**（`LP2-10`）· **`§269`**（组合写集 / 闭包 / `E1` ingress 冻结）·
**`§270`**（实现记录与实测读数）。

⚠️ **本文件不新增任何判定**（`§265` 明令本模块是「**纯转发**既有检测器」）：
一律复用既有共用检测器，⛔ 不抄第二套解析 —— 与
`test_import_detectors.TestNoReDivergence` 的收口口径一致。
"""

from __future__ import annotations

from _agent_manager_locks import (  # noqa: F401  (`[10]` 的权威 allowlist 判定)
    class_method_node,
    manager_data_attribute_reads,
    manager_self_attribute_reads,
)
from _context_locks import (  # noqa: F401  (模块级函数节点；`function_node` 不在 `_agent_manager_locks` 的转发面里)
    function_node,
)
from _terminal_locks import (  # noqa: F401  (聚合点；其自身亦为转发)
    annotated_attribute_targets,
    attribute_assignment_targets,
    attribute_chains,
    call_target_counts,
    calls_to,
    clock_call_hits,
    exception_subclass_names,
    forbidden_import_hits,
    function_raise_guards,
    imported_module_tokens,
    imported_names,
    method_call_target_counts,
    method_source_hashes,
    method_sources,
    module_level_container_assignments,
    open_calls,
    raise_guards,
    referenced_names,
    signature_shape,
)

__all__ = [
    "annotated_attribute_targets",
    "attribute_assignment_targets",
    "attribute_chains",
    "call_target_counts",
    "calls_to",
    "class_method_node",
    "clock_call_hits",
    "exception_subclass_names",
    "forbidden_import_hits",
    "function_node",
    "function_raise_guards",
    "imported_module_tokens",
    "imported_names",
    "manager_data_attribute_reads",
    "manager_self_attribute_reads",
    "method_call_target_counts",
    "method_source_hashes",
    "method_sources",
    "module_level_container_assignments",
    "open_calls",
    "raise_guards",
    "referenced_names",
    "signature_shape",
]
