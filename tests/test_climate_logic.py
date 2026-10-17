"""climate / binary_sensor 实体逻辑测试（用最小 HA 替身，不碰真机）。"""
from __future__ import annotations

import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import ha_stubs  # noqa: E402

ha_stubs.install()

from homeassistant.components.climate import ClimateEntityFeature, HVACAction, HVACMode  # noqa: E402
from homeassistant.components.climate.const import (FAN_AUTO, FAN_HIGH, FAN_LOW,  # noqa: E402
                                                    FAN_MEDIUM, SWING_OFF, SWING_ON)

from custom_components.haier_esdk.binary_sensor import HaierFaultSensor  # noqa: E402
from custom_components.haier_esdk.climate import (DEVICE_TO_HA_FAN, DEVICE_TO_HA_MODE,  # noqa: E402
                                                  HA_TO_DEVICE_FAN, HA_TO_DEVICE_MODE,
                                                  HaierClimate)
from custom_components.haier_esdk.protocol import HaierStatus  # noqa: E402
from custom_components.haier_esdk.switch import (HaierHealthSwitch,  # noqa: E402
                                                HaierRemoteLockSwitch)

BLOCK = bytes.fromhex("001b003a000300000000000100020000000900000000000b")   # 开·制冷·27℃·低风·健康


class RecordingClient:
    """记录 set_hvac 收到的参数，不做真通信。"""

    def __init__(self, status: HaierStatus, calls: list[dict]) -> None:
        self.status = status
        self.calls = calls

    def set_hvac(self, **changes: object) -> HaierStatus:      # noqa: D102
        self.calls.append(changes)
        return self.status

    def set_bits(self, index: int, mask: int, on: bool) -> HaierStatus:   # noqa: D102
        self.calls.append({"bits": (index, mask, on)})
        return self.status


class FakeCoordinator:
    def __init__(self, status: HaierStatus) -> None:
        self.data = status
        self.device_id = "001122334455"
        self.last_fault = b"\x00" * 6
        self.calls: list[dict] = []

    async def async_write(self, job):                          # noqa: D102
        self.calls = []
        return job(RecordingClient(self.data, self.calls))


def make_entity(block: bytes = BLOCK) -> tuple[HaierClimate, FakeCoordinator]:
    coordinator = FakeCoordinator(HaierStatus(block))
    return HaierClimate(coordinator, None), coordinator         # type: ignore[arg-type]


class EntityDeclarationsTest(unittest.TestCase):
    def test_supported_features_and_limits(self) -> None:
        entity, _ = make_entity()
        features = entity.supported_features
        for feature in (ClimateEntityFeature.TARGET_TEMPERATURE, ClimateEntityFeature.FAN_MODE,
                        ClimateEntityFeature.SWING_MODE):
            self.assertTrue(features & feature)
        self.assertEqual(entity.target_temperature_step, 1.0)   # 半度被主板拒绝
        self.assertEqual((entity.min_temp, entity.max_temp), (16, 30))
        self.assertEqual(entity.temperature_unit, "°C")
        self.assertIn(HVACMode.OFF, entity.hvac_modes)
        self.assertEqual(entity.name, None)                     # 用设备名
        self.assertTrue(entity.has_entity_name)

    def test_mode_and_fan_mappings_are_bijective(self) -> None:
        self.assertEqual(len(set(DEVICE_TO_HA_MODE.values())), len(DEVICE_TO_HA_MODE))
        self.assertEqual(len(set(DEVICE_TO_HA_FAN.values())), len(DEVICE_TO_HA_FAN))
        for device, ha in DEVICE_TO_HA_MODE.items():
            self.assertEqual(HA_TO_DEVICE_MODE[ha], device)
        for device, ha in DEVICE_TO_HA_FAN.items():
            self.assertEqual(HA_TO_DEVICE_FAN[ha], device)
        self.assertEqual(sorted(DEVICE_TO_HA_FAN.values()),
                         sorted([FAN_LOW, FAN_MEDIUM, FAN_HIGH, FAN_AUTO]))


class StateTest(unittest.TestCase):
    def test_reads_device_state(self) -> None:
        entity, _ = make_entity()
        self.assertEqual(entity.hvac_mode, HVACMode.COOL)
        self.assertEqual(entity.hvac_action, HVACAction.COOLING)
        self.assertEqual(entity.fan_mode, FAN_LOW)
        self.assertEqual(entity.swing_mode, SWING_OFF)
        self.assertEqual(entity.target_temperature, 27.0)
        self.assertEqual(entity.current_temperature, 27.0)
        self.assertEqual(entity.current_humidity, 58)
        self.assertTrue(entity.extra_state_attributes["health_mode"])

    def test_powered_off_reports_off(self) -> None:
        entity, _ = make_entity(HaierStatus(BLOCK).with_power(False).raw)
        self.assertEqual(entity.hvac_mode, HVACMode.OFF)
        self.assertEqual(entity.hvac_action, HVACAction.OFF)

    def test_every_device_mode_has_an_ha_mode(self) -> None:
        for device_mode, ha_mode in DEVICE_TO_HA_MODE.items():
            block = HaierStatus(BLOCK).with_mode(device_mode).raw
            entity, _ = make_entity(block)
            self.assertEqual(entity.hvac_mode, ha_mode)

    def test_swing_on(self) -> None:
        entity, _ = make_entity(HaierStatus(BLOCK).with_byte(15, 1).raw)
        self.assertEqual(entity.swing_mode, SWING_ON)


