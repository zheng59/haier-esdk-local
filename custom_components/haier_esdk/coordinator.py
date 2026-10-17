"""轮询协调器：把同步的 TCP 客户端放进执行器里跑，并串行化所有访问。

模块**同时只接受一个 TCP 连接**（对高频连接还会短暂限流），所以：
* 所有请求都过同一把 asyncio 锁；
* 每次操作开一个短连接，用完立刻关；
* 读状态时顺手把模块连接时主动推来的故障位图留下来（省一次往返）。
"""
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Any, Callable

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .client import DEFAULT_MAC, HaierClient, HaierError
from .const import (CONF_DEVICE_ID, CONF_HOST, CONF_MAC, CONF_PORT, CONF_SCAN_INTERVAL,
                    DEFAULT_PORT, DEFAULT_SCAN_INTERVAL, DOMAIN)
from .protocol import TYPE_ALARM, HaierStatus

_LOGGER = logging.getLogger(__name__)

Job = Callable[[HaierClient], HaierStatus]


class HaierCoordinator(DataUpdateCoordinator[HaierStatus]):
    """一辆空调 = 一个协调器。"""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        host: str = entry.data[CONF_HOST]
        port: int = entry.data.get(CONF_PORT, DEFAULT_PORT)
        interval: int = entry.options.get(
            CONF_SCAN_INTERVAL, entry.data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL))
        super().__init__(hass, _LOGGER,
                         name=f"{DOMAIN}-{host}",
                         update_interval=timedelta(seconds=interval))
        self.host, self.port = host, port
        self.mac: bytes = (entry.data.get(CONF_MAC) or DEFAULT_MAC)
        if isinstance(self.mac, str):
            self.mac = self.mac.encode("ascii")
        self.device_id: str | None = entry.data.get(CONF_DEVICE_ID)
        self.last_fault: bytes = b""
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ 读
    async def _async_update_data(self) -> HaierStatus:
        try:
            return await self._run(lambda client: client.read_status())
        except HaierError as exc:
            raise UpdateFailed(str(exc)) from exc

    # ------------------------------------------------------------------ 写
    async def async_write(self, job: Job) -> HaierStatus:
        """写操作：异常向上抛给实体（不吞成 UpdateFailed）。"""
        status = await self._run(job)
        self.async_set_updated_data(status)
        return status

    # ------------------------------------------------------------------ 底层
    async def _run(self, job: Job) -> HaierStatus:
        async with self._lock:
            return await self.hass.async_add_executor_job(self._run_sync, job)

    def _run_sync(self, job: Job) -> HaierStatus:
        with HaierClient(self.host, self.port, self.mac) as client:
            status = job(client)
            self._remember_fault(client)
            if client.device_id and not self.device_id:
                self.device_id = client.device_id
            return status

    def _remember_fault(self, client: HaierClient) -> None:
        """连接时模块会主动推故障位图（type=0x04），白拿的，不用额外请求。"""
        for env in client.burst:
            if env.frame.frame_type == TYPE_ALARM and len(env.frame.data) >= 2:
                self.last_fault = bytes(env.frame.data[2:])

    # ------------------------------------------------------------------ 供配置流程复用
    @staticmethod
    async def async_probe(hass: HomeAssistant, host: str,
                          port: int = DEFAULT_PORT) -> dict[str, Any]:
        """连一次、读一次，返回设备标识（配置流程用来验活 + 取 unique_id）。"""

        def _probe() -> dict[str, Any]:
            with HaierClient(host, port) as client:
                status = client.read_status()
                return {"device_id": client.device_id, "status": status}

        return await hass.async_add_executor_job(_probe)
