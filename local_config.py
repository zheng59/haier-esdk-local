#!/usr/bin/env python3
"""本地私有配置的加载器（**这个文件会提交进仓库**）。

真实的空调 IP / MAC 不写在这里。取值优先级：

1. 同目录的 ``local_settings.py`` —— **已在 .gitignore 里，不会提交**；
   从 ``local_settings.example.py`` 复制一份填好即可；
2. 环境变量 ``HAIER_HOST`` / ``HAIER_PORT`` / ``HAIER_MAC``；
3. 下面的占位符 —— 只够跑测试和文档示例，连不上真机。

注意：请求信封里那 12 位 ASCII「MAC」同时就是设备 ID，
必须和模块一致，所以本机调试前请先配好 ``local_settings.py``。
"""
from __future__ import annotations

import os

DEFAULT_HOST = "192.168.1.100"      # 占位符，不是任何真机
DEFAULT_PORT = 56800
DEFAULT_MAC = "001122334455"        # 占位符：12 位 ASCII MAC（即设备 ID）


def _from_settings_file(name: str):
    try:
        import local_settings  # type: ignore[import-not-found]
    except ImportError:
        return None
    return getattr(local_settings, name, None)


def _pick(name: str, env: str, fallback):
    value = _from_settings_file(name)
    if value:
        return value
    value = os.environ.get(env)
    if value:
        return value
    return fallback


HOST = str(_pick("HOST", "HAIER_HOST", DEFAULT_HOST))
PORT = int(_pick("PORT", "HAIER_PORT", DEFAULT_PORT))
MAC = str(_pick("MAC", "HAIER_MAC", DEFAULT_MAC))

__all__ = ["HOST", "PORT", "MAC", "DEFAULT_HOST", "DEFAULT_PORT", "DEFAULT_MAC"]
