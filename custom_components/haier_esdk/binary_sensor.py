"""故障告警（把模块主动推来的故障位图暴露成二进制传感器）。

位图含义海尔没公开，这里只做「有没有故障」的判断：等位图非全 0 即告警。
"""
from __future__ import annotations

from homeassistant.components.binary_sensor import (BinarySensorDeviceClass,
                                                    BinarySensorEntity)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER, MODULE_MODEL
from .coordinator import HaierCoordinator


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry,
                            async_add_entities: AddEntitiesCallback) -> None:
    coordinator: HaierCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([HaierFaultSensor(coordinator, entry)])


class HaierFaultSensor(CoordinatorEntity[HaierCoordinator], BinarySensorEntity):
    """空调自报的故障位图非零 → 有问题。"""

    _attr_has_entity_name = True
    _attr_translation_key = "fault"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False    # 平时不占仪表盘

    def __init__(self, coordinator: HaierCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.device_id or coordinator.host}-fault"

    @property
    def device_info(self):                       # type: ignore[no-untyped-def]
        identifier = self.coordinator.device_id or self.coordinator.host
        return {"identifiers": {(DOMAIN, identifier)}, "name": f"{MANUFACTURER} 空调",
                "manufacturer": MANUFACTURER, "model": MODULE_MODEL}

    @property
    def is_on(self) -> bool | None:
        fault = self.coordinator.last_fault
        if not fault:
            return None                          # 还没读到过
        return any(fault)

    @property
    def extra_state_attributes(self) -> dict[str, str] | None:
        fault = self.coordinator.last_fault
        return {"bitmap": fault.hex()} if fault else None
