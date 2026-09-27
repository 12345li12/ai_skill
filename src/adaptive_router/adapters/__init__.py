"""``[11] Runtime Adapter`` 适配层。

架构依据：`docs/architecture.md` 2 节 ``[11] Runtime Adapter``（**Amendment A14**，FROZEN）
与 0.1 的分层边界（`:23`：本目录 = "把 Core 的决定**翻译**给平台"）。

**边界（A14.6 / L11-1 / L11-2）：**

    Core（contracts / router / policy / planner / model_router / scheduler /
          agent_manager / context_builder / cost …）  ──✗──>  adapters
    adapters                                              ──✓──>  contracts（只消费冻结输入）
    execution.py（P1）                                    ──✓──>  adapters   ← **唯一**允许者

⚠️ **Core 一律不得 import 本包**：本包的 DTO 故意放在适配层内，
这样"Core 不得依赖 Adapter"这条边界由 **import 锁**机械保证，而不是靠自觉。

⛔ **V1 只交付契约层**：`contract.py`（DTO）。**不含任何真实平台适配器**
（provider 未选定，且真实适配器需要 SDK / 凭证 / 错误模型才能产生有效断言）。
动作面（start / append / stop）的形状由行为测试中的 **in-memory 假适配器**承载。
"""

from __future__ import annotations

from .contract import (
    AdapterError,
    AdapterFailure,
    AdapterFailureKind,
    AdapterStartReceipt,
)

__all__ = [
    "AdapterError",
    "AdapterFailure",
    "AdapterFailureKind",
    "AdapterStartReceipt",
]
