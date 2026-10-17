"""海尔空调的 climate 实体。

能力（实测 2026-10-17）：开关 / 模式(自动·制冷·制热·除湿·送风) / 设定温度 16~30℃（整度）/
风速(低·中·高·自动) / 摆风 / 健康。半度、极速、静音、显示屏开关这台主板不支持。
"""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.climate import (ClimateEntity, ClimateEntityFeature,
                                             HVACAction, HVACMode)
from homeassistant.components.climate.const import (FAN_AUTO, FAN_HIGH, FAN_LOW,
                                                    FAN_MEDIUM, SWING_OFF, SWING_ON)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER, MODULE_MODEL
from .coordinator import HaierCoordinator
from .protocol import alarm_bits

_LOGGER = logging.getLogger(__name__)

# 设备口径（smartair2）↔ Home Assistant 口径
DEVICE_TO_HA_MODE = {"auto": HVACMode.AUTO, "cool": HVACMode.COOL, "heat": HVACMode.HEAT,
                     "dry": HVACMode.DRY, "fan": HVACMode.FAN_ONLY}
HA_TO_DEVICE_MODE = {v: k for k, v in DEVICE_TO_HA_MODE.items()}

DEVICE_TO_HA_FAN = {"low": FAN_LOW, "mid": FAN_MEDIUM, "high": FAN_HIGH, "auto": FAN_AUTO}
HA_TO_DEVICE_FAN = {v: k for k, v in DEVICE_TO_HA_FAN.items()}

# 这台主板只有通风机在转的信息，没有压缩机反馈位 → 只在「关机」上做准确判断，
# 其余按模式给一个合理的 hvac_action。
MODE_TO_ACTION = {HVACMode.COOL: HVACAction.COOLING, HVACMode.HEAT: HVACAction.HEATING,
                  HVACMode.DRY: HVACAction.DRYING, HVACMode.FAN_ONLY: HVACAction.FAN,
                  HVACMode.AUTO: HVACAction.IDLE}


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry,
                            async_add_entities: AddEntitiesCallback) -> None:
    coordinator: HaierCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([HaierClimate(coordinator, entry)])


class HaierClimate(CoordinatorEntity[HaierCoordinator], ClimateEntity):
    """一台空调。"""

    _attr_has_entity_name = True
    _attr_name = None
    _attr_icon = "mdi:air-conditioner"
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_target_temperature_step = 1.0          # 半度被主板拒绝（回 INVALID）
    _attr_min_temp = 16
    _attr_max_temp = 30
    _attr_hvac_modes = [HVACMode.OFF, HVACMode.AUTO, HVACMode.COOL, HVACMode.HEAT,
                        HVACMode.DRY, HVACMode.FAN_ONLY]
    _attr_fan_modes = [FAN_LOW, FAN_MEDIUM, FAN_HIGH, FAN_AUTO]
    _attr_swing_modes = [SWING_OFF, SWING_ON]
    _attr_supported_features = (ClimateEntityFeature.TARGET_TEMPERATURE
                                | ClimateEntityFeature.FAN_MODE
                                | ClimateEntityFeature.SWING_MODE)
    # 开/关走 hvac_mode（HVACMode.OFF），不用已废弃的 TURN_ON/TURN_OFF 特性
    _enable_turn_on_off_backwards_compat = False

    def __init__(self, coordinator: HaierCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"{coordinator.device_id or coordinator.host}-climate"

    # ------------------------------------------------------------------ 设备信息
    @property
    def device_info(self) -> DeviceInfo:
        coordinator = self.coordinator
        identifier = coordinator.device_id or coordinator.host
        info = DeviceInfo(
            identifiers={(DOMAIN, identifier)},
            name=f"{MANUFACTURER} 空调",
            manufacturer=MANUFACTURER,
            model=MODULE_MODEL,
        )
        mc = coordinator.device_id
        if mc and len(mc) == 12:
            mac = ":".join(mc[i:i + 2] for i in range(0, 12, 2)).lower()
            info["connections"] = {("mac", mac)}
        return info

    # ------------------------------------------------------------------ 状态
    @property
    def _status(self):                      # type: ignore[no-untyped-def]
        return self.coordinator.data

    @property
    def current_temperature(self) -> float | None:
        return self._status.room_temperature

    @property
    def current_humidity(self) -> int | None:
        return self._status.room_humidity

    @property
    def target_temperature(self) -> float | None:
        return self._status.target_temperature

    @property
    def hvac_mode(self) -> HVACMode:
        if not self._status.power:
            return HVACMode.OFF
        return DEVICE_TO_HA_MODE.get(self._status.mode, HVACMode.AUTO)

    @property
    def hvac_action(self) -> HVACAction | None:
        if not self._status.power:
            return HVACAction.OFF
        return MODE_TO_ACTION.get(self.hvac_mode, HVACAction.IDLE)

    @property
    def fan_mode(self) -> str | None:
        return DEVICE_TO_HA_FAN.get(self._status.fan)

    @property
    def swing_mode(self) -> str | None:
        return SWING_ON if self._status.swing else SWING_OFF

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        status = self._status
        fault = self.coordinator.last_fault
        return {
            "health_mode": status.health,
            "control_block": status.raw.hex(),
            "device_id": self.coordinator.device_id,
            "fault_bitmap": fault.hex() if fault else None,
            "fault_bits": alarm_bits(fault) if fault else None,
        }

    # ------------------------------------------------------------------ 控制
    async def async_set_temperature(self, **kwargs: Any) -> None:
        temperature = kwargs.get(ATTR_TEMPERATURE)
        if temperature is None:
            return
        # 关机状态下改温度顺手开机，否则用户点了没反应
        power_on = None if self._status.power else True
        await self.coordinator.async_write(
            lambda client: client.set_hvac(power=power_on, temperature=float(temperature)))

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        if hvac_mode == HVACMode.OFF:
            await self.coordinator.async_write(lambda client: client.set_hvac(power=False))
            return
        device_mode = HA_TO_DEVICE_MODE.get(hvac_mode)
        if device_mode is None:
            raise ValueError(f"不支持的模式：{hvac_mode}")
        await self.coordinator.async_write(
            lambda client: client.set_hvac(power=True, mode=device_mode))

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        device_fan = HA_TO_DEVICE_FAN.get(fan_mode)
        if device_fan is None:
            raise ValueError(f"不支持的风速：{fan_mode}")
        await self.coordinator.async_write(lambda client: client.set_hvac(fan=device_fan))

    async def async_set_swing_mode(self, swing_mode: str) -> None:
        await self.coordinator.async_write(
            lambda client: client.set_hvac(swing=(swing_mode == SWING_ON)))

    async def async_turn_on(self) -> None:
        await self.coordinator.async_write(lambda client: client.set_hvac(power=True))

    async def async_turn_off(self) -> None:
        await self.coordinator.async_write(lambda client: client.set_hvac(power=False))
