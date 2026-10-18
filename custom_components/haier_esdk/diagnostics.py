"""诊断导出（设置 → 设备 → 下载诊断信息）。

导出的都是排障要用的东西：连接参数、设备 ID、当前 24 字节控制块、故障位图。
没有账号 / 密钥 / 云 token 这类敏感信息（整条链路本来就只有局域网明文），所以不脱敏。
"""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .protocol import alarm_bits

# 没有需要脱敏的字段（不上云、无凭据）；保留这个常量是为了将来加字段时有地方写
TO_REDACT: list[str] = []


async def async_get_config_entry_diagnostics(
        hass: HomeAssistant, entry: ConfigEntry) -> dict:
    coordinator = hass.data[DOMAIN][entry.entry_id]
    status = coordinator.data
    fault = coordinator.last_fault
    return {
        "entry": {
            "data": dict(entry.data),
            "options": dict(entry.options),
        },
        "connection": {
            "host": coordinator.host,
            "port": coordinator.port,
            "device_id": coordinator.device_id,
            "rediscover": getattr(coordinator, "rediscover", None),
        },
        "status": status.as_dict() if status else None,
        "fault": {
            "bitmap": fault.hex() if fault else None,
            "set_bits": alarm_bits(fault),
        },
    }
