"""`host_demo` 的自建 fixtures（⛔ **不 import `tests/*`**）。

本包存在的理由：`HostDemo` 要证明"被冻结的 Core 可以被**外部 Host** 驱动"，
因此它需要**自己的场景事实**——而不是复用测试替身。⛔ 借用测试替身会让证明力退化。

见 `tests/behavior/README_step5b.md` **`§277`**（Preflight）/ **`§278`**（Scope Review）/
**`§279`**（实现授权申请 · 构造契约）。
"""

from __future__ import annotations
