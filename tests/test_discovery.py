"""「IP 变了自动找回」的单元测试（纯逻辑 + 假 socket，不碰真机、不扫真网段）。"""
from __future__ import annotations

import ipaddress
import pathlib
import socket
import sys
import unittest
from datetime import timedelta
from types import SimpleNamespace
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import ha_stubs  # noqa: E402

ha_stubs.install()

from custom_components.haier_esdk import discovery  # noqa: E402
from custom_components.haier_esdk import coordinator as coordinator_mod  # noqa: E402
from custom_components.haier_esdk.const import (CONF_DEVICE_ID, CONF_HOST,  # noqa: E402
                                                CONF_PORT, CONF_REDISCOVER,
                                                CONF_SCAN_INTERVAL, DOMAIN)
from custom_components.haier_esdk.coordinator import HaierCoordinator  # noqa: E402
from custom_components.haier_esdk.protocol import HaierStatus  # noqa: E402

DEVICE_ID = b"001122334455"
BLOCK = bytes.fromhex("001b003a000300000000000100020000000900000000000b")
# 连上就推两条（故障位图 + 状态），设备 ID 藏在信封头的 40–51 偏移
BURST = bytes.fromhex("00002715" + "00" * 36 + DEVICE_ID.hex() + "00" * 27 + "13"
                      "ffff10000000000001040f5a0000000000007e")


class FakeSocket:
    """第一次 recv 回一段推送，之后超时（客户端只读不写就够了）。"""

    def __init__(self, payload: bytes) -> None:
        self._in = bytearray(payload)
        self.sent: list[bytes] = []
        self.closed = False

    def settimeout(self, value: float) -> None:      # noqa: D102
        pass

    def recv(self, bufsize: int) -> bytes:           # noqa: D102
        if not self._in:
            raise socket.timeout()
        chunk = bytes(self._in[:bufsize])
        del self._in[:bufsize]
        return chunk

    def sendall(self, data: bytes) -> None:          # noqa: D102
        self.sent.append(data)

    def close(self) -> None:                         # noqa: D102
        self.closed = True


class TestParsing(unittest.TestCase):
    def test_looks_like_device_id(self) -> None:
        self.assertTrue(discovery.looks_like_device_id("001122334455"))
        self.assertTrue(discovery.looks_like_device_id(" 001122334455 "))
        self.assertFalse(discovery.looks_like_device_id("host-192.168.1.5"))
        self.assertFalse(discovery.looks_like_device_id("0011223344"))     # 太短
        self.assertFalse(discovery.looks_like_device_id(None))

    def test_networks_from_adapters(self) -> None:
        adapters = [
            {"enabled": True, "ipv4": [{"address": "192.168.1.5/24"}]},
            {"enabled": False, "ipv4": [{"address": "10.0.0.2/24"}]},       # 网卡关了
            {"enabled": True, "ipv4": [{"address": "172.16.3.4", "network_prefix": 28}]},
            {"enabled": True, "ipv4": [{"address": "不是地址"}]},            # 脏数据忽略
            {"enabled": True, "ipv4": [{"address": "192.168.1.5/24"}]},     # 去重
            {"enabled": True, "ipv6": [{"address": "fe80::1/64"}]},
        ]
        nets = discovery.networks_from_adapters(adapters)
        self.assertEqual([str(n) for n in nets], ["192.168.1.0/24", "172.16.3.0/28"])

    def test_expand_hosts(self) -> None:
        self.assertEqual(discovery.expand_hosts([ipaddress.ip_network("192.168.1.0/30")]),
                         ["192.168.1.1", "192.168.1.2"])
        self.assertEqual(discovery.expand_hosts([]), [])

    def test_expand_hosts_respects_limit(self) -> None:
        nets = [ipaddress.ip_network("192.168.1.0/24"), ipaddress.ip_network("10.0.0.0/24")]
        hosts = discovery.expand_hosts(nets, limit=5)
        self.assertEqual(hosts, ["192.168.1.1", "192.168.1.2", "192.168.1.3",
                                 "192.168.1.4", "192.168.1.5"])

    def test_local_networks_skips_oversized(self) -> None:
        # /16 太大（65k 个地址）→ 退回本机 /24
        adapters = [{"enabled": True, "ipv4": [{"address": "10.1.2.3/16"}]}]
        with mock.patch.object(discovery, "fallback_network",
                               return_value=ipaddress.ip_network("10.1.2.0/24")) as fallback:
            nets = discovery.local_networks(adapters, "192.168.1.100")
        self.assertEqual([str(n) for n in nets], ["10.1.2.0/24"])
        fallback.assert_called_once()

    def test_local_networks_rejects_public(self) -> None:
        adapters = [{"enabled": True, "ipv4": [{"address": "8.8.8.7/24"}]}]
        with mock.patch.object(discovery, "fallback_network", return_value=None):
            self.assertEqual(discovery.local_networks(adapters), [])

    def test_fallback_network_uses_udp_route(self) -> None:
        sock = mock.MagicMock()
        sock.getsockname.return_value = ("192.168.1.50", 12345)
        with mock.patch("socket.socket", return_value=sock):
            net = discovery.fallback_network("192.168.1.100")
        self.assertEqual(str(net), "192.168.1.0/24")
        sock.close.assert_called_once()


