"""兼容层 —— 真正实现只有一份：custom_components/haier_esdk/client.py"""
import component_loader  # noqa: F401  导入即完成加载

from haier_esdk.client import *  # noqa: F401,F403
from haier_esdk.client import (  # noqa: F401
    DEFAULT_MAC, DEFAULT_PORT, HaierClient, HaierCommandRejected, HaierError, format_status,
)
from haier_esdk.protocol import Envelope, EppFrame, HaierStatus  # noqa: F401  顺手带上，调试方便

_fmt = format_status          # 老脚本里的简写
