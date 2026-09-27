"""adaptive_router —— 自适应多代理调度的 Core Router。

三层职责边界（架构 0.1，不可跨越）：
    规格层      original-skill/     自然语言需求规格，已验证，唯一真相来源
    核心层      本包                把规格规则变成代码；禁止平台细节
    适配层      adapters/           把 Core 的决定翻译给平台；不做决策

当前阶段（架构第 10 节）：
    Step 1 架构     ✅ FROZEN
    Step 2 契约     ▶ 进行中 —— 本包当前只有 ``contracts`` 与 ``models`` 门面
    Step 3 实现     ⛔ 暂不进入

⚠️ 本模块**不导入** ``contracts``：导入门面本身不应产生副作用，
   也不应让"只想看一眼发包名"的调用方顺带加载整个契约层。
"""

from __future__ import annotations
