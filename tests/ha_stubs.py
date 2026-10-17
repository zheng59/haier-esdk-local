"""最小 homeassistant 替身 —— 本机没装 HA，但集成里的 climate / binary_sensor 得能测。

只做到「import 得进来、实例化得了、属性查得到」的程度，不模拟 HA 运行时
（`async_write` 之类的由测试自己伪造）。
"""
from __future__ import annotations

import sys
import types


def _module(name: str, *, package: bool = False, **attrs: object) -> types.ModuleType:
    module = types.ModuleType(name)
    if package:
        module.__path__ = []                     # type: ignore[attr-defined]
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


class HVACMode:                                  # HA 里是 StrEnum，值相同
    OFF = "off"
    HEAT = "heat"
    COOL = "cool"
    AUTO = "auto"
    DRY = "dry"
    FAN_ONLY = "fan_only"


class HVACAction:
    OFF = "off"
    HEATING = "heating"
    COOLING = "cooling"
    DRYING = "drying"
    FAN = "fan"
    IDLE = "idle"
    PREHEATING = "preheating"
    DEFROSTING = "defrosting"


class ClimateEntityFeature:
    TARGET_TEMPERATURE = 1
    TARGET_TEMPERATURE_RANGE = 2
    TARGET_HUMIDITY = 4
    FAN_MODE = 8
    PRESET_MODE = 16
    SWING_MODE = 32
    TURN_ON = 128
    TURN_OFF = 256


class UnitOfTemperature:
    CELSIUS = "°C"
    FAHRENHEIT = "°F"


class EntityCategory:
    CONFIG = "config"
    DIAGNOSTIC = "diagnostic"


class DeviceInfo(dict):
    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)


class _Subscriptable:
    def __class_getitem__(cls, item: object) -> type:        # 让 DataUpdateCoordinator[T] 能写
        return cls


class CoordinatorEntity(_Subscriptable):
    def __init__(self, coordinator: object) -> None:
        self.coordinator = coordinator


class DataUpdateCoordinator(_Subscriptable):
    def __init__(self, *args: object, **kwargs: object) -> None:
        pass


class UpdateFailed(Exception):
    pass


class _EntityStub:
    """模拟 HA 那层「property 读 _attr_*」的写法，让测试走的是同一条路。"""

    def __getattr__(self, name: str) -> object:
        if not name.startswith("_"):
            try:
                return object.__getattribute__(self, f"_attr_{name}")
            except AttributeError:
                pass
        raise AttributeError(f"{type(self).__name__!r} object has no attribute {name!r}")


class ClimateEntity(_EntityStub):
    pass


class BinarySensorEntity(_EntityStub):
    pass


class SwitchEntity(_EntityStub):
    pass


class BinarySensorDeviceClass:
    PROBLEM = "problem"


class ConfigEntry:
    pass


class ConfigFlow:
    pass


class OptionsFlow:
    pass


class HomeAssistant:
    pass


def callback(func):                              # type: ignore[no-untyped-def]
    return func


def install() -> None:
    """把替身塞进 sys.modules（幂等）。"""
    if sys.modules.get("homeassistant") is not None and getattr(
            sys.modules["homeassistant"], "__haier_stub__", False):
        return
    ha = _module("homeassistant", package=True)
    ha.__haier_stub__ = True                     # type: ignore[attr-defined]

    _module("homeassistant.components", package=True)
    _module("homeassistant.components.climate", package=True,
            ClimateEntity=ClimateEntity, ClimateEntityFeature=ClimateEntityFeature,
            HVACAction=HVACAction, HVACMode=HVACMode)
    _module("homeassistant.components.climate.const",
            FAN_AUTO="auto", FAN_HIGH="high", FAN_LOW="low", FAN_MEDIUM="medium",
            SWING_OFF="off", SWING_ON="on")
    _module("homeassistant.components.binary_sensor",
            BinarySensorEntity=BinarySensorEntity, BinarySensorDeviceClass=BinarySensorDeviceClass)
    _module("homeassistant.components.switch", SwitchEntity=SwitchEntity)

    _module("homeassistant.config_entries", ConfigEntry=ConfigEntry, ConfigFlow=ConfigFlow,
            OptionsFlow=OptionsFlow)
    _module("homeassistant.const", ATTR_TEMPERATURE="temperature",
            UnitOfTemperature=UnitOfTemperature)
    _module("homeassistant.core", HomeAssistant=HomeAssistant, callback=callback)

    _module("homeassistant.helpers", package=True)
    _module("homeassistant.helpers.device_registry", DeviceInfo=DeviceInfo)
    _module("homeassistant.helpers.entity", EntityCategory=EntityCategory)
    _module("homeassistant.helpers.entity_platform", AddEntitiesCallback=object)
    _module("homeassistant.helpers.update_coordinator", CoordinatorEntity=CoordinatorEntity,
            DataUpdateCoordinator=DataUpdateCoordinator, UpdateFailed=UpdateFailed)
