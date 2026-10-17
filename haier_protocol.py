"""兼容层 —— 真正实现只有一份：custom_components/haier_esdk/protocol.py

（保留这个文件是为了让仓库根目录下的调试脚本照旧 from haier_protocol import ...）
"""
import component_loader  # noqa: F401  导入即完成加载

from haier_esdk.protocol import *  # noqa: F401,F403
from haier_esdk.protocol import (  # noqa: F401
    ADDR_AC, ADDR_MODULE, CMD_GET_STATUS, CMD_POWER_OFF, CMD_POWER_ON, CMD_SET,
    FANS, FANS_INV, FRAME_NAMES, HEADER_LEN, MAGIC_REQUEST, MAGIC_RESPONSE, MODES,
    MODES_INV, TYPE_ALARM, TYPE_ALARM_RESPONSE, TYPE_CONFIRM, TYPE_CONTROL,
    TYPE_GET_ALARM, TYPE_GET_VERSION, TYPE_INVALID, TYPE_REPORT, TYPE_STATUS,
    Envelope, EppFrame, HaierStatus, build_request, epp_frame, parse_envelopes, parse_epp,
)