class WriteTest(unittest.IsolatedAsyncioTestCase):
    async def test_set_temperature_on_off_unit_powers_on(self) -> None:
        entity, coordinator = make_entity(HaierStatus(BLOCK).with_power(False).raw)
        await entity.async_set_temperature(temperature=24)
        self.assertEqual(coordinator.calls, [{"power": True, "temperature": 24.0}])

    async def test_set_temperature_on_running_unit_only_sets_temp(self) -> None:
        entity, coordinator = make_entity()
        await entity.async_set_temperature(temperature=24)
        self.assertEqual(coordinator.calls, [{"power": None, "temperature": 24.0}])

    async def test_set_hvac_mode_off(self) -> None:
        entity, coordinator = make_entity()
        await entity.async_set_hvac_mode(HVACMode.OFF)
        self.assertEqual(coordinator.calls, [{"power": False}])

    async def test_set_hvac_mode_heat_turns_on(self) -> None:
        entity, coordinator = make_entity(HaierStatus(BLOCK).with_power(False).raw)
        await entity.async_set_hvac_mode(HVACMode.HEAT)
        self.assertEqual(coordinator.calls, [{"power": True, "mode": "heat"}])

    async def test_set_fan_mode(self) -> None:
        entity, coordinator = make_entity()
        await entity.async_set_fan_mode(FAN_AUTO)
        self.assertEqual(coordinator.calls, [{"fan": "auto"}])

    async def test_set_swing_mode(self) -> None:
        entity, coordinator = make_entity()
        await entity.async_set_swing_mode(SWING_ON)
        self.assertEqual(coordinator.calls, [{"swing": True}])
        await entity.async_set_swing_mode(SWING_OFF)
        self.assertEqual(coordinator.calls, [{"swing": False}])

    async def test_turn_on_keeps_current_mode(self) -> None:
        entity, coordinator = make_entity(HaierStatus(BLOCK).with_power(False).raw)
        await entity.async_turn_on()
        self.assertEqual(coordinator.calls, [{"power": True}])
        await entity.async_turn_off()
        self.assertEqual(coordinator.calls, [{"power": False}])

    async def test_unsupported_mode_raises(self) -> None:
        entity, _ = make_entity()
        with self.assertRaises(ValueError):
            await entity.async_set_hvac_mode("turbo")


class DeviceInfoTest(unittest.TestCase):
    def test_device_info(self) -> None:
        entity, _ = make_entity()
        info = entity.device_info
        self.assertIn(("haier_esdk", "001122334455"), info["identifiers"])
        self.assertEqual(info["connections"], {("mac", "00:11:22:33:44:55")})
        self.assertEqual(info["manufacturer"], "Haier")
        self.assertEqual(entity.unique_id, "001122334455-climate")


class SwitchTest(unittest.IsolatedAsyncioTestCase):
    """健康模式 / 遥控锁：直接读写控制块里的位。"""

    def _switches(self) -> tuple[HaierHealthSwitch, HaierRemoteLockSwitch, FakeCoordinator]:
        coordinator = FakeCoordinator(HaierStatus(BLOCK))
        return HaierHealthSwitch(coordinator), HaierRemoteLockSwitch(coordinator), coordinator

    def test_reads_bits_from_control_block(self) -> None:
        health, lock, _ = self._switches()
        self.assertTrue(health.is_on)          # c17 = 0x09 → bit3 已开
        self.assertFalse(lock.is_on)           # c16 = 0x00
        self.assertEqual(health.unique_id, "001122334455-health")
        self.assertEqual(lock.unique_id, "001122334455-remote_lock")
        self.assertFalse(lock.entity_registry_enabled_default)   # 高级功能，默认不建

    async def test_toggle_health(self) -> None:
        health, _, coordinator = self._switches()
        await health.async_turn_off()
        self.assertEqual(coordinator.calls, [{"bits": (17, 0x08, False)}])
        await health.async_turn_on()
        self.assertEqual(coordinator.calls, [{"bits": (17, 0x08, True)}])

    async def test_toggle_remote_lock(self) -> None:
        _, lock, coordinator = self._switches()
        await lock.async_turn_on()
        self.assertEqual(coordinator.calls, [{"bits": (16, 0x80, True)}])
        await lock.async_turn_off()
        self.assertEqual(coordinator.calls, [{"bits": (16, 0x80, False)}])

    def test_lock_bit_matches_real_device_layout(self) -> None:
        """真机探测过：c16 bit7 = 遥控锁。"""
        _, lock, _ = self._switches()
        self.assertEqual((lock._byte, lock._mask), (16, 0x80))
        self.assertTrue(HaierStatus(BLOCK).with_bits(16, 0x80, True).raw[16] & 0x80)


class FaultSensorTest(unittest.TestCase):
    def _sensor(self, fault: bytes) -> HaierFaultSensor:
        coordinator = FakeCoordinator(HaierStatus(BLOCK))
        coordinator.last_fault = fault
        return HaierFaultSensor(coordinator, None)               # type: ignore[arg-type]

    def test_unknown_before_first_read(self) -> None:
        self.assertIsNone(self._sensor(b"").is_on)

    def test_no_fault(self) -> None:
        self.assertFalse(self._sensor(b"\x00" * 6).is_on)

    def test_fault_reported_with_bitmap(self) -> None:
        sensor = self._sensor(b"\x00\x08\x00\x00\x00\x00")
        self.assertTrue(sensor.is_on)
        self.assertEqual(sensor.extra_state_attributes, {"bitmap": "000800000000"})


if __name__ == "__main__":
    unittest.main()
