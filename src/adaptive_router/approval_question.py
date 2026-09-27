"""``审批问题载体 + Gate 专用发出 seam`` 的 **C2b-2c-1 人机交互边界载体**（接线层）。

架构依据：`docs/architecture.md`

- **Approval Gate 节点行**：HUMAN INTERACTION BOUNDARY（**五层之外**）——
  "**不是 `bool` 函数**"；"它是可以挂起执行、释放控制权并等待外部输入的**同步阻塞边界**"；
  "**Gate 只做三件事**：暂停 → 交还控制权 → 等待人类输入"；五步图：
  暂停 → 把控制权交还给用户 → 等待明确输入 → 批准 / 拒绝 / 修改 → 产生新的授权事实；
- **L41 / L43 / L45**：审批**只列 4 项**（拟执行动作 / 影响范围 / 主要风险 / 恢复方式）；
- **`INV-10`**：高风险副作用必须经过 Gate；
- **4.1 Session BYPASS 流 + L5 / L7**（**先例**）：`[01]` 的首次询问是**回合边界**交互 ——
  `compose_first_question()` 产出"**只含问题、立即结束**"的回复，`ask_enable(reply)` 发出，
  **成功发出后**才记账；状态迁移只由 `note_user_message()` / `record_decision()` 完成；
- **5 节平台边界**：Core "禁止出现平台细节" ⇒ **Core 不弹 UI、不阻塞、不轮询**；
- **A13.5**（显式外部事实注入先例）· **A8**（disclosure 唯一入口）· **A18.5**（V1 无 Gate ⇒ 可达性受限）。

范围、冻结与授权见 `tests/behavior/README_step5b.md` **§138**（preflight）· **§139**（scope review）·
**§140**（冻结记录）· **§141**（实现授权）· **§142**（实现记录）。

**本模块的定性（冻结口径，逐字）**：

```text
C2b-2c-1 = "交还控制权" 的**最小协议**  ≠  真实交互回路  ≠  答案回收  ≠  执行恢复
```

**一条边（C2b-2c-1 只做这一件事）**：

```text
GateOutcome（C2b-2a FROZEN；**只接受挂起态** is_pending is True）
        │  ① L43 四项**逐字透传**
        │  ② `prompt` = `PROMPT_TEMPLATE` 对四项的**确定性投影**（**非自由文案**）
        ▼
   ApprovalQuestion  ──►  emit（**调用方注入**，边界外）  ──►  **当前回合结束**
```

**它是**：可发出的**审批问题** + 一个**发出 seam**（把问题交给边界外的发出者，**发出即结束**）。
**它不是**：答案回收器 · 执行恢复器 · Gate 本体 · UI / handler · 阻塞等待者。

**六条口径（§140 冻结，逐条对应 `LGC` 锁）**：

1. **卡片与卡片边不变**（`LGC-1`）：`GateOutcome` 六字段与 `settle_gate` 签名**不动**；
   本模块**不构造** `GateOutcome` / `GateDecision`；
2. **只搬运 / 零局部绑定**（`LGC-2`）：四项逐字来自 `gate_outcome.*`；`prompt` 由模板**内联**生成；
   函数体内**没有任何局部绑定**；
3. **零取值**（`LGC-3`）：**不读** `decision.modification`、**不引用** `decision`
   —— 人类答案的文本**不进入本边**（C2b-2b-1 的 F1 边界同源）；
4. **不等待 / 不阻塞 / 不轮询**（`LGC-4`）：无 `threading` / `asyncio` / `time` / `sleep` / `input` /
   `while`；`emit` **恰好调用一次**（无重试路径）；
5. **不写运行态**（`LGC-5`）：不调用 `[10]` 任何方法 · 不写终态 / attempt · 不引用 `RoutingRun`；
6. **不接另一个边界**（`LGC-6`）：不 import `session` / `entrypoint`（会话级 consent ≠ 单次动作审批）。

**五字段锁死（你的裁定）**：载荷**只允许**
``prompt`` / ``proposed_action`` / ``impact_scope`` / ``main_risk`` / ``recovery_method`` ——
新增**身份字段 / 恢复字段 / 决策字段**（`id` / `gate_id` / `metadata` / `decision` / `answer` /
`run_id` / `branch_id` / `attempt`）**一律视为越界** ⇒ 须**重新 Scope Review**。

**明确不做（越界即缺陷）**：❌ 不实现 UI / handler ❌ 不等待人类输入 ❌ 不解析 / 不回收答案
❌ 不恢复同一 run ❌ 不写运行态 / attempt ❌ 不实现第二次 Gate ❌ 不接 `session` / `entrypoint` / `C2c`。

**可达性（如实登记）**：本模块完成后，「**审批问题可被产出**」与「**问题可被交给边界外的发出者**」
**可达**（= "交还控制权的协议"完成）；「**人类答案被回收**」「**同一 run 被恢复**」
「**真实交互回路成立**」**仍不可达**（属 `C2b-2c-2` / `C2b-2c-3` / `C2c`）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from .approval_gate_wiring import GateOutcome

__all__ = [
    "PROMPT_TEMPLATE",
    "ApprovalQuestion",
    "ApprovalQuestionError",
    "build_approval_question",
    "emit_approval_question",
]

#: **固定措辞**：审批问题的模板（对 L43 四项的**确定性投影**；**不允许自由文案层**）。
#:
#: ⚠️ 它只做**格式填充**，不做任何语义判断 —— "这句话该怎么问"不属 Core 的解释范围；
#: "人类说了什么"更不在本模块（本模块**不读** ``decision``）。
PROMPT_TEMPLATE = (
    "本次动作需要你的明确授权：{proposed_action}"
    "（影响范围：{impact_scope}；主要风险：{main_risk}；恢复方式：{recovery_method}）"
)


class ApprovalQuestionError(RuntimeError):
    """C2b-2c-1 接线级显式失败：**不是** Gate，也**不是**答案回收器。

    ⚠️ 它只在**接线自身**的前置条件不成立时抛出：对象不是 C2b-2a 的恒定载体、
    该卡片**不在挂起态**（已解析 ⇒ 不该再问）、或模板填充结果不是非空 str。
    ⚠️ 它**不得**被降级成"照样产出问题" —— 对**已解析**的卡片再问一次，
    会让人类重复回答一个已经答过的问题（`INV-10` 的授权事实因此被污染）。
    """


@dataclass(frozen=True)
class ApprovalQuestion:
    """交给**边界外**的问题（C2b-2c-1 的唯一产出）。**恰好五字段**，多一个都不许。

    ⚠️ **pure carrier**：本类不做值校验，也不表达"是否获批"——它只承载**要问什么**。
    ⚠️ ``prompt`` 由 ``PROMPT_TEMPLATE`` 对四项**逐字填充**得到（**确定性投影**，
    不是自由文案）：边界外**只接收并发送**，不需要自己再拼一次文本。
    ⚠️ 本类**不含** `decision` / `answer` / `id` / `gate_id` / `metadata` /
    `run_id` / `branch_id` / `attempt` —— 五字段**锁死**（新增即越界，须重开 Scope Review）。
    """

    prompt: str
    proposed_action: str
    impact_scope: str
    main_risk: str
    recovery_method: str


def _require_gate_outcome(gate_outcome: object) -> GateOutcome:
    """身份守卫 + **挂起态守卫**：只有"仍在等待人类输入"的卡片才允许产出问题。

    ⚠️ 已解析（`is_pending is False`）⇒ **显式失败**：那表示人类**已经**回答过了
    （`APPROVE` / `DENY` / `MODIFY`），再问一次就是**重复提问**。
    """
    if not isinstance(gate_outcome, GateOutcome):
        raise ApprovalQuestionError(
            f"build_approval_question 只接受 GateOutcome（C2b-2a 的恒定载体），收到 {gate_outcome!r}"
        )
    if gate_outcome.is_pending is not True:
        raise ApprovalQuestionError(
            "GateOutcome 不在挂起态（is_pending is not True）⇒ 人类**已经回答过**，"
            "不得重复提问（本条只对『仍在等待输入』的卡片产出问题）"
        )
    return gate_outcome


def _require_prompt(prompt: object) -> str:
    """**严格 `type is str`** 且非空（模板填充结果必须是可发出的文本）。

    ⚠️ 用 `type(...) is not str` 而不是 `isinstance`（`bool`/`str` 子类一律拒收）；
    空串检查用 `== ""` 而**不用** `.strip()` —— 后者是本边**唯一**可能引入的字符串方法，
    而 `LGC-3` 要求"除 `format` 外不调用任何字符串方法"（本边**零取值、零文本加工**）。
    """
    if type(prompt) is not str or prompt == "":
        raise ApprovalQuestionError(
            f"prompt 必须是非空 str（PROMPT_TEMPLATE 的填充结果），收到 {prompt!r}"
        )
    return prompt


def build_approval_question(*, gate_outcome: GateOutcome) -> ApprovalQuestion:
    """把**挂起态**卡片的 L43 四项**确定性投影**成可发出的问题。**唯一构造点**。

    ⚠️ 本函数**不拥有审批判断权**：它只消费**已经存在**的 `gate_outcome`，
    **不生成** `GateDecision`、**不判断**"是否需要审批"、**不读**人类答案。
    ⚠️ 入参先过 `_require_gate_outcome`（**身份 + 挂起态**双守卫）：**已解析的卡片 ⇒ 显式失败**
    （人类已经回答过，不得重复提问）。
    ⚠️ `prompt` 在 `ApprovalQuestion(...)` 的实参处**内联**生成（你的裁定：**乙**）——
    保持生成路径最窄。
    ⚠️ **函数体内零局部绑定**：守卫以**嵌套调用**形式内联，四项的对象身份与文本
    因此不可能被中途替换（不让 `checked = …` 这类中间名出现在函数体里）。
    """
    return ApprovalQuestion(
        prompt=_require_prompt(
            PROMPT_TEMPLATE.format(
                proposed_action=_require_prompt(
                    _require_gate_outcome(gate_outcome).proposed_action
                ),
                impact_scope=_require_prompt(
                    _require_gate_outcome(gate_outcome).impact_scope
                ),
                main_risk=_require_prompt(_require_gate_outcome(gate_outcome).main_risk),
                recovery_method=_require_prompt(
                    _require_gate_outcome(gate_outcome).recovery_method
                ),
            )
        ),
        proposed_action=_require_gate_outcome(gate_outcome).proposed_action,
        impact_scope=_require_gate_outcome(gate_outcome).impact_scope,
        main_risk=_require_gate_outcome(gate_outcome).main_risk,
        recovery_method=_require_gate_outcome(gate_outcome).recovery_method,
    )


def emit_approval_question(
    *, question: ApprovalQuestion, emit: Callable[[ApprovalQuestion], Any]
) -> Any:
    """把问题交给**边界外**的发出者，**发出即结束**（不等待、不轮询、不重试）。

    ⚠️ `emit` 由**调用方注入**（照 `handlers.ask_enable` 的既有先例："发出"这件事属边界外）；
    本模块**只负责交给它**，**不负责**把问题送达人类。
    ⚠️ 返回值**原样透传**（照 `dispatch` 的冻结纪律："**直接透传 handler 返回值**、
    本模块**不包装**结果"）—— 包装会让调用方看不到自己 handler 的真实结果。
    ⚠️ `emit` 抛错时**原样上抛**：本边**不重试、不吞异常、不产生第二次调用**
    （重试会让人类收到重复的问题）。
    """
    return emit(question)