class TestProbeAndSelect(unittest.TestCase):
    def test_probe_host_reads_device_id(self) -> None:
        with mock.patch("socket.create_connection", return_value=FakeSocket(BURST)):
            probe = discovery.probe_host("192.0.2.10")
        assert probe is not None
        self.assertEqual((probe.host, probe.device_id), ("192.0.2.10", "001122334455"))

    def test_probe_host_returns_none_when_refused(self) -> None:
        with mock.patch("socket.create_connection", side_effect=ConnectionRefusedError):
            self.assertIsNone(discovery.probe_host("192.0.2.11"))

    def test_probe_host_returns_none_for_silent_service(self) -> None:
        # 端口开着但不是海尔模块：连上只超时、读不到东西 → 不算数
        with mock.patch("socket.create_connection", return_value=FakeSocket(b"")):
            self.assertIsNone(discovery.probe_host("192.0.2.12"))

    def test_scan_keeps_order_and_swallows_errors(self) -> None:
        def probe(host: str, port: int, mac: bytes, timeout: float):  # noqa: ANN202
            if host.endswith(".9"):
                raise RuntimeError("探测任务自己炸了")
            return discovery.Probe(host, "001122334455") if host.endswith(".5") else None

        found = discovery.scan(["192.0.2.1", "192.0.2.5", "192.0.2.9"], probe=probe)
        self.assertEqual([p.host for p in found], ["192.0.2.5"])

    def test_select_device_by_id(self) -> None:
        found = [discovery.Probe("192.0.2.5", "aabbccddeeff"),
                 discovery.Probe("192.0.2.6", "001122334455")]
        self.assertEqual(discovery.select_device(found, "001122334455"), "192.0.2.6")

    def test_select_device_ignores_other_devices(self) -> None:
        found = [discovery.Probe("192.0.2.5", "aabbccddeeff")]
        self.assertIsNone(discovery.select_device(found, "001122334455"))

    def test_select_device_without_id_needs_a_single_answer(self) -> None:
        one = [discovery.Probe("192.0.2.5", None)]
        self.assertEqual(discovery.select_device(one, "host-192.0.2.9"), "192.0.2.5")
        two = one + [discovery.Probe("192.0.2.6", None)]
        self.assertIsNone(discovery.select_device(two, "host-192.0.2.9"))
        self.assertIsNone(discovery.select_device([], "001122334455"))


