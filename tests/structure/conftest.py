"""让 ``tests/structure`` 下的检测器模块可直接导入。

``tests/conftest.py`` 已把 ``src`` 加入 ``sys.path``；
这里再把本目录加入，使 ``from _detectors import ...`` 稳定可用。
"""

from __future__ import annotations

import pathlib
import sys

_HERE = pathlib.Path(__file__).resolve().parent

if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))
