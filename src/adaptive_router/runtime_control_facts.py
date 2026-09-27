"""``[02] → P1 → [10]`` 的运行期控制事实 —— Amendment A13.5 的实现级 seam。

架构依据：`docs/architecture.md` 2 节 ``[10] Agent Manager``（**Amendment A13**，FROZEN）。

**为什么必须独立成 seam：** Core 对外的 ``RouterOutcome`` **只有四个字段**
（``plan`` / ``routing`` / ``costs`` / ``approval``，`router.py:127`，**已冻结**）——
``[02]`` 解算出来的 L45 停止命令**在 Core 边界就被丢弃**，运行半边拿不到它
（这正是 preflight 的 G3）。A13.5 因此新增一条**独立**通路，而**不扩** ``RouterOutcome``。

**语义边界（A13.5）：**

- 5B.1 冻结的干预路由表**已经声明** ``target="agent_manager"``
  （`policy.py:409-415`：*"停止分支需 ``[10]`` 的真实能力；``[02]`` 只声明意图"*，``executed=False``）
  ⇒ 本 seam 只是把这份**既有冻结意图**搬到运行半边，**不是**新语义；
- ``Intervention.STOP_BRANCHES`` 的 ``requires_argument=False``（`policy.py:413`）
  ⇒ V1 **只能**合法解释为**当前 RoutingRun 的 stop-all**；
  **不得**凭空发明"只停 ``br_a`` / ``br_b``"这类 branch-id 子集能力。
- 极性：``True`` ⇒ ``[10]`` **必须**执行 stop-all；``False`` ⇒ **不产生任何停止动作**，
  **不得**读成"禁止停止"（`policy.py:314` 的默认理由本来就是
  *"no higher-priority restriction on stopping branches"*）。

⚠️ ``[10]`` **不 import ``policy``**：本 DTO 是它唯一被允许读取的控制事实。
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["RuntimeControlFacts"]


@dataclass(frozen=True)
class RuntimeControlFacts:
    """``[02]`` 解算后的运行期控制事实（V1 只有一个字段）。"""

    stop_branches_requested: bool

    def __post_init__(self) -> None:
        # `type is bool` —— 显式拒 int（Python 的 bool 是 int 子类，反向不成立）
        if type(self.stop_branches_requested) is not bool:
            raise TypeError(
                "stop_branches_requested 必须是 bool，"
                f"收到 {self.stop_branches_requested!r}"
            )
