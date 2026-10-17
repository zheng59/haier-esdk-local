"""开关实体：climate 放不下的两个真实能力（都在真机上验过）。

* 健康模式：控制块 c17 bit3
* 遥控锁  ：控制块 c16 bit7（锁了以后面板/遥控器按不动，HA 仍可解开）

都没做成「可调项」而是开关：设备本身就是位开关。
"""
from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER, MODULE_MODEL
from .coordinator import HaierCoordinator

HEALTH_BYTE, HEALTH_MASK = 17, 0x08
LOCK_BYTE, LOCK_MASK = 16, 0x80


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry,
                            async_add_entities: AddEntitiesCallback) -> None:
    coordinator: HaierCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([HaierHealthSwitch(coordinator), HaierRemoteLockSwitch(coordinator)])


class _BitSwitch(CoordinatorEntity[HaierCoordinator], SwitchEntity):
    """按控制块里的一个位读写的开关。"""

    _attr_has_entity_name = True
    _byte = 0
    _mask = 0
    _suffix = "bit"

    def __init__(self, coordinator: HaierCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.device_id or coordinator.host}-{self._suffix}"

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(identifiers={(DOMAIN, self.coordinator.device_id or self.coordinator.host)},
                          name=f"{MANUFACTURER} 空调", manufacturer=MANUFACTURER,
                          model=MODULE_MODEL)

    @property
    def is_on(self) -> bool:
        return bool(self.coordinator.data.raw[self._byte] & self._mask)

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_write(
            lambda client: client.set_bits(self._byte, self._mask, True))

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_write(
            lambda client: client.set_bits(self._byte, self._mask, False))


class HaierHealthSwitch(_BitSwitch):
    """健康模式（自清洁/离子那类，设备管它叫 health）。"""

    _attr_translation_key = "health"
    _attr_icon = "mdi:heart-pulse"
    _byte, _mask = HEALTH_BYTE, HEALTH_MASK
    _suffix = "health"


class HaierRemoteLockSwitch(_BitSwitch):
    """遥控锁：锁住后空调面板与遥控器都按不动（HA 不受影响，可解锁）。"""

    _attr_translation_key = "remote_lock"
    _attr_icon = "mdi:lock"
    _attr_entity_registry_enabled_default = False     # 平时用不到，别默认开出来
    _byte, _mask = LOCK_BYTE, LOCK_MASK
    _suffix = "remote_lock"
