"""pytest 全局配置。

把 ``src`` 加入 ``sys.path``，使 ``adaptive_router`` 无需安装即可导入。
放在 ``tests/`` 下而非仓库根，是为了避免污染根目录的导入语义。
"""

from __future__ import annotations

import pathlib
import sys

_SRC = pathlib.Path(__file__).resolve().parents[1] / "src"

if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))
