"""诊断导出测试（用最小 HA 替身，不碰真机）。"""
from __future__ import annotations

import pathlib
import sys
import unittest
from types import SimpleNamespace

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import ha_stubs  # noqa: E402

ha_stubs.install()

from custom_components.haier_esdk.const import DOMAIN  # noqa: E402
from custom_components.haier_esdk.diagnostics import (  # noqa: E402
    async_get_config_entry_diagnostics)
from custom_components.haier_esdk.protocol import HaierStatus  # noqa: E402

BLOCK = bytes.fromhex("001b003a000300000000000100020000000900000000000b")   # 开·制冷·27℃·低风·健康
FAULT = bytes.fromhex("000800000000")                                     # 第 11 位（byte1 bit3）


class FakeCoordinator:
    host = "192.0.2.10"
    port = 56800
    device_id = "001122334455"
    data = HaierStatus(BLOCK)
    last_fault = FAULT


class TestDiagnostics(unittest.IsolatedAsyncioTestCase):
    async def test_dump_has_connection_status_and_fault(self) -> None:
        entry = SimpleNamespace(entry_id="entry-1",
                                data={"host": "192.0.2.10", "port": 56800},
                                options={"scan_interval": 30})
        hass = SimpleNamespace(data={DOMAIN: {"entry-1": FakeCoordinator()}})

        dump = await async_get_config_entry_diagnostics(hass, entry)

        self.assertEqual(dump["connection"],
                         {"host": "192.0.2.10", "port": 56800, "device_id": "001122334455"})
        self.assertEqual(dump["entry"]["options"], {"scan_interval": 30})
        self.assertEqual(dump["status"]["mode"], "cool")
        self.assertEqual(dump["status"]["raw"], BLOCK.hex())
        self.assertEqual(dump["fault"], {"bitmap": FAULT.hex(), "set_bits": [11]})

    async def test_dump_without_status_yet(self) -> None:
        coordinator = FakeCoordinator()
        coordinator.data = None
        coordinator.last_fault = b""
        entry = SimpleNamespace(entry_id="entry-1", data={}, options={})
        hass = SimpleNamespace(data={DOMAIN: {"entry-1": coordinator}})

        dump = await async_get_config_entry_diagnostics(hass, entry)

        self.assertIsNone(dump["status"])
        self.assertEqual(dump["fault"], {"bitmap": None, "set_bits": []})


if __name__ == "__main__":
    unittest.main()
