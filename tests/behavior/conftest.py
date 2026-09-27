"""行为测试的共享夹具。

⚠️ 沙箱说明：pytest 默认的 ``tmp_path`` 落在平台临时目录
（``%TEMP%/pytest-of-*``），本环境下该目录**不可访问**（WinError 5，
连 ``os.scandir`` 都被拒）。

因此这里不用 ``tmp_path`` / ``tmp_path_factory``，改为**工作区内**的
``.pytest-tmp`` 下自建目录，并在用后清理。

另外把本目录加入 ``sys.path``，使共用的 import 检测器
``from _import_detectors import ...`` 稳定可用（与 ``tests/structure/conftest.py``
同一做法）。
"""

from __future__ import annotations

import pathlib
import shutil
import sys
import uuid

import pytest

_HERE = pathlib.Path(__file__).resolve().parent

if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

_WORKSPACE = pathlib.Path(__file__).resolve().parents[2]
_TMP_BASE = _WORKSPACE / ".pytest-tmp"


@pytest.fixture
def tmp_dir() -> pathlib.Path:
    """一个位于工作区内的临时目录，测试结束后删除。"""
    _TMP_BASE.mkdir(exist_ok=True)
    path = _TMP_BASE / f"case-{uuid.uuid4().hex[:12]}"
    path.mkdir()
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)
