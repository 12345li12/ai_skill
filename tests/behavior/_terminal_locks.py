"""``Phase A``（`[10]` 终态写入）的结构锁检测器（纯函数）。

范围与冻结项见 `tests/behavior/README_step5b.md` **§96**（Phase A Implementation Checklist）。

⚠️ **一律复用，不抄第二套判定**：import / 签名 / 容器 / 调用点 / 属性链 / 时钟 / 异常子类全部转发。
本文件**只新增三类**真实需要的判定：

    ① ``method_source_hashes``      —— 类内**逐方法**源码 SHA256（**PA-7a** 冻结区域哈希锁）
    ② ``call_target_counts``        —— 按被调用者源码形态计数（**LT-1 / LT-4**：AST 级，不做子串扫描）
    ③ ``attribute_assignment_targets`` / ``annotated_attribute_targets``
                                    —— ``self.x = …`` 与 ``self.x: T = …`` 的赋值点（**LT-5** 唯一写点）
"""

from __future__ import annotations

import ast
import hashlib

from _import_detectors import imported_names  # noqa: F401  (复用：真实 import 进来的标识符)
from _results_locks import (  # noqa: F401  (re-export 供测试统一引用)
    referenced_names,
)
from _scheduler_locks import signature_shape  # noqa: F401  (类方法签名形状)
from _validation_locks import (  # noqa: F401
    attribute_chains,
    calls_to,
    exception_subclass_names,
    forbidden_import_hits,
    imported_module_tokens,
    open_calls,
    raise_guards,
)
from _validation_wiring_locks import (  # noqa: F401
    clock_call_hits,
    function_raise_guards,
    module_level_container_assignments,
)

__all__ = [
    "annotated_attribute_targets",
    "attribute_assignment_targets",
    "attribute_chains",
    "call_target_counts",
    "calls_to",
    "clock_call_hits",
    "exception_subclass_names",
    "forbidden_import_hits",
    "function_raise_guards",
    "imported_module_tokens",
    "imported_names",
    "method_source_hashes",
    "method_sources",
    "method_call_target_counts",
    "module_level_container_assignments",
    "open_calls",
    "raise_guards",
    "referenced_names",
    "signature_shape",
]


def _tree(source: str) -> ast.Module:
    return ast.parse(source)


def _class_node(source: str, class_name: str) -> ast.ClassDef:
    for node in _tree(source).body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            return node
    raise AssertionError(f"未找到类 {class_name}")


def method_sources(source: str, class_name: str) -> dict[str, str]:
    """类内每个方法的**源码文本**（``ast.get_source_segment``）。"""
    found: dict[str, str] = {}
    for node in _class_node(source, class_name).body:
        if isinstance(node, ast.FunctionDef):
            segment = ast.get_source_segment(source, node)
            found[node.name] = segment if segment is not None else ""
    return found


def method_source_hashes(source: str, class_name: str) -> dict[str, str]:
    """类内每个方法的源码 SHA256（前 16 位十六进制）—— **PA-7a** 的比对表。"""
    return {
        name: hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
        for name, text in method_sources(source, class_name).items()
    }


def call_target_counts(source: str) -> dict[str, int]:
    """按**被调用者的源码形态**计数（AST 级）—— 用于 ``RunTerminal`` / ``self._replace`` 等锚点。

    ⚠️ **不做子串扫描**：注释与 docstring 里提到 ``RunTerminal(`` 不会计入
    （这正是"子串计数 2 但真实构造点 1"那一类假读数的防线）。
    """
    counts: dict[str, int] = {}
    for node in ast.walk(_tree(source)):
        if isinstance(node, ast.Call):
            target = ast.unparse(node.func)
            counts[target] = counts.get(target, 0) + 1
    return counts


def method_call_target_counts(
    source: str, class_name: str, method_name: str
) -> dict[str, int]:
    """**只在指定方法体内**按被调用者源码形态计数（AST 级）。

    用于"这个方法里有没有做某件事"（例如 `close_run` 里 `RunTerminal` 恰 1 处、
    `self._records.append` **0** 处）—— 比字符串切片可靠，且不受注释影响。
    """
    for node in _class_node(source, class_name).body:
        if isinstance(node, ast.FunctionDef) and node.name == method_name:
            counts: dict[str, int] = {}
            for child in ast.walk(node):
                if isinstance(child, ast.Call):
                    target = ast.unparse(child.func)
                    counts[target] = counts.get(target, 0) + 1
            return counts
    raise AssertionError(f"未找到方法 {class_name}.{method_name}")


def attribute_assignment_targets(source: str) -> tuple[str, ...]:
    """``self.x = …`` 形态（``Assign``）的赋值目标，按出现顺序 —— **LT-5** 的唯一写点锁。"""
    found: list[str] = []
    for node in ast.walk(_tree(source)):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Attribute):
                    found.append(ast.unparse(target))
    return tuple(found)


def annotated_attribute_targets(source: str) -> tuple[str, ...]:
    """``self.x: T = …`` 形态（``AnnAssign``）的赋值目标 —— 与上者区分**声明**与**再赋值**。"""
    found: list[str] = []
    for node in ast.walk(_tree(source)):
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Attribute):
            found.append(ast.unparse(node.target))
    return tuple(found)