class TestAsyncFindDevice(unittest.IsolatedAsyncioTestCase):
    """异步入口：网段获取与扫描都丢进执行器，这里用一个假 hass 直接跑。"""

    class FakeHass:
        async def async_add_executor_job(self, func, *args):     # noqa: D102, ANN001
            return func(*args)

    async def test_finds_host_by_device_id(self) -> None:
        adapters = [{"enabled": True, "ipv4": [{"address": "192.168.1.5/30"}]}]
        with mock.patch.object(discovery, "_adapters", return_value=adapters), \
                mock.patch.object(discovery, "scan",
                                  return_value=[discovery.Probe("192.168.1.2", "001122334455")]):
            host = await discovery.async_find_device(self.FakeHass(), "001122334455")
        self.assertEqual(host, "192.168.1.2")

    async def test_returns_none_without_networks(self) -> None:
        with mock.patch.object(discovery, "_adapters", return_value=[]), \
                mock.patch.object(discovery, "fallback_network", return_value=None):
            self.assertIsNone(await discovery.async_find_device(self.FakeHass(), "001122334455"))


class FakeHass:
    """够协调器重发现用：执行器直跑 + 记录条目更新。"""

    def __init__(self) -> None:
        self.updated: list[dict] = []
        outer = self

        class ConfigEntries:
            def async_update_entry(inner, entry, **kwargs):      # noqa: N805, ANN001
                outer.updated.append(kwargs)

        self.config_entries = ConfigEntries()

    async def async_add_executor_job(self, func, *args):         # noqa: ANN001, D102
        return func(*args)


def make_coordinator(**overrides) -> HaierCoordinator:
    """只造重发现那条路要用的字段（不走 HA 的 __init__）。"""
    entry = SimpleNamespace(
        entry_id="entry-1",
        data={CONF_HOST: "192.168.1.100", CONF_PORT: 56800, CONF_SCAN_INTERVAL: 30,
              CONF_DEVICE_ID: "001122334455", CONF_REDISCOVER: True},
        options={})
    coordinator = HaierCoordinator.__new__(HaierCoordinator)
    coordinator.hass = FakeHass()
    coordinator.entry = entry
    coordinator.host, coordinator.port = "192.168.1.100", 56800
    coordinator.device_id = "001122334455"
    coordinator.rediscover = True
    coordinator.update_interval = timedelta(seconds=30)
    coordinator._ever_ok = True
    for key, value in overrides.items():
        setattr(coordinator, key, value)
    return coordinator


