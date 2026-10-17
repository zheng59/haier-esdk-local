"""把 custom_components/haier_esdk 里的 protocol/client 加载进来。

集成的 ``__init__.py`` 依赖 homeassistant，本机（没有装 HA）不能直接 import，
所以这里造一个空壳包、再按文件加载这两个模块 —— 好处是**测试与工具跑的就是集成里的同一份代码**，
不存在「开发版 / 发布版」两份实现漂移的问题。
"""
from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

PKG = "haier_esdk"
_DIR = Path(__file__).resolve().parent / "custom_components" / PKG
_SUBMODULES = ("protocol", "client")


def load() -> None:
    """幂等地把 haier_esdk.protocol / haier_esdk.client 注册进 sys.modules。"""
    pkg = sys.modules.get(PKG)
    if pkg is None or not isinstance(pkg, types.ModuleType):
        pkg = types.ModuleType(PKG)
        pkg.__path__ = [str(_DIR)]          # type: ignore[attr-defined]
        sys.modules[PKG] = pkg
    for name in _SUBMODULES:
        full = f"{PKG}.{name}"
        if full in sys.modules:
            continue
        spec = importlib.util.spec_from_file_location(full, _DIR / f"{name}.py")
        if spec is None or spec.loader is None:      # pragma: no cover
            raise ImportError(f"加载不了 {full}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[full] = module
        spec.loader.exec_module(module)


load()
