# adaptive-agent-router

自然语言需求规格 → Core Router 规则 → 平台 Adapter 的**自适应调度核心**。

> ⚠️ 本 README **只陈述当前已被验证的事实**。
> ⛔ 不声称存在独立可执行程序 · ⛔ 不声称 Python 3.10–3.14 全部已验证。

## 架构三层边界（不可跨越）

| 层 | 位置 | 职责 |
|---|---|---|
| 核心层 | `src/adaptive_router/` | 把规格规则变成代码；**禁止平台细节** |
| 适配层 | `src/adaptive_router/adapters/` | 把 Core 的决定翻译给平台；**不做决策** |

Specification assets are maintained separately and are not included in this distribution.

## 使用前提

本包是库，不提供 console entry point 或独立运行入口。

要驱动 Core，部署侧必须提供 external Host，并向
`build_entrypoint_handlers(...)` 提供其 required runtime injections
（当前签名含 10 个 required keyword-only inputs）。

本包不提供真实 deployment Host，也不自行拥有 deployment/session lifecycle。

`host_demo/` 是一个最小 external Host 示例，可作为集成起点。

## 安装

从构建产物安装（本仓库当前**未发布到包索引**）：

```console
python -m venv .venv
.venv/bin/python -m pip install adaptive_agent_router-0.1.0-py3-none-any.whl
```

（Windows 下解释器路径为 `.venv\Scripts\python.exe`。）

## 从源码构建

```console
python release/build_check.py     # 产出 wheel + sdist 到 .pytest-tmp/retry_prep/relpkg/dist/
```

构建走标准 PEP 517（`setuptools.build_meta`），在**工作区内的 scratch 副本**中进行
⇒ ⛔ 不写 `src/`。

## 支持范围

```text
Validated
    Python 3.14.6
    Python 3.12

NOT VALIDATED
    Python 3.10
    Python 3.11
    Python 3.13
```

`requires-python = ">=3.10"` 是**声明面**；当前的**验证面**只有上面两个解释器。
⛔ 请勿读成「Python 3.10–3.14 verified」。

## 依赖

**zero runtime third-party dependency** —— `dependencies = []`。

这是**设计状态**，不是遗漏：`src/` 的顶层 import 全部来自标准库。

## 验证

本分发支持：

- test suite execution（`python -m pytest -q`）
- package build verification（`release/build_check.py`）
- wheel verification（`release/wheel_check.py`）
- sdist verification（`release/sdist_check.py`）

Internal governance verification artifacts are maintained separately.

## 许可证

The runtime components of this repository are licensed under Apache License 2.0.

Copyright 2026 Wesac Piletya.

The license scope does not automatically extend to:
- the specification assets (`original-skill/`)
- the internal governance records

Those components are not part of this distribution
and are not covered by this LICENSE file.
