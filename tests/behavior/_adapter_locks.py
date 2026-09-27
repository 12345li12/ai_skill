"""``[11] Runtime Adapter`` 的边界锁检测器（纯函数）。

用途与 `_agent_manager_locks.py` 相同：① 对**真实**生产源码断言；② 接受**合成变异源码**字符串做
probe，证明每条锁**会因目标语义变异而红**（变异证据永久留在 CI，不改真实文件）。

⚠️ **可复用的部分一律直接复用**，**绝不**再抄一份朴素 import 判定 ——
`test_import_detectors.TestNoReDivergence` 会判红（`[10]` 轮已真实发生过一次）。
本文件因此**只新增一个**检测器（``modules_importing``），其余全部转发。
"""

from __future__ import annotations

from typing import Mapping

from _agent_manager_locks import (  # noqa: F401  (re-export 供测试统一引用)
    clock_call_hits,
    manager_data_attribute_reads,
    module_constant_expression,
)
from _context_locks import (  # noqa: F401
    calls_to,
    class_method_node,
    exact_keys_comparisons,
    forbidden_import_hits,
    helper_call_count,
    keyword_only_defaults,
    mapping_get_calls,
    open_calls,
    resolved_symbols,
)
from _import_detectors import imported_module_tokens  # noqa: F401
from _scheduler_locks import (  # noqa: F401
    dataclass_field_names,
    enum_member_names,
    signature_shape,
)

__all__ = [
    "calls_to",
    "class_method_node",
    "clock_call_hits",
    "dataclass_field_names",
    "enum_member_names",
    "exact_keys_comparisons",
    "forbidden_import_hits",
    "helper_call_count",
    "imported_module_tokens",
    "keyword_only_defaults",
    "manager_data_attribute_reads",
    "mapping_get_calls",
    "module_constant_expression",
    "modules_importing",
    "open_calls",
    "resolved_symbols",
    "signature_shape",
]


def modules_importing(sources: Mapping[str, str], package: str) -> tuple[str, ...]:
    """**导入了** ``package`` 的模块路径（按路径排序）—— L11-1 / L11-2 / L11-7 的唯一判定点。

    直接复用共用检测器 ``forbidden_import_hits``（它已处理
    ``from . import adapters`` 这类 ``node.module is None`` 的形式，
    以及 ``import adaptive_router.adapters.xxx`` 这类点路径）——
    本函数**不**自行解析 ``ImportFrom.module``（那会构成第二套朴素判定）。
    """
    hits = [
        path
        for path, source in sources.items()
        if forbidden_import_hits(source, (package,))
    ]
    return tuple(sorted(hits))