class TestCoordinatorRediscovery(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        coordinator_mod._LAST_SCAN.clear()

    async def test_updates_entry_when_device_moved(self) -> None:
        coordinator = make_coordinator()
        finder = mock.AsyncMock(return_value="192.168.1.180")
        with mock.patch.object(discovery, "async_find_device", finder):
            self.assertTrue(await coordinator._async_rediscover())

        self.assertEqual(coordinator.host, "192.168.1.180")
        self.assertEqual(coordinator.hass.updated,
                         [{"options": {CONF_HOST: "192.168.1.180", CONF_PORT: 56800}}])
        finder.assert_awaited_once()
        # 认人用的是存下来的设备 ID
        self.assertEqual(finder.await_args.args[1], "001122334455")

    async def test_cooldown_prevents_rescanning(self) -> None:
        coordinator = make_coordinator()
        finder = mock.AsyncMock(return_value="192.168.1.180")
        with mock.patch.object(discovery, "async_find_device", finder):
            self.assertTrue(await coordinator._async_rediscover())
            self.assertFalse(await coordinator._async_rediscover())      # 冷却中
        self.assertEqual(finder.await_count, 1)

    async def test_no_scan_when_disabled(self) -> None:
        coordinator = make_coordinator(rediscover=False)
        finder = mock.AsyncMock(return_value="192.168.1.180")
        with mock.patch.object(discovery, "async_find_device", finder):
            self.assertFalse(await coordinator._async_rediscover())
        finder.assert_not_awaited()
        self.assertEqual(coordinator.hass.updated, [])

    async def test_same_host_is_not_an_ip_change(self) -> None:
        coordinator = make_coordinator()
        with mock.patch.object(discovery, "async_find_device",
                               mock.AsyncMock(return_value="192.168.1.100")):
            self.assertFalse(await coordinator._async_rediscover())
        self.assertEqual(coordinator.hass.updated, [])

    async def test_scan_failure_does_not_raise(self) -> None:
        coordinator = make_coordinator()
        with mock.patch.object(discovery, "async_find_device",
                               mock.AsyncMock(side_effect=RuntimeError("boom"))):
            self.assertFalse(await coordinator._async_rediscover())

    async def test_update_data_recovers_after_ip_change(self) -> None:
        coordinator = make_coordinator()
        status = HaierStatus(BLOCK)
        calls = {"n": 0}

        async def fake_read() -> HaierStatus:
            calls["n"] += 1
            if calls["n"] == 1:
                raise ConnectionRefusedError("connection refused")
            return status

        coordinator._read = fake_read                        # type: ignore[method-assign]
        with mock.patch.object(discovery, "async_find_device",
                               mock.AsyncMock(return_value="192.168.1.180")):
            result = await coordinator._async_update_data()

        self.assertEqual(result.raw, BLOCK)
        self.assertEqual(coordinator.host, "192.168.1.180")
        self.assertEqual(calls["n"], 2)

    async def test_update_data_raises_when_rediscovery_fails(self) -> None:
        from homeassistant.helpers.update_coordinator import UpdateFailed

        coordinator = make_coordinator()

        async def fake_read() -> HaierStatus:
            raise ConnectionRefusedError("connection refused")

        coordinator._read = fake_read                        # type: ignore[method-assign]
        with mock.patch.object(discovery, "async_find_device", mock.AsyncMock(return_value=None)):
            with self.assertRaises(UpdateFailed):
                await coordinator._async_update_data()

    async def test_write_retries_after_rediscovery(self) -> None:
        coordinator = make_coordinator()
        status = HaierStatus(BLOCK)
        coordinator.async_set_updated_data = mock.Mock()
        coordinator._run = mock.AsyncMock(side_effect=[ConnectionRefusedError("refused"), status])
        # _async_rediscover 里用到的字段，直接假成功
        with mock.patch.object(HaierCoordinator, "_async_rediscover",
                               mock.AsyncMock(return_value=True)):
            result = await coordinator.async_write(lambda client: status)

        self.assertEqual(result.raw, BLOCK)
        self.assertEqual(coordinator._run.await_count, 2)
        coordinator.async_set_updated_data.assert_called_once()

    async def test_write_does_not_retry_rejected_command(self) -> None:
        from haier_esdk.client import HaierCommandRejected

        coordinator = make_coordinator()
        coordinator._run = mock.AsyncMock(side_effect=HaierCommandRejected("空调拒绝了该命令"))
        with mock.patch.object(HaierCoordinator, "_async_rediscover",
                               mock.AsyncMock(return_value=True)) as rediscover:
            with self.assertRaises(HaierCommandRejected):
                await coordinator.async_write(lambda client: None)

        self.assertEqual(coordinator._run.await_count, 1)
        rediscover.assert_not_awaited()

    async def test_matches_entry_after_auto_update(self) -> None:
        coordinator = make_coordinator()
        with mock.patch.object(discovery, "async_find_device",
                               mock.AsyncMock(return_value="192.168.1.180")):
            await coordinator._async_rediscover()

        # 条目被写回新 IP 后：参数一致 → 更新监听器不用重载
        entry = coordinator.entry
        entry.options[CONF_HOST] = "192.168.1.180"
        self.assertTrue(coordinator.matches_entry(entry))
        # 用户又手工改了别的（比如轮询间隔）→ 该重载
        entry.options[CONF_SCAN_INTERVAL] = 60
        self.assertFalse(coordinator.matches_entry(entry))


if __name__ == "__main__":
    unittest.main()
