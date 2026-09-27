"""models —— 契约包的**便利门面**（re-export only）。

架构依据：`docs/architecture.md` 第 11.2 节

> 外部可以简单地 ``from adaptive_router.models import RoutingPlan``，
> 而**内部代码知道真正的契约归属**。

⚠️ 本模块**只做 re-export**：
   不定义新类型、不加默认值、不做转换、不加逻辑。
   真正的契约归属见 ``adaptive_router.contracts`` 下的四个模块。
"""

from __future__ import annotations

from .contracts import *  # noqa: F401,F403
from .contracts import __all__ as _contracts_all

__all__ = list(_contracts_all)
